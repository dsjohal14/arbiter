"""Versioned, local records of plan-review and verification runs.

Records are opt-in because they contain the plan, diff, and provider responses.
They deliberately exclude subprocess commands, environment variables, and
session identifiers. The format is the future replay API, so every document
carries an explicit schema version from its first release.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .models import ModelResponse, Session

SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def recorded_input(content: str, source: str | None = None) -> dict:
    """Embed one input with a stable digest for corpus comparisons."""
    return {
        "source": source,
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "content": content,
    }


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
                "usage": None,
            }
        )

    def failure(self, phase: str, session: Session, error: Exception) -> None:
        self.calls.append(
            {
                "phase": phase,
                "role": session.key,
                "provider": session.provider.key,
                "cli": session.cli,
                "model": session.model,
                "status": "failed",
                "duration_ms": None,
                "content": None,
                "error": {"type": type(error).__name__, "message": str(error)},
                "usage": None,
            }
        )


@dataclass
class RunRecorder:
    """Build and atomically persist one schema-v1 run document."""

    path: Path
    command: str
    inputs: dict
    configuration: dict
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
