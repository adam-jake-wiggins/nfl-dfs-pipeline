"""Player-name normalization.

Projection sources identify players by name, and no two of them spell names
the same way. ``Ja'Marr Chase``, ``JaMarr Chase`` and ``Ja Marr Chase`` are
one person; ``Travis Etienne Jr.`` and ``Travis Etienne`` are one person;
``C.J. Stroud`` and ``CJ Stroud`` are one person.

What this module is
-------------------
A deterministic key generator, used to give a projection row a **stable
identifier within its own source**, so that re-capturing the same source next
week lines up with last week's history even if the spelling drifts slightly.

What this module is NOT
-----------------------
A cross-source matcher. Resolving a projection-source name onto an nflverse
identifier is a different and harder problem: it needs team and position
agreement, a persistent crosswalk, and a human in the loop for the residue.
Normalization alone would happily merge two different players who share a
name, which is why :func:`normalize_name` returns a key and never a claim of
identity.

The distinction matters because collapsing it is exactly how the prototype
failed: it matched on lowercased names, missed, and silently substituted a
season average.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["normalize_name", "NAME_SUFFIXES", "name_key"]

#: Generational suffixes stripped when they appear as the final token. Sources
#: are inconsistent about carrying them, so they cannot participate in a key.
#: Only matched at the end, so a surname like "Ivy" or a first name "Roman"
#: is untouched.
NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})

_PUNCTUATION_TO_DROP = re.compile(r"[.'’`]")
_PUNCTUATION_TO_SPACE = re.compile(r"[-_/\\,]")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]")
_WHITESPACE = re.compile(r"\s+")


def normalize_name(raw: str) -> str:
    """Return a deterministic matching key for a player name.

    The transformation, in order:

    1. Unicode-normalize and strip accents (``Nuñez`` -> ``nunez``).
    2. Casefold.
    3. Delete periods and apostrophes, so ``C.J.`` -> ``cj`` and ``Ja'Marr``
       -> ``jamarr``. These are dropped rather than spaced because sources
       write ``C.J.``, ``CJ`` and ``C J`` for the same person.
    4. Convert hyphens and slashes to spaces, so ``Croskey-Merritt`` and
       ``Croskey Merritt`` agree.
    5. Drop any remaining non-alphanumeric characters.
    6. Strip trailing generational suffixes.
    7. Collapse whitespace.

    Returns an empty string for input that normalizes away entirely, which
    callers must treat as unusable rather than as a valid key.
    """
    if not raw:
        return ""

    decomposed = unicodedata.normalize("NFKD", raw)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))

    text = ascii_only.casefold()
    text = _PUNCTUATION_TO_DROP.sub("", text)
    text = _PUNCTUATION_TO_SPACE.sub(" ", text)
    text = _NON_ALNUM.sub(" ", text)
    tokens = _WHITESPACE.sub(" ", text).strip().split()

    # Strip suffixes from the end only, and never the entire name: a single
    # token that happens to look like a suffix is a name, not a suffix.
    while len(tokens) > 1 and tokens[-1] in NAME_SUFFIXES:
        tokens.pop()

    return " ".join(tokens)


def name_key(name: str, team: str | None = None, position: str | None = None) -> str:
    """Build a subject key, disambiguated by team and position when supplied.

    Two different players genuinely can share a name on one slate. Where the
    source gives team and position, folding them into the key separates them;
    where it does not, the caller must detect the collision and fail rather
    than silently merge two people's projections.
    """
    normalized = normalize_name(name)
    parts = [normalized]
    if team:
        parts.append(team.strip().upper())
    if position:
        parts.append(position.strip().upper())
    return "|".join(parts)
