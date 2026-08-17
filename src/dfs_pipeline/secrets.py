"""Reading credentials from the environment, and never from source.

A key in source code is one careless commit from being public forever, and
git does not forget. Values are read from the process environment, falling
back to a ``.env`` file that ``.gitignore`` refuses to stage.

Nothing here logs, prints, or returns a secret in an error message. The
failure modes are "it is missing" and "it is present" -- never "here is what
we found instead."
"""

from __future__ import annotations

import os
import re
from pathlib import Path

__all__ = ["MissingSecret", "read_odds_api_key", "load_dotenv_value"]

ODDS_API_KEY = "ODDS_API_KEY"

#: Matches KEY=value, tolerating surrounding whitespace, `export ` prefixes,
#: and quoted values -- all of which operators write without thinking.
_ENV_LINE = re.compile(
    r"""^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*?)\s*$"""
)


class MissingSecret(RuntimeError):
    """A required credential was not found.

    The message names the variable and where to put it, never any value that
    was found in its place -- a "wrong key" message that echoes the wrong key
    still leaks a secret.
    """

    def __init__(self, name: str, searched: list[str]) -> None:
        self.name = name
        locations = ", ".join(searched)
        super().__init__(
            f"{name} is not set. Add it to .env (copy .env.example) or export "
            f"it in your shell. Searched: {locations}."
        )


def load_dotenv_value(name: str, dotenv_path: str | Path = ".env") -> str | None:
    """Read one value from a ``.env`` file without importing the whole file.

    Deliberately does not mutate ``os.environ``. A capture run should not
    change the environment of the process that launched it, and a secret
    quietly injected into the environment is a secret that leaks into every
    subprocess and crash report thereafter.
    """
    path = Path(dotenv_path)
    if not path.is_file():
        return None

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if match is None or match.group("key") != name:
            continue
        value = match.group("value")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value.strip() or None
    return None


def read_odds_api_key(dotenv_path: str | Path = ".env") -> str:
    """Return the Odds API key, or raise :class:`MissingSecret`.

    Environment first, ``.env`` second: an explicitly exported variable should
    beat a file left over from last season.
    """
    value = os.environ.get(ODDS_API_KEY) or load_dotenv_value(
        ODDS_API_KEY, dotenv_path
    )
    if not value or value.strip() in ("", "replace_me", "your_key_here"):
        # The placeholder from .env.example counts as missing. Treating it as a
        # real key produces a 401 from the API and a confusing error, when the
        # actual problem is that the file was never filled in.
        raise MissingSecret(
            ODDS_API_KEY, [f"environment variable {ODDS_API_KEY}", str(dotenv_path)]
        )
    return value.strip()
