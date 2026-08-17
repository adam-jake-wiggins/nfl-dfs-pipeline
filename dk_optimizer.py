#!/usr/bin/env python3
"""
DraftKings NFL Classic Lineup Optimizer
=======================================

True mixed-integer linear programming optimizer. Every lineup it returns is
provably optimal for the constraints you gave it, not a greedy approximation.

BASIC USE
---------
    python dk_optimizer.py DKSalaries.csv

    python dk_optimizer.py DKSalaries.csv \
        --projections my_projections.csv \
        --lineups 20 \
        --stack 2 \
        --bringback 1 \
        --max-exposure 0.4 \
        --output lineups.csv

INPUTS
------
DKSalaries.csv  Export straight from the DraftKings contest page
                (Export to CSV link above the player list).

projections.csv Optional. Two columns minimum: Name, Projection.
                Any of these header spellings work: Name / Player,
                Projection / Proj / FPTS / Points.
                Without it, the optimizer falls back to AvgPointsPerGame,
                which is a weak proxy. Bring real projections.

OUTPUT
------
A CSV with the header row QB,RB,RB,WR,WR,WR,TE,FLEX,DST containing
"Name (ID)" entries, which is the format DraftKings accepts for bulk
lineup upload.
"""

import argparse
import csv
import sys
from collections import defaultdict

import pulp

SALARY_CAP = 50000
ROSTER_SIZE = 9
SLOT_ORDER = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST"]


# ----------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------

def _pick(row, *candidates):
    """Return the first present, non-empty column from candidates."""
    for c in candidates:
        for key in row:
            if key and key.strip().lower() == c.lower():
                val = row[key]
                if val is not None and str(val).strip() != "":
                    return str(val).strip()
    return None


def load_salaries(path):
    players = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            name = _pick(row, "Name")
            pos = _pick(row, "Position")
            if not name or not pos:
                continue

            salary = _pick(row, "Salary")
            try:
                salary = int(float(salary))
            except (TypeError, ValueError):
                continue

            avg = _pick(row, "AvgPointsPerGame")
            try:
                avg = float(avg)
            except (TypeError, ValueError):
                avg = 0.0

            game_info = _pick(row, "Game Info") or ""
            game_key = game_info.split(" ")[0] if game_info else "UNKNOWN"

            team = _pick(row, "TeamAbbrev") or ""
            opp = ""
            if "@" in game_key:
                away, home = game_key.split("@", 1)
                opp = home if team == away else away

            players.append({
                "name": name,
                "id": _pick(row, "ID") or "",
                "pos": pos.upper(),
                "salary": salary,
                "avg": avg,
                "proj": avg,
                "team": team.upper(),
                "opp": opp.upper(),
                "game": game_key,
            })

    if not players:
        sys.exit(f"No usable rows found in {path}. Is it a DraftKings salary export?")
    return players


def apply_projections(players, path):
    proj = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            name = _pick(row, "Name", "Player", "Player Name")
            val = _pick(row, "Projection", "Proj", "FPTS", "Points", "Fantasy Points")
            if not name or val is None:
                continue
            try:
                proj[name.strip().lower()] = float(val)
            except ValueError:
                continue

    matched = 0
    for p in players:
        key = p["name"].strip().lower()
        if key in proj:
            p["proj"] = proj[key]
            matched += 1

    unmatched = len(proj) - matched
    print(f"Projections matched to {matched} players.", file=sys.stderr)
    if unmatched > 0:
        print(f"  {unmatched} projection rows did not match a salary name.", file=sys.stderr)
    if matched == 0:
        print("  WARNING: zero matches. Check name spellings. Falling back to "
              "AvgPointsPerGame.", file=sys.stderr)
    return players


# ----------------------------------------------------------------------
# Optimization
# ----------------------------------------------------------------------

