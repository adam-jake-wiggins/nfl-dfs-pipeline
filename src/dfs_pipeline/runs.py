"""Run directories: one self-contained, auditable record per invocation.

Every run writes its own directory containing the resolved configuration, the
SHA-256 of every input file, timestamps, package version, and the outcome.
Months later this answers "what exactly did the September 13 capture do, with
which file, under what settings" without anyone reconstructing it from memory.

Crucially the directory is written **even when the run fails**, with the
failure recorded. A run that leaves no trace when it breaks is a run you
cannot debug on a Sunday morning.
"""

from __future__ import annotations

import json
import logging
import platform
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dfs_pipeline import __version__

__all__ = ["RunRecord", "RunDirectory"]

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


@dataclass
class RunRecord:
    """The metadata written to ``run.json``."""

    run_id: str
    command: str
    started_at: str
    package_version: str = __version__
    python_version: str = field(default_factory=lambda: sys.version.split()[0])
    platform: str = field(default_factory=platform.platform)

    # Filled in as the run proceeds.
    config: dict[str, Any] = field(default_factory=dict)
    inputs: list[dict[str, Any]] = field(default_factory=list)
    results: dict[str, Any] = field(default_factory=dict)

    finished_at: str | None = None
    outcome: str = "incomplete"
    error: str | None = None

    #: No randomness exists anywhere in the capture path, so there is no seed
    #: to record. Stated explicitly rather than omitted: a reader should be
    #: able to confirm the run was deterministic, not merely assume it. When
    #: the optimizer lands -- solver tie-breaking is the first real source of
    #: nondeterminism -- this becomes a recorded seed.
    randomness: str = "none"

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2, sort_keys=False) + "\n"


class RunDirectory:
    """Context manager owning one run's directory, log file, and metadata.

    Used as::

        with RunDirectory(root, command="snapshot") as run:
            run.record_input(path, sha256, bytes_len)
            ...
            run.results["observations"] = n

    On a clean exit the outcome is ``success``; on an exception it is
    ``failed`` with the error recorded, and the exception still propagates.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        command: str,
        now: datetime | None = None,
        console_level: int = logging.WARNING,
    ) -> None:
        stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
        self.root = Path(root)
        self.path = self.root / f"{stamp}-{command}"
        self.command = command
        self.record = RunRecord(
            run_id=self.path.name,
            command=command,
            started_at=(now or datetime.now(timezone.utc)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        )
        self._console_level = console_level
        self._file_handler: logging.FileHandler | None = None
        self._console_handler: logging.StreamHandler | None = None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> RunDirectory:
        self._claim_unique_directory()
        self._install_logging()
        logging.getLogger("dfs_pipeline.run").info("run %s started", self.record.run_id)
        return self

    def __exit__(self, exc_type, exc, _tb) -> bool:
        self.record.finished_at = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        if exc is None:
            self.record.outcome = "success"
        else:
            self.record.outcome = "failed"
            self.record.error = f"{type(exc).__name__}: {exc}"
            logging.getLogger("dfs_pipeline.run").error(
                "run failed: %s", self.record.error
            )

        self.write_metadata()
        self._remove_logging()
        return False  # never swallow the exception

    def _claim_unique_directory(self, limit: int = 1000) -> None:
        """Create this run's directory, never reusing an existing one.

        Run identifiers are timestamped to the second, so two runs started
        within the same second would otherwise share a directory and the
        second would overwrite the first's metadata -- silently destroying the
        audit trail this class exists to provide. Re-running a failed capture
        immediately is a completely ordinary thing to do, so this is a
        realistic collision, not a theoretical one.

        ``mkdir(exist_ok=False)`` is atomic, so claiming the name by creating
        it is race-free in a way that checking-then-creating is not.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        base = self.path
        for suffix in range(1, limit + 1):
            candidate = base if suffix == 1 else base.with_name(f"{base.name}-{suffix}")
            try:
                candidate.mkdir(exist_ok=False)
            except FileExistsError:
                continue
            self.path = candidate
            self.record.run_id = candidate.name
            return
        raise OSError(
            f"could not create a unique run directory under {self.root} "
            f"after {limit} attempts"
        )

    def _install_logging(self) -> None:
        """Quiet console, verbose file.

        The operator sees only what matters; the run log keeps everything, so
        a failure three weeks later is diagnosable without having thought to
        pass -v at the time.
        """
        root = logging.getLogger("dfs_pipeline")
        root.setLevel(logging.DEBUG)

        self._file_handler = logging.FileHandler(self.path / "run.log", encoding="utf-8")
        self._file_handler.setLevel(logging.DEBUG)
        self._file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(self._file_handler)

        self._console_handler = logging.StreamHandler(sys.stderr)
        self._console_handler.setLevel(self._console_level)
        self._console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        root.addHandler(self._console_handler)

    def _remove_logging(self) -> None:
        root = logging.getLogger("dfs_pipeline")
        for handler in (self._file_handler, self._console_handler):
            if handler is not None:
                handler.close()
                root.removeHandler(handler)

    # -- recording ---------------------------------------------------------

    def record_input(
        self,
        path: str | Path,
        *,
        sha256: str,
        byte_size: int,
        kind: str,
    ) -> None:
        """Record an input file and its digest.

        The digest is what makes the claim "this is exactly the file we used"
        checkable rather than asserted.
        """
        self.record.inputs.append(
            {
                "kind": kind,
                "path": str(Path(path).resolve()),
                "filename": Path(path).name,
                "sha256": sha256,
                "byte_size": byte_size,
            }
        )

    @property
    def results(self) -> dict[str, Any]:
        return self.record.results

    def write_metadata(self) -> None:
        (self.path / "run.json").write_text(self.record.to_json(), encoding="utf-8")
