#!/usr/bin/env python3
"""
Vegas Implied Team Total Adjustment Layer
=========================================

Takes a free projection set (Daily Fantasy Fuel, Stokastic, or anything with
a name and a projected points column) and reweights it by the betting market's
view of each game.

The premise: implied team total is the single best public predictor of how many
points an offense will actually score, and fantasy scoring is downstream of team
scoring. This layer applies that view on top of whatever projections you feed it.

USAGE
-----
    # 1. Generate a blank odds sheet to fill in
    python dk_vegas_adjust.py --make-odds-template odds.csv

    # 2. Fill in spread and total for each team, then run
    python dk_vegas_adjust.py projections.csv odds.csv --output adjusted.csv

    # 3. Feed the result to the optimizer
    python dk_optimizer.py DKSalaries.csv --projections adjusted.csv \
        --lineups 20 --stack 2 --bringback 1

ODDS SHEET FORMAT
-----------------
    Team,Opponent,Spread,Total
    KC,BUF,-2.5,48.5
    BUF,KC,2.5,48.5

Spread is from that team's perspective. Negative means favored. Enter a row for
both teams in every game. Totals are the game total, identical for both rows.

THE MATH
--------
    implied team total = (total / 2) - (spread / 2)

A 48.5 total with KC at -2.5 gives KC 25.75 and Buffalo 22.75.

Offensive players scale by (team implied / slate baseline) raised to --alpha.
Defenses scale inversely by the OPPONENT's implied total raised to --beta, with
a separate bump for being favored, since trailing teams throw more and give up
more sacks and interceptions.

IMPORTANT CAVEAT
----------------
Most published projections already bake in Vegas lines to some degree. Applying
a full-strength adjustment on top of them double counts the market. That is why
alpha defaults to 0.50 rather than 1.0, and why the adjustment is clamped. Turn
it up only if you have reason to believe your source ignores the market.
"""

import argparse
import csv
import sys
from statistics import mean

OFFENSE = {"QB", "RB", "WR", "TE"}
DEFENSE = {"DST", "D", "DEF", "D/ST"}


def _pick(row, *candidates):
    for c in candidates:
        for key in row:
            if key and key.strip().lower() == c.lower():
                val = row[key]
                if val is not None and str(val).strip() != "":
                    return str(val).strip()
    return None


def _num(val):
    if val is None:
        return None
    try:
        return float(str(val).replace("+", "").replace(",", "").strip())
    except ValueError:
        return None


# ----------------------------------------------------------------------

NFL_TEAMS = [
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
    "DET", "GB", "HOU", "IND", "JAX", "KC", "LAC", "LAR", "LV", "MIA",
    "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB",
    "TEN", "WAS",
]


def make_template(path, teams=None):
    teams = teams or NFL_TEAMS
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Team", "Opponent", "Spread", "Total"])
        for t in teams:
            w.writerow([t, "", "", ""])
    print(f"Wrote odds template to {path} with all 32 teams.")
    print("Fill in Opponent, Spread (negative if favored), and Total for the "
          "teams on your slate. Delete the rest, or leave them blank and they "
          "will be ignored.")


def load_projections(path):
    players = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            name = _pick(row, "Name", "Player", "Player Name", "PlayerName")
            proj = _num(_pick(row, "Projection", "Proj", "FPTS", "Points",
                              "Points Proj", "Fantasy Points", "ProjPoints",
                              "DK Projection", "Projected Points"))
            if not name or proj is None:
                continue
            pos = (_pick(row, "Position", "Pos") or "").upper()
            team = (_pick(row, "Team", "TeamAbbrev", "Tm") or "").upper()
            players.append({
                "name": name, "pos": pos, "team": team,
                "base": proj, "proj": proj, "factor": 1.0,
            })
    if not players:
        sys.exit(f"Could not read any name/projection pairs from {path}.")
    return players


def load_odds(path):
    odds = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            team = (_pick(row, "Team", "TeamAbbrev", "Tm") or "").upper()
            opp = (_pick(row, "Opponent", "Opp", "Against") or "").upper()
            spread = _num(_pick(row, "Spread", "Line", "Point Spread"))
            total = _num(_pick(row, "Total", "O/U", "OverUnder", "Game Total"))
            if not team or spread is None or total is None:
                continue
            odds[team] = {
                "opp": opp,
                "spread": spread,
                "total": total,
                "implied": (total / 2.0) - (spread / 2.0),
            }
    if not odds:
        sys.exit(f"No usable odds rows in {path}. Need Team, Spread, and Total.")
    return odds


# ----------------------------------------------------------------------