def build_lineups(players, n_lineups, stack, bringback, max_exposure,
                  min_salary, min_unique, locks, bans):
    pool = [p for p in players if p["name"] not in bans and p["proj"] > 0]
    if len(pool) < ROSTER_SIZE:
        sys.exit("Player pool too small after filtering. Check --ban and projections.")

    by_pos = defaultdict(list)
    for i, p in enumerate(pool):
        by_pos[p["pos"]].append(i)

    for required in ("QB", "RB", "WR", "TE", "DST"):
        if not by_pos[required]:
            sys.exit(f"No {required} found in the player pool.")

    games = defaultdict(list)
    for i, p in enumerate(pool):
        games[p["game"]].append(i)

    exposure_cap = int(max_exposure * n_lineups) if max_exposure < 1.0 else n_lineups
    exposure_cap = max(1, exposure_cap)

    used_counts = defaultdict(int)
    previous = []
    lineups = []

    for n in range(n_lineups):
        prob = pulp.LpProblem(f"dk_lineup_{n}", pulp.LpMaximize)
        x = {i: pulp.LpVariable(f"x_{i}", cat="Binary") for i in range(len(pool))}

        prob += pulp.lpSum(pool[i]["proj"] * x[i] for i in x)

        # Roster shape
        prob += pulp.lpSum(x[i] for i in x) == ROSTER_SIZE
        prob += pulp.lpSum(x[i] for i in by_pos["QB"]) == 1
        prob += pulp.lpSum(x[i] for i in by_pos["DST"]) == 1
        prob += pulp.lpSum(x[i] for i in by_pos["RB"]) >= 2
        prob += pulp.lpSum(x[i] for i in by_pos["RB"]) <= 3
        prob += pulp.lpSum(x[i] for i in by_pos["WR"]) >= 3
        prob += pulp.lpSum(x[i] for i in by_pos["WR"]) <= 4
        prob += pulp.lpSum(x[i] for i in by_pos["TE"]) >= 1
        prob += pulp.lpSum(x[i] for i in by_pos["TE"]) <= 2

        # Salary
        prob += pulp.lpSum(pool[i]["salary"] * x[i] for i in x) <= SALARY_CAP
        if min_salary > 0:
            prob += pulp.lpSum(pool[i]["salary"] * x[i] for i in x) >= min_salary

        # DraftKings requires players from at least two different games
        y = {g: pulp.LpVariable(f"g_{k}", cat="Binary") for k, g in enumerate(games)}
        for g, idxs in games.items():
            prob += y[g] <= pulp.lpSum(x[i] for i in idxs)
        prob += pulp.lpSum(y.values()) >= 2

        # Locks
        for locked in locks:
            idxs = [i for i in x if pool[i]["name"].lower() == locked.lower()]
            if not idxs:
                sys.exit(f"Locked player not found in pool: {locked}")
            prob += pulp.lpSum(x[i] for i in idxs) == 1

        # Stacking: QB plus N pass catchers from his own team
        if stack > 0:
            for qb in by_pos["QB"]:
                team = pool[qb]["team"]
                mates = [i for i in x
                         if pool[i]["team"] == team and pool[i]["pos"] in ("WR", "TE")]
                prob += pulp.lpSum(x[i] for i in mates) >= stack * x[qb]

        # Bring-back: pass catcher from the opposing team in the QB's game
        if bringback > 0:
            for qb in by_pos["QB"]:
                opp = pool[qb]["opp"]
                if not opp:
                    continue
                opp_catchers = [i for i in x
                                if pool[i]["team"] == opp and pool[i]["pos"] in ("WR", "TE", "RB")]
                if opp_catchers:
                    prob += pulp.lpSum(x[i] for i in opp_catchers) >= bringback * x[qb]

        # Never pair a QB with the defense playing against him
        for qb in by_pos["QB"]:
            for dst in by_pos["DST"]:
                if pool[dst]["team"] and pool[dst]["team"] == pool[qb]["opp"]:
                    prob += x[qb] + x[dst] <= 1

        # Exposure caps across the set
        for i in x:
            if used_counts[pool[i]["name"]] >= exposure_cap:
                prob += x[i] == 0

        # Force differences between lineups
        for prev in previous:
            prob += pulp.lpSum(x[i] for i in prev) <= ROSTER_SIZE - min_unique

        status = prob.solve(pulp.PULP_CBC_CMD(msg=0))
        if pulp.LpStatus[status] != "Optimal":
            print(f"Stopped after {len(lineups)} lineups: no further feasible solution.",
                  file=sys.stderr)
            break

        chosen = [i for i in x if x[i].value() and x[i].value() > 0.5]
        previous.append(chosen)
        for i in chosen:
            used_counts[pool[i]["name"]] += 1
        lineups.append([pool[i] for i in chosen])

    return lineups


