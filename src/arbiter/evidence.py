"""Mechanical checking of cited evidence.

A reviewer saying "src/arbiter/cli.py:84" and "verified by tests/test_cli.py::
test_unknown_model" is making two claims. Both are checkable, and checking them
is what separates this from a tool that reports opinions:

  - the file:line either exists or it does not, and we can print what is actually
    on that line next to the claim, so a fabricated citation dies immediately
  - the test either exists and passes, or it does not

Everything here is deliberately dumb and deterministic. No model is consulted.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# "src/app.py:84", "src/app.py:84-91", "./app.py line 84"
_FILE_CITATION_RE = re.compile(
    r"(?P<path>[\w./\-]+\.\w+)\s*(?::|\s+lines?\s+)\s*(?P<start>\d+)(?:\s*[-–]\s*(?P<end>\d+))?",  # noqa: RUF001 - models write en dashes in ranges
)
# "tests/test_cli.py::test_unknown_model", "tests/test_cli.py::TestX::test_y"
_TEST_CITATION_RE = re.compile(r"(?P<path>[\w./\-]+\.py)::(?P<node>[\w:\[\]\-.]+)")

TEST_TIMEOUT = 120.0

# Running a cited test executes code from the branch under review. Only these
# variables are inherited directly by the child. This reduces accidental secret
# exposure; it is NOT a security boundary. The test still runs as the invoking
# user with that user's filesystem and network access, so untrusted code must
# use --no-run-tests or an external sandbox.
_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TEMP",
    "TMP",
    "PYTHONPATH",
    "PYTHONHASHSEED",
    "VIRTUAL_ENV",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
)


def scrubbed_env() -> dict[str, str]:
    """Return a minimally inherited environment, not a sandbox."""
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    # Let a test detect that it is being run as evidence, if it cares.
    env["ARBITER_EVIDENCE_CHECK"] = "1"
    # Keep pytest from picking up ambient config that could re-widen the env.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def test_citation_path(citation: str) -> str | None:
    """Return the repository-relative path from a pytest node citation."""
    m = _TEST_CITATION_RE.search(citation or "")
    return m.group("path") if m else None


def _test_ast_node(citation: str, root: Path) -> tuple[bool, ast.AST | None]:
    """Return (readable, node) for a pytest function/class citation.

    `node is None` with `readable=True` means the test did not exist in that
    tree. Parse/read failures stay distinct so callers can fail closed.
    """
    m = _TEST_CITATION_RE.search(citation or "")
    if not m:
        return False, None
    path = _safe_path(m.group("path"), root)
    if path is None or not path.is_file():
        return True, None
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return False, None

    names = m.group("node").split("::")
    names[-1] = names[-1].split("[", 1)[0]
    body = tree.body
    found: ast.AST | None = None
    for name in names:
        found = next(
            (
                item
                for item in body
                if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == name
            ),
            None,
        )
        if found is None:
            return True, None
        body = found.body if isinstance(found, ast.ClassDef) else []
    return True, found


def test_node_changed(citation: str, head: Path, base: Path) -> bool | None:
    """Whether the selected pytest node itself changed from base to HEAD.

    A node absent at base is newly written evidence, not a rewrite of evidence
    the plan author previously approved. `None` means comparison was impossible
    and callers must preserve the conservative changed-file classification.
    """
    head_ok, head_node = _test_ast_node(citation, head)
    base_ok, base_node = _test_ast_node(citation, base)
    if not head_ok or not base_ok or head_node is None:
        return None
    if base_node is None:
        return False
    return ast.dump(head_node, include_attributes=False) != ast.dump(
        base_node, include_attributes=False
    )


@dataclass
class EvidenceCheck:
    """The result of checking one citation. `verified is None` means we could not
    check it at all, which is different from checking it and finding it false."""

    kind: str  # "file" | "test"
    citation: str
    verified: bool | None
    detail: str

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "citation": self.citation,
            "verified": self.verified,
            "detail": self.detail,
        }


def _safe_path(raw: str, root: Path) -> Path | None:
    """Resolve a cited path inside the repo, refusing anything that escapes it."""
    try:
        candidate = (root / raw).resolve()
        candidate.relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    return candidate


def check_file_citation(citation: str, root: Path | None = None) -> EvidenceCheck:
    """Confirm a cited file:line exists, and quote what is actually there."""
    root = root or Path.cwd()
    m = _FILE_CITATION_RE.search(citation or "")
    if not m:
        return EvidenceCheck("file", citation, None, "no file:line citation found")

    rel, start = m.group("path"), int(m.group("start"))
    end = int(m.group("end") or start)
    path = _safe_path(rel, root)
    if path is None:
        return EvidenceCheck("file", citation, False, f"{rel} escapes the repository root")
    if not path.is_file():
        return EvidenceCheck("file", citation, False, f"{rel} does not exist")

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return EvidenceCheck("file", citation, None, f"could not read {rel}: {e}")

    if start < 1 or start > len(lines):
        return EvidenceCheck(
            "file", citation, False, f"{rel} has {len(lines)} lines; line {start} does not exist"
        )

    end = min(end, len(lines), start + 4)  # quote at most 5 lines
    quoted = "\n".join(lines[start - 1 : end]).strip()
    return EvidenceCheck("file", citation, True, quoted or "(blank line)")


# ── Tests ─────────────────────────────────────────────────────────────────────


def detect_test_command(root: Path | None = None) -> list[str] | None:
    """Only pytest is auto-detected. Anything else must be passed explicitly with
    --test-command: guessing a project's runner wrong is worse than admitting we
    do not know.

    Falls back to `python -m pytest`, because pytest is frequently importable in
    an environment without being on PATH — a venv whose scripts were never
    rewritten, for instance.
    """
    root = root or Path.cwd()
    markers = ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "conftest.py")
    if not (any((root / m).exists() for m in markers) or (root / "tests").is_dir()):
        return None
    if shutil.which("pytest"):
        return ["pytest"]
    if importlib.util.find_spec("pytest") is not None:
        return [sys.executable, "-m", "pytest"]
    return None


def check_test_citation(
    citation: str,
    root: Path | None = None,
    test_command: list[str] | None = None,
    run: bool = True,
    scrub_env: bool = True,
) -> EvidenceCheck:
    """Confirm a cited test exists and, when permitted, that it passes.

    Executing the test runs code from the branch under review. With `scrub_env`
    the child gets a minimal environment so secrets are not reachable; it is
    still not a sandbox, and untrusted input should use `run=False`.
    """
    root = root or Path.cwd()
    m = _TEST_CITATION_RE.search(citation or "")
    if not m:
        return EvidenceCheck("test", citation, None, "no test node id found")

    node = m.group(0)
    path = _safe_path(m.group("path"), root)
    if path is None:
        return EvidenceCheck("test", node, False, f"{m.group('path')} escapes the repository root")
    if not path.is_file():
        return EvidenceCheck("test", node, False, f"{m.group('path')} does not exist")

    if not run:
        return EvidenceCheck("test", node, None, "test file exists; not executed (--no-run-tests)")

    cmd = test_command or detect_test_command(root)
    if not cmd:
        return EvidenceCheck("test", node, None, "no test runner detected; not executed")

    try:
        proc = subprocess.run(
            [*cmd, node, "-q", "--no-header"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT,
            check=False,
            env=scrubbed_env() if scrub_env else None,
        )
    except subprocess.TimeoutExpired:
        return EvidenceCheck("test", node, None, f"timed out after {TEST_TIMEOUT:.0f}s")
    except OSError as e:
        return EvidenceCheck("test", node, None, f"could not run tests: {e}")

    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode == 0:
        return EvidenceCheck("test", node, True, "passed")
    # pytest exits 4 for collection errors — the test does not exist under that id.
    if proc.returncode == 4 or "no tests ran" in out.lower() or "not found" in out.lower():
        return EvidenceCheck("test", node, False, "no such test")
    return EvidenceCheck("test", node, False, f"failed\n{_tail(out)}")


def _tail(text: str, lines: int = 6) -> str:
    kept = [ln for ln in text.splitlines() if ln.strip()][-lines:]
    return "\n".join(kept)


# ── Baseline discrimination ──────────────────────────────────────────────────


@contextlib.contextmanager
def base_worktree(root: Path, ref: str) -> Iterator[Path | None]:
    """Check out `ref` into a throwaway worktree, leaving the user's tree alone.

    Yields None if the worktree cannot be created — a missing ref, a shallow
    clone, no git. Callers must treat that as "could not check", never as a
    failure of the change under review.
    """
    tmp = tempfile.mkdtemp(prefix="arbiter-base-")
    created = False
    try:
        proc = subprocess.run(
            ["git", "worktree", "add", "--detach", tmp, ref],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        created = proc.returncode == 0
        yield Path(tmp) if created else None
    except OSError:
        yield None
    finally:
        if created:
            subprocess.run(
                ["git", "worktree", "remove", "--force", tmp],
                cwd=root,
                capture_output=True,
                check=False,
            )
        shutil.rmtree(tmp, ignore_errors=True)


_BASELINE_SITECUSTOMIZE = """\
import os
import sys
from pathlib import Path


