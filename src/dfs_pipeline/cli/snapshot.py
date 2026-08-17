"""``dfs-snapshot`` -- capture one weekly bundle of point-in-time slate data.

NOT YET IMPLEMENTED. This module exists so that the packaging is verifiable
end to end: installing the project must produce a working ``dfs-snapshot``
command on PATH, and that claim should be testable before there is anything
behind it.

The command deliberately exits non-zero. A stub that prints a friendly
message and exits 0 would report success it has not earned, which is the
failure mode this project treats as worse than crashing.

Planned behaviour, per the Phase 0 specification:

* DraftKings slate data (salaries, positions, IDs, game info, lock times)
  behind an adapter, with the manual ``DKSalaries.csv`` import as a
  proven-equivalent fallback path.
* Projections as they stood, timestamped on ingest.
* Odds snapshot (spreads and totals) from The Odds API.
* Post-slate realized scoring computed from nflverse at DK Classic rules.

Every stored observation carries two timestamps that are never overwritten:
``effective_at`` (when the source says the information was current) and
``captured_at`` (when this system obtained it). Reconstructing the
information state at a past cutoff requires both to fall at or before it.
"""

from __future__ import annotations

import sys

EXIT_NOT_IMPLEMENTED = 2


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``dfs-snapshot`` console script.

    Returns the process exit status rather than calling :func:`sys.exit`
    directly, so tests can invoke it without trapping ``SystemExit``.
    """
    _ = argv  # no arguments parsed yet
    print(
        "dfs-snapshot is not implemented yet.\n"
        "\n"
        "Packaging is wired up correctly -- this command exists and is on your\n"
        "PATH -- but no capture logic has been written. It exits non-zero by\n"
        "design so that nothing mistakes this for a working snapshot.\n"
        "\n"
        "See DEVLOG.md for current status.",
        file=sys.stderr,
    )
    return EXIT_NOT_IMPLEMENTED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