# ----------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------

def assign_slots(lineup):
    """Map nine players onto the DraftKings slot order."""
    remaining = list(lineup)
    slots = {}

    def take(pos):
        for p in remaining:
            if p["pos"] == pos:
                remaining.remove(p)
                return p
        return None

    slots["QB"] = [take("QB")]
    slots["RB"] = [take("RB"), take("RB")]
    slots["WR"] = [take("WR"), take("WR"), take("WR")]
    slots["TE"] = [take("TE")]
    slots["DST"] = [take("DST")]
    flex = [p for p in remaining if p["pos"] in ("RB", "WR", "TE")]
    slots["FLEX"] = [flex[0]] if flex else [None]

    ordered = (slots["QB"] + slots["RB"] + slots["WR"]
               + slots["TE"] + slots["FLEX"] + slots["DST"])
    return ordered


def write_output(lineups, path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(SLOT_ORDER)
        for lu in lineups:
            ordered = assign_slots(lu)
            w.writerow([f"{p['name']} ({p['id']})" if p else "" for p in ordered])


def summarize(lineups):
    print()
    for n, lu in enumerate(lineups, 1):
        ordered = assign_slots(lu)
        salary = sum(p["salary"] for p in lu)
        proj = sum(p["proj"] for p in lu)
        print(f"Lineup {n}:  ${salary:,}  |  {proj:.1f} proj")
        for slot, p in zip(SLOT_ORDER, ordered):
            if p:
                print(f"    {slot:<5} {p['name']:<24} {p['team']:<4} "
                      f"${p['salary']:>6,}  {p['proj']:>5.1f}")
        print()

    counts = defaultdict(int)
    for lu in lineups:
        for p in lu:
            counts[p["name"]] += 1
    print("Exposure:")
    for name, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"    {c/len(lineups):>5.0%}  {name}")


# ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="DraftKings NFL Classic optimizer")
    ap.add_argument("salaries", help="DKSalaries.csv exported from DraftKings")
    ap.add_argument("--projections", help="CSV with Name and Projection columns")
    ap.add_argument("--lineups", type=int, default=1, help="How many lineups to build")
    ap.add_argument("--stack", type=int, default=0,
                    help="Pass catchers required from the QB's own team")
    ap.add_argument("--bringback", type=int, default=0,
                    help="Players required from the team opposing the QB")
    ap.add_argument("--max-exposure", type=float, default=1.0,
                    help="Cap any one player at this share of lineups, e.g. 0.4")
    ap.add_argument("--min-salary", type=int, default=0,
                    help="Force spending at least this much of the cap")
    ap.add_argument("--min-unique", type=int, default=1,
                    help="Players that must differ between any two lineups")
    ap.add_argument("--lock", action="append", default=[],
                    help="Force a player in. Repeatable.")
    ap.add_argument("--ban", action="append", default=[],
                    help="Exclude a player. Repeatable.")
    ap.add_argument("--output", default="lineups.csv", help="Output CSV path")
    args = ap.parse_args()

    players = load_salaries(args.salaries)
    print(f"Loaded {len(players)} players from {args.salaries}.", file=sys.stderr)

    if args.projections:
        apply_projections(players, args.projections)
    else:
        print("No projections supplied. Using AvgPointsPerGame, which is a weak "
              "proxy for a single slate.", file=sys.stderr)

    lineups = build_lineups(
        players,
        n_lineups=args.lineups,
        stack=args.stack,
        bringback=args.bringback,
        max_exposure=args.max_exposure,
        min_salary=args.min_salary,
        min_unique=args.min_unique,
        locks=args.lock,
        bans=set(args.ban),
    )

    if not lineups:
        sys.exit("No feasible lineups. Loosen your constraints.")

    write_output(lineups, args.output)
    summarize(lineups)
    print(f"Wrote {len(lineups)} lineups to {args.output}, "
          f"ready for DraftKings bulk upload.")


if __name__ == "__main__":
    main()
