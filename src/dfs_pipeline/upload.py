"""DraftKings bulk-upload format for NFL Classic lineups.

Schema **VERIFIED 2026-08-17** against a real upload template downloaded from
DraftKings. Every claim below is quoted from that file rather than assumed --
the prototype's format was a belief, and beliefs about file formats are how
uploads get silently rejected on a Sunday morning.

What the template actually contains
-----------------------------------
One file serving two purposes side by side::

    cols 0-8   QB,RB,RB,WR,WR,WR,TE,FLEX,DST     <- the lineup you fill in
    col  9     (blank spacer)
    col  10+   Position,Name + ID,Name,ID,...    <- the salary listing to copy from

DraftKings' own instructions, verbatim from the file:

1. Locate the player you want to select in the list below
2. Copy the ID of your player (you can use the Name + ID column or the ID column)
3. Paste the ID into the roster position desired
4. You must include an ID for each player; you cannot use just the player's name
5. **You can create up to 500 lineups per file**

Three things that settles
-------------------------
**The header was right.** ``QB,RB,RB,WR,WR,WR,TE,FLEX,DST`` matches the
prototype's assumption exactly.

**``Name (ID)`` is accepted.** Instruction 2 permits either the ``Name + ID``
column or the bare ``ID`` column, and the template's ``Name + ID`` values are
literally ``C.J. Stroud (43837771)``. Instruction 4 rules out a bare name.
This module writes ``Name (ID)`` because it stays human-readable when someone
opens the file to check it, and a reviewer who cannot read the file cannot
catch a mistake in it.

**One id per player, used in any slot.** The salary block lists each player
once with a combined ``Roster Position`` such as ``RB/FLEX`` and a single
``ID``. So the FLEX row takes that same id -- there is no separate FLEX
identifier to hunt for here, even though the draftables API does issue one.
Using the API's FLEX ``draftableId`` in the FLEX column would be a plausible
mistake with no way to notice until an upload failed.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from pathlib import Path

from dfs_pipeline.contest import ROSTER_SIZE, SLOT_ORDER

__all__ = [
    "MAX_LINEUPS_PER_FILE",
    "UPLOAD_HEADER",
    "UploadError",
    "format_cell",
    "write_upload_csv",
]

#: DraftKings' stated limit, verbatim from the template's instructions:
#: "You can create up to 500 lineups per file". A file exceeding this is
#: rejected at upload, which is the worst possible time to discover it -- so
#: the writer refuses before writing rather than after.
MAX_LINEUPS_PER_FILE = 500

#: The header row DraftKings expects. VERIFIED against a real template.
UPLOAD_HEADER: tuple[str, ...] = SLOT_ORDER


class UploadError(ValueError):
    """A lineup set cannot be written as a valid DraftKings upload."""


def format_cell(name: str, player_id: str | int) -> str:
    """Render one roster cell as ``Name (ID)``.

    An empty id is refused rather than written as ``Name ()``. DraftKings'
    instruction 4 is explicit that a name alone is not accepted, so a cell
    without an id produces a file that looks complete and uploads as broken.
    """
    identifier = str(player_id).strip()
    if not identifier:
        raise UploadError(
            f"no DraftKings id for {name!r}. DraftKings requires an id in every "
            f"cell; a name alone is rejected at upload."
        )
    cleaned = str(name).strip()
    return f"{cleaned} ({identifier})" if cleaned else identifier


def write_upload_csv(
    lineups: Iterable[Sequence[tuple[str, str | int]]],
    path: str | Path,
) -> int:
    """Write lineups in DraftKings bulk-upload format. Returns the count.

    Each lineup is nine ``(name, player_id)`` pairs **already in slot order** --
    QB, RB, RB, WR, WR, WR, TE, FLEX, DST. Slot assignment is the optimizer's
    job; this function's only responsibility is the file format, and mixing the
    two is how a formatting change quietly becomes a lineup change.
    """
    materialized = [list(lineup) for lineup in lineups]

    if not materialized:
        raise UploadError("no lineups to write")

    if len(materialized) > MAX_LINEUPS_PER_FILE:
        raise UploadError(
            f"{len(materialized)} lineups exceeds DraftKings' limit of "
            f"{MAX_LINEUPS_PER_FILE} per file. Split them across multiple files."
        )

    for index, lineup in enumerate(materialized, start=1):
        if len(lineup) != ROSTER_SIZE:
            raise UploadError(
                f"lineup {index} has {len(lineup)} players, expected {ROSTER_SIZE} "
                f"({', '.join(UPLOAD_HEADER)})"
            )

    target = Path(path)
    with open(target, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(UPLOAD_HEADER)
        for lineup in materialized:
            writer.writerow([format_cell(name, pid) for name, pid in lineup])

    return len(materialized)
