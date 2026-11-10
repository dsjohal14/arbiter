"""Versioned, local records of plan-review and verification runs.

Records are opt-in because they contain the plan, diff, and provider responses.
They deliberately exclude subprocess commands, environment variables, and
session identifiers. The format is the replay API — `replay.py` re-derives a
verdict from nothing but one of these documents — so every record carries an
explicit schema version, and a reader refuses a version it does not know.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .models import ModelResponse, Session

# v2 added `workspace`. v3 makes that snapshot cover the whole Git-visible
# tree (or the whole non-Git directory) and separates replay observations from
# the result they are used to reproduce. Older records remain replayable, but
# cannot support an exact replay check or be bound to a verify eval case.
#
# v4 states what running each cited test produced (`outcome`) on its evidence
# check, and the downgrade rules key on that instead of on the check's prose.
# The document shape is otherwise v3's. The bump exists for older readers: one
# that applied prose rules to a v4 record would reach a different status for an
# errored or skipped test, so it has to refuse the record rather than guess.
SCHEMA_VERSION = 4

MAX_WORKSPACE_FILES = 100_000
MAX_WORKSPACE_FILE_BYTES = 512 * 1024 * 1024
MAX_WORKSPACE_BYTES = 2 * 1024 * 1024 * 1024

# A non-Git fixture has no .gitignore to keep deterministic runner output out
# of its identity. These paths are tooling state, not inputs to evidence. Git
# scope continues to let the repository's own ignore rules make that decision.
_TREE_ARTIFACT_DIRS = frozenset({".pytest_cache", "__pycache__", ".hypothesis"})
_TREE_ARTIFACT_FILES = frozenset({".coverage"})


class WorkspaceIdentityError(ValueError):
    """A workspace cannot be represented safely and completely."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _git(args: list[str], root: Path) -> str | None:
    """Best-effort git, for provenance only. A `None` here means "unknown"; do
    not use it to decide the scope, which `_inside_work_tree` settles instead."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, check=False, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _has_git_directory(root: Path) -> bool:
    """Whether anything up the ancestry claims to be a repository.

    `.git` is a directory in a normal checkout and a file in a linked worktree
    or submodule, so existence is the test. This is only consulted when git
    itself cannot answer.
    """
    return any((parent / ".git").exists() for parent in (root, *root.parents))


def _inside_work_tree(root: Path) -> bool:
    """Whether root is inside a Git worktree — or an error if git cannot say.

    A failed invocation is not the same answer as `false`. Treating "git is
    missing" as "not a repository" would silently enumerate the whole directory
    instead of the tracked tree: an identity that ignores `.gitignore`, sweeps
    in `venv/` and `node_modules/`, and can never be scored against a record
    made where git did work. So the only fallback is the one case that needs no
    git to decide — nothing up the ancestry claims to be a repository at all.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except FileNotFoundError as e:
        if _has_git_directory(root):
            raise WorkspaceIdentityError(
                f"{root} is inside a Git repository but git is not installed or not on "
                "PATH, so its tracked tree cannot be enumerated"
            ) from e
        return False
    except (OSError, subprocess.SubprocessError) as e:
        raise WorkspaceIdentityError(f"could not ask git about {root}: {e}") from e

    if proc.returncode == 0:
        return proc.stdout.strip() == "true"
    detail = proc.stderr.strip()
    if "not a git repository" in detail.lower():
        return False
    raise WorkspaceIdentityError(f"could not ask git about {root}{f': {detail}' if detail else ''}")


def _git_workspace_paths(root: Path) -> list[str] | None:
    """Git-visible files below root, relative to root; None outside a worktree."""
    if not _inside_work_tree(root):
        return None
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "."],
            cwd=root,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise WorkspaceIdentityError(f"could not enumerate the Git workspace: {e}") from e
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise WorkspaceIdentityError(
            f"could not enumerate the Git workspace{f': {detail}' if detail else ''}"
        )
    return [os.fsdecode(part) for part in proc.stdout.split(b"\0") if part]


def _directory_workspace_paths(root: Path) -> list[str]:
    """Stable fixture inputs, excluding VCS metadata and test-run artifacts."""
    paths: list[str] = []

    def unreadable(error: OSError) -> None:
        raise WorkspaceIdentityError(f"could not enumerate workspace {root}: {error}") from error

    try:
        for directory, names, filenames in os.walk(root, followlinks=False, onerror=unreadable):
            names[:] = sorted(
                name for name in names if name != ".git" and name not in _TREE_ARTIFACT_DIRS
            )
            base = Path(directory)
            linked_directories = [name for name in names if (base / name).is_symlink()]
            names[:] = [name for name in names if name not in linked_directories]
            paths.extend(os.fspath((base / name).relative_to(root)) for name in linked_directories)
            paths.extend(
                os.fspath((base / name).relative_to(root))
                for name in sorted(filenames)
                if name not in _TREE_ARTIFACT_FILES and not name.startswith(".coverage.")
            )
    except OSError as e:
        raise WorkspaceIdentityError(f"could not enumerate workspace {root}: {e}") from e
    return paths


