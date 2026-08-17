"""Configuration: TOML file for standing defaults, CLI flags to override.

Precedence is explicit and one-directional::

    built-in defaults  <  dfs.toml  <  command-line flags

A weekly command should not require re-typing the same six paths, but the
config must never silently win over something the operator typed. Every
resolved value records where it came from, so ``--show-config`` can answer
"why is it using that store?" without anyone reading source.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["Config", "ConfigError", "load_config", "DEFAULT_CONFIG_NAMES"]

#: Files searched, in order, when no --config is given.
DEFAULT_CONFIG_NAMES = ("dfs.toml", ".dfs.toml")

_DEFAULTS: dict[str, dict[str, Any]] = {
    "store": {"path": "data/snapshots.sqlite"},
    "runs": {"directory": "runs"},
    "capture": {"on_duplicate": "error"},
}

_VALID_ON_DUPLICATE = ("error", "ignore")


class ConfigError(ValueError):
    """Raised when a config file exists but cannot be trusted.

    A malformed config is a hard failure rather than a fallback to defaults.
    Silently ignoring a file the operator wrote -- and running against a
    different store than they intended -- is exactly the class of quiet
    wrongness this project refuses.
    """


@dataclass(frozen=True, slots=True)
class Config:
    """Resolved configuration, with the origin of each value retained."""

    store_path: Path
    runs_directory: Path
    on_duplicate: str
    source_file: Path | None = None
    origins: dict[str, str] = field(default_factory=dict)

    def describe(self) -> str:
        """Human-readable dump for ``--show-config``."""
        origin = str(self.source_file) if self.source_file else "built-in defaults"
        lines = [f"config source : {origin}", ""]
        for key, value in (
            ("store.path", self.store_path),
            ("runs.directory", self.runs_directory),
            ("capture.on_duplicate", self.on_duplicate),
        ):
            lines.append(f"{key:<22} {value}   [{self.origins.get(key, 'default')}]")
        return "\n".join(lines)


def _find_config(start: Path) -> Path | None:
    for name in DEFAULT_CONFIG_NAMES:
        candidate = start / name
        if candidate.is_file():
            return candidate
    return None


def load_config(
    explicit_path: str | Path | None = None,
    *,
    search_from: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> Config:
    """Resolve configuration from defaults, an optional TOML file, and overrides.

    ``overrides`` carries command-line values; ``None`` entries mean "the flag
    was not given" and leave the lower-precedence value in place.
    """
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    origins: dict[str, str] = {}

    if explicit_path is not None:
        source = Path(explicit_path)
        if not source.is_file():
            raise ConfigError(f"config file not found: {source}")
    else:
        source = _find_config(Path(search_from or Path.cwd()))

    data: dict[str, Any] = {}
    if source is not None:
        try:
            data = tomllib.loads(source.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{source}: invalid TOML: {exc}") from None
        except OSError as exc:
            raise ConfigError(f"{source}: cannot read: {exc}") from None

        unknown = set(data) - set(_DEFAULTS)
        if unknown:
            raise ConfigError(
                f"{source}: unknown section(s): {', '.join(sorted(unknown))}. "
                f"Known sections: {', '.join(sorted(_DEFAULTS))}."
            )
        for section, values in data.items():
            if not isinstance(values, dict):
                raise ConfigError(f"{source}: [{section}] must be a table")
            unknown_keys = set(values) - set(_DEFAULTS[section])
            if unknown_keys:
                raise ConfigError(
                    f"{source}: unknown key(s) in [{section}]: "
                    f"{', '.join(sorted(unknown_keys))}"
                )

    def resolve(section: str, key: str, override_key: str) -> Any:
        dotted = f"{section}.{key}"
        if override_key in overrides:
            origins[dotted] = "command line"
            return overrides[override_key]
        if section in data and key in data[section]:
            origins[dotted] = f"config file"
            return data[section][key]
        origins[dotted] = "default"
        return _DEFAULTS[section][key]

    on_duplicate = resolve("capture", "on_duplicate", "on_duplicate")
    if on_duplicate not in _VALID_ON_DUPLICATE:
        raise ConfigError(
            f"capture.on_duplicate must be one of "
            f"{', '.join(_VALID_ON_DUPLICATE)}; got {on_duplicate!r}"
        )

    return Config(
        store_path=Path(resolve("store", "path", "store_path")).expanduser(),
        runs_directory=Path(resolve("runs", "directory", "runs_directory")).expanduser(),
        on_duplicate=on_duplicate,
        source_file=source,
        origins=origins,
    )
