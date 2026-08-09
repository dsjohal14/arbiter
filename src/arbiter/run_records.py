"""Versioned, local records of plan-review and verification runs.

Records are opt-in because they contain the plan, diff, and provider responses.
They deliberately exclude subprocess commands, environment variables, and
session identifiers. The format is the replay API — `replay.py` re-derives a
verdict from nothing but one of these documents — so every record carries an
explicit schema version, and a reader refuses a version it does not know.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .models import ModelResponse, Session

# v2 added `workspace`: which tree the evidence checks actually read. A v1
# record is still readable, but cannot be bound to an eval case's fixture repo.
SCHEMA_VERSION = 2


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _git(args: list[str], root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, check=False, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def workspace_identity(paths: list[str], root: Path | None = None) -> dict:
    """Identify the tree an evidence check read, by digesting what it read.

    Digests exactly the files in scope rather than the whole checkout: it is
    bounded, and those are the files whose content decided the evidence result.
    Two runs of the same plan and diff against different working trees produce
    different identities here, which is what stops a result being scored against
    an eval case whose fixture repo it never ran in.

    The commit and dirty flag are provenance for a human reading the record;
    only `files` is compared, because a corpus fixture is not a git checkout.
    """
    root = (root or Path.cwd()).resolve()
    files: dict[str, str] = {}
    for rel in sorted(paths):
        try:
            files[rel] = digest((root / rel).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue  # absent here; a differing key set is itself the mismatch
    status = _git(["status", "--porcelain"], root)
    return {
        "commit": _git(["rev-parse", "HEAD"], root),
        "dirty": None if status is None else bool(status),
        "files": files,
    }


def digest(content: str) -> str:
    """The digest used to tell whether two runs saw the same input."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def recorded_input(content: str, source: str | None = None) -> dict:
    """Embed one input with a stable digest for corpus comparisons."""
    return {"source": source, "sha256": digest(content), "content": content}


@dataclass
class RunTrace:
    """Provider outcomes accumulated while a command is running."""

    calls: list[dict] = field(default_factory=list)

    def success(self, phase: str, session: Session, response: ModelResponse) -> None:
        self.calls.append(
            {
                "phase": phase,
                "role": session.key,
                "provider": session.provider.key,
                "cli": session.cli,
                "model": session.model,
                "status": "completed",
                "duration_ms": round(response.duration * 1000),
                "content": response.content,
                "error": None,
                # None where the provider reports nothing we can read, which is
                # not the same as a call that consumed nothing.
                "usage": response.usage.as_dict() if response.usage else None,
            }
        )

    def failure(self, phase: str, session: Session, error: Exception) -> None:
        # A failed call still spent wall-clock, and a provider commonly reports
        # usage in the same event that reports the error. Recording null for
        # both would bias every later cost and latency summary downward by
        # exactly the runs that went wrong. `getattr` because not every failure
        # reaching here is a ModelCallError.
        duration = getattr(error, "duration", None)
        usage = getattr(error, "usage", None)
        self.calls.append(
            {
                "phase": phase,
                "role": session.key,
                "provider": session.provider.key,
                "cli": session.cli,
                "model": session.model,
                "status": "failed",
                "duration_ms": round(duration * 1000) if duration is not None else None,
                "content": None,
                "error": {"type": type(error).__name__, "message": str(error)},
                "usage": usage.as_dict() if usage is not None else None,
            }
        )


@dataclass
class RunRecorder:
    """Build and atomically persist one versioned run document."""

    path: Path
    command: str
    inputs: dict
    configuration: dict
    # Which tree the evidence checks read. None for a command that checks none.
    workspace: dict | None = None
    trace: RunTrace = field(default_factory=RunTrace)
    started_at: str = field(default_factory=_utc_now)
    _started_monotonic: float = field(default_factory=time.monotonic, repr=False)

    def finish(
        self,
        *,
        exit_code: int,
        result: dict | None,
        error: Exception | str | None = None,
    ) -> dict:
        finished_at = _utc_now()
        error_value: dict | None = None
        if error is not None:
            error_value = {
                "type": type(error).__name__ if isinstance(error, Exception) else "RuntimeError",
                "message": str(error),
            }

        payload = {
            "schema_version": SCHEMA_VERSION,
            "sensitive": True,
            "command": self.command,
            "status": "error" if error_value else "completed",
            "started_at": self.started_at,
            "finished_at": finished_at,
            "duration_ms": round((time.monotonic() - self._started_monotonic) * 1000),
            "arbiter_version": __version__,
            "inputs": self.inputs,
            "configuration": self.configuration,
            "workspace": self.workspace,
            "provider_calls": self.trace.calls,
            "result": result,
            "exit_code": exit_code,
            "error": error_value,
        }
        write_record(self.path, payload)
        return payload


def write_record(path: Path, payload: dict) -> None:
    """Write JSON by atomic replacement, with owner-only file permissions."""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with suppress(OSError):
            os.close(fd)
        Path(temporary).unlink(missing_ok=True)
        raise