def _safe_workspace_file(raw: str, root: Path) -> tuple[str, Path] | None:
    """Resolve one regular file below root; absent/special files are skipped."""
    rel = Path(raw)
    if rel.is_absolute():
        raise WorkspaceIdentityError(f"workspace path is absolute: {raw}")
    try:
        candidate = (root / rel).resolve()
        candidate.relative_to(root)
    except (OSError, ValueError) as e:
        raise WorkspaceIdentityError(f"workspace path escapes its root: {raw}") from e
    try:
        mode = candidate.stat().st_mode
    except FileNotFoundError:
        return None
    except OSError as e:
        raise WorkspaceIdentityError(f"could not inspect workspace path {raw}: {e}") from e
    if not stat.S_ISREG(mode):
        return None
    return rel.as_posix(), candidate


def _file_digest(path: Path, rel: str, remaining: int) -> tuple[str, int]:
    """Hash raw bytes incrementally, refusing an unbounded snapshot."""
    try:
        size = path.stat().st_size
    except OSError as e:
        raise WorkspaceIdentityError(f"could not inspect workspace file {rel}: {e}") from e
    if size > MAX_WORKSPACE_FILE_BYTES:
        raise WorkspaceIdentityError(
            f"workspace file is too large to record safely: {rel} ({size} bytes)"
        )
    if size > remaining:
        raise WorkspaceIdentityError(
            f"workspace exceeds the {MAX_WORKSPACE_BYTES}-byte snapshot limit at {rel}"
        )

    hashed = hashlib.sha256()
    read = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                read += len(chunk)
                if read > MAX_WORKSPACE_FILE_BYTES or read > remaining:
                    raise WorkspaceIdentityError(f"workspace file grew past its limit: {rel}")
                hashed.update(chunk)
    except OSError as e:
        raise WorkspaceIdentityError(f"could not read workspace file {rel}: {e}") from e
    return hashed.hexdigest(), read


def workspace_identity(
    paths: list[str] | None = None,
    root: Path | None = None,
    *,
    exclude: list[Path] | None = None,
) -> dict:
    """Identify every repository file that may affect an evidence check.

    A Git worktree includes tracked and non-ignored untracked files below
    ``root``. A corpus fixture, which is normally not a Git checkout, includes
    every stable regular file below its root while excluding standard test-run
    caches. ``paths`` remains available for diagnostics and older callers, but
    produces a selected snapshot that eval scoring refuses.

    Paths are containment-checked, special files are never read, raw bytes are
    streamed into SHA-256, and explicit limits turn oversized trees into an
    error before a paid review begins.
    """
    root = (root or Path.cwd()).resolve()
    if not root.is_dir():
        raise WorkspaceIdentityError(f"workspace root is not a directory: {root}")

    git_paths = None if paths is not None else _git_workspace_paths(root)
    scope = "selected" if paths is not None else "git-visible" if git_paths is not None else "tree"
    if paths is not None:
        candidates = paths
    elif git_paths is not None:
        candidates = git_paths
    else:
        candidates = _directory_workspace_paths(root)
    if len(candidates) > MAX_WORKSPACE_FILES:
        raise WorkspaceIdentityError(
            f"workspace has {len(candidates)} files; limit is {MAX_WORKSPACE_FILES}"
        )

    excluded: set[Path] = set()
    for item in exclude or []:
        try:
            candidate = (item if item.is_absolute() else root / item).resolve()
            candidate.relative_to(root)
        except (OSError, ValueError):
            continue
        excluded.add(candidate)

    files: dict[str, str] = {}
    total = 0
    for raw in sorted(set(candidates)):
        resolved = _safe_workspace_file(raw, root)
        if resolved is None:
            continue
        rel, path = resolved
        if path in excluded:
            continue
        value, consumed = _file_digest(path, rel, MAX_WORKSPACE_BYTES - total)
        files[rel] = value
        total += consumed
    status = _git(["status", "--porcelain"], root)
    return {
        "scope": scope,
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
            # These are replay inputs, not expected output. Keeping them apart
            # is what lets an exhaustive result comparison detect a changed
            # evidence citation/detail or blinding map even when status holds.
            "observations": _result_observations(result),
            "provider_calls": self.trace.calls,
            "result": result,
            "exit_code": exit_code,
            "error": error_value,
        }
        write_record(self.path, payload)
        return payload


_EVIDENCE_FIELDS = ("impl_check", "test_check", "baseline_check", "evidence_source")


def _result_observations(result: dict | None) -> dict | None:
    """Inputs learned during a run that deterministic replay cannot re-measure."""
    if result is None:
        return None
    criteria = []
    for row in result.get("criteria") or []:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        criteria.append(
            {"id": row["id"], **{name: copy.deepcopy(row.get(name)) for name in _EVIDENCE_FIELDS}}
        )
    return {"blinding": copy.deepcopy(result.get("blinding")), "criteria_evidence": criteria}


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