def adjust(players, odds, alpha, beta, gamma, clamp_lo, clamp_hi, baseline):
    if baseline is None:
        baseline = mean(o["implied"] for o in odds.values())

    unmatched = set()
    for p in players:
        info = odds.get(p["team"])
        if not info:
            if p["team"]:
                unmatched.add(p["team"])
            continue

        if p["pos"] in DEFENSE:
            opp = odds.get(info["opp"])
            opp_implied = opp["implied"] if opp else baseline
            factor = (baseline / max(opp_implied, 6.0)) ** beta
            # Favored defenses see more pass attempts and pressure situations
            if info["spread"] < 0:
                factor *= 1.0 + gamma * (min(-info["spread"], 14.0) / 14.0)
        elif p["pos"] in OFFENSE or not p["pos"]:
            factor = (info["implied"] / baseline) ** alpha
        else:
            factor = 1.0

        factor = max(clamp_lo, min(clamp_hi, factor))
        p["factor"] = factor
        p["proj"] = round(p["base"] * factor, 2)

    if unmatched:
        print(f"WARNING: no odds row for these teams: {', '.join(sorted(unmatched))}",
              file=sys.stderr)
    return baseline


def print_slate(odds, baseline):
    print(f"\nSlate baseline implied total: {baseline:.2f}\n")
    print(f"{'TEAM':<6}{'OPP':<6}{'SPREAD':>8}{'TOTAL':>8}{'IMPLIED':>10}")
    print("-" * 38)
    for team, o in sorted(odds.items(), key=lambda kv: -kv[1]["implied"]):
        print(f"{team:<6}{o['opp']:<6}{o['spread']:>8.1f}"
              f"{o['total']:>8.1f}{o['implied']:>10.2f}")


def print_movers(players, n=12):
    moved = [p for p in players if abs(p["factor"] - 1.0) > 0.001 and p["base"] >= 5]
    moved.sort(key=lambda p: -abs(p["proj"] - p["base"]))
    if not moved:
        return
    print(f"\nLargest adjustments (players projected 5+ points):\n")
    print(f"{'PLAYER':<24}{'POS':<5}{'TM':<5}{'BASE':>7}{'ADJ':>7}{'DELTA':>8}")
    print("-" * 56)
    for p in moved[:n]:
        delta = p["proj"] - p["base"]
        print(f"{p['name'][:23]:<24}{p['pos']:<5}{p['team']:<5}"
              f"{p['base']:>7.1f}{p['proj']:>7.1f}{delta:>+8.1f}")


def write_output(players, path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Name", "Projection", "BaseProjection", "Team", "Position",
                    "VegasFactor"])
        for p in players:
            w.writerow([p["name"], p["proj"], p["base"], p["team"], p["pos"],
                        round(p["factor"], 4)])


# ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Vegas-adjust DFS projections")
    ap.add_argument("projections", nargs="?", help="Projection CSV from your source")
    ap.add_argument("odds", nargs="?", help="Odds CSV with Team, Spread, Total")
    ap.add_argument("--make-odds-template", metavar="PATH",
                    help="Write a blank odds sheet and exit")
    ap.add_argument("--alpha", type=float, default=0.50,
                    help="Offense sensitivity to implied total. 0 disables, "
                         "1.0 is full strength. Default 0.50")
    ap.add_argument("--beta", type=float, default=0.60,
                    help="Defense sensitivity to opponent implied total")
    ap.add_argument("--gamma", type=float, default=0.12,
                    help="Extra credit for favored defenses, scaled by spread")
    ap.add_argument("--clamp", type=float, nargs=2, default=[0.78, 1.28],
                    metavar=("LO", "HI"),
                    help="Bound the adjustment factor. Default 0.78 1.28")
    ap.add_argument("--baseline", type=float,
                    help="Override the slate average implied total")
    ap.add_argument("--output", default="adjusted_projections.csv")
    args = ap.parse_args()

    if args.make_odds_template:
        make_template(args.make_odds_template)
        return

    if not args.projections or not args.odds:
        ap.error("Provide both a projections CSV and an odds CSV, or use "
                 "--make-odds-template.")

    players = load_projections(args.projections)
    odds = load_odds(args.odds)
    print(f"Loaded {len(players)} projections and {len(odds)} team odds rows.",
          file=sys.stderr)

    baseline = adjust(players, odds, args.alpha, args.beta, args.gamma,
                      args.clamp[0], args.clamp[1], args.baseline)

    print_slate(odds, baseline)
    print_movers(players)
    write_output(players, args.output)
    print(f"\nWrote {len(players)} adjusted projections to {args.output}.")
    print(f"Feed it in with: --projections {args.output}")


if __name__ == "__main__":
    main()