def resolved(value):
    try:
        return str(Path(value or ".").resolve())
    except OSError:
        return value


current = resolved(os.environ["ARBITER_CURRENT_ROOT"])
base = Path(os.environ["ARBITER_BASE_ROOT"]).resolve()
blocked = {current, str(Path(current) / "src"), str(Path(current) / "lib")}
sys.path[:] = [entry for entry in sys.path if resolved(entry) not in blocked]

# PEP 660 editable installs can use a meta-path finder rather than a plain .pth
# entry. Drop only finders whose mapping points back into the active checkout.
kept = []
for finder in sys.meta_path:
    module = sys.modules.get(getattr(finder, "__module__", ""))
    mapping = getattr(module, "MAPPING", {}) if module else {}
    if any(resolved(path) == current or resolved(path).startswith(current + os.sep) for path in mapping.values()):
        continue
    kept.append(finder)
sys.meta_path[:] = kept

candidates = [base / "src", base / "lib", base]
for candidate in reversed([path for path in candidates if path.exists()]):
    text = str(candidate)
    if text not in sys.path:
        sys.path.insert(0, text)
"""


def _baseline_env(root: Path, base: Path, scrub_env: bool) -> dict[str, str]:
    """Build an environment that imports project code from the base worktree.

    Reusing the caller's Python is intentional—it already has pytest and project
    dependencies—but its editable install must not make the base run import HEAD.
    A sitecustomize hook runs after .pth files and removes only paths/finders for
    the active checkout, leaving the interpreter's site-packages available.
    """
    env = scrubbed_env() if scrub_env else os.environ.copy()
    bootstrap = base / ".arbiter-pythonpath"
    bootstrap.mkdir(exist_ok=True)
    (bootstrap / "sitecustomize.py").write_text(_BASELINE_SITECUSTOMIZE, encoding="utf-8")

    blocked = {root.resolve(), (root / "src").resolve(), (root / "lib").resolve()}
    inherited = []
    for raw in env.get("PYTHONPATH", "").split(os.pathsep):
        if not raw:
            continue
        try:
            if Path(raw).resolve() in blocked:
                continue
        except OSError:
            pass
        inherited.append(raw)

    env["PYTHONPATH"] = os.pathsep.join([str(bootstrap), *inherited])
    env["PYTHONNOUSERSITE"] = "1"
    env["ARBITER_CURRENT_ROOT"] = str(root.resolve())
    env["ARBITER_BASE_ROOT"] = str(base.resolve())
    return env


def check_test_discriminates(
    citation: str,
    root: Path,
    base_ref: str,
    test_command: list[str] | None = None,
    scrub_env: bool = True,
    base_dir: Path | None = None,
) -> EvidenceCheck:
    """Does this test actually distinguish the change, or would it pass anyway?

    Runs the *current* version of the cited test against the *base* revision's
    code. A test that fails there and passes now is proof the change is what
    made it pass. A test that passes at base proves nothing about the change —
    it is either a pre-existing regression guard or vacuous (`assert True`
    passes everywhere), and those two are not distinguishable from the outside.

    This is why the check reports rather than punishes: `verified is False` means
    "does not discriminate", which is a legitimate state for an invariant.
    """
    m = _TEST_CITATION_RE.search(citation or "")
    if not m:
        return EvidenceCheck("baseline", citation, None, "no test node id found")

    node, rel = m.group(0), m.group("path")
    head_file = _safe_path(rel, root)
    if head_file is None or not head_file.is_file():
        return EvidenceCheck("baseline", node, None, "test file not readable at HEAD")

    with contextlib.ExitStack() as stack:
        # One checkout serves every criterion; creating one per test would make
        # this unusably slow on a real plan.
        base = base_dir or stack.enter_context(base_worktree(root, base_ref))
        if base is None:
            return EvidenceCheck("baseline", node, None, f"could not check out {base_ref}")

        # Carry the test itself back to the old code: we are asking what the
        # change did, not what the old test suite said.
        target = base / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(head_file, target)
        except OSError as e:
            return EvidenceCheck("baseline", node, None, f"could not stage the test: {e}")

        cmd = test_command or detect_test_command(root) or detect_test_command(base)
        if not cmd:
            return EvidenceCheck("baseline", node, None, "no test runner detected")

        try:
            env = _baseline_env(root, base, scrub_env)
            proc = subprocess.run(
                [*cmd, node, "-q", "--no-header"],
                cwd=base,
                capture_output=True,
                text=True,
                timeout=TEST_TIMEOUT,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return EvidenceCheck("baseline", node, None, "timed out against the base revision")
        except OSError as e:
            return EvidenceCheck("baseline", node, None, f"could not run against base: {e}")

    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode == 0:
        return EvidenceCheck(
            "baseline",
            node,
            False,
            f"passes against {base_ref} too, so it does not distinguish this change",
        )
    # Collection errors mean the test could not even load against the old code —
    # usually it imports something this change introduced. That is not evidence
    # either way.
    collection_error = proc.returncode == 4 or (
        "error" in out.lower()[:400] and "assert" not in out.lower()
    )
    if collection_error:
        return EvidenceCheck(
            "baseline", node, None, f"could not run against {base_ref} (likely new imports)"
        )
    return EvidenceCheck("baseline", node, True, f"fails against {base_ref}, passes here")
