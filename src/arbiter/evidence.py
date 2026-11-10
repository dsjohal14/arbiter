"""Mechanical checking of cited evidence.

A reviewer saying "src/arbiter/cli.py:84" and "verified by tests/test_cli.py::
test_unknown_model" is making two claims. Both are checkable, and checking them
is what separates this from a tool that reports opinions:

  - the file:line either exists or it does not, and we can print what is actually
    on that line next to the claim, so a fabricated citation dies immediately
  - the test either exists and passes, or it does not

Everything here is deliberately dumb and deterministic. No model is consulted.

What happened to a test is read from the runner's own structured report, never
inferred from an exit status or from words in its output. Both of those lie:
pytest exits 0 for a test that was skipped or that failed as an expected
failure, exits non-zero for a passing test when a coverage threshold trips, and
a failing assertion is free to print "not found".
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
from xml.etree import ElementTree

# "src/app.py:84", "src/app.py:84-91", "./app.py line 84"
_FILE_CITATION_RE = re.compile(
    r"(?P<path>[\w./\-]+\.\w+)\s*(?::|\s+lines?\s+)\s*(?P<start>\d+)(?:\s*[-–]\s*(?P<end>\d+))?",  # noqa: RUF001 - models write en dashes in ranges
)
# "tests/test_cli.py::test_unknown_model", "tests/test_cli.py::TestX::test_y"
_TEST_CITATION_RE = re.compile(r"(?P<path>[\w./\-]+\.py)::(?P<node>[\w:\[\]\-.]+)")

TEST_TIMEOUT = 120.0

# What running a cited test produced. The downgrade rules key on these rather
# than on the prose in `detail`, so "it failed", "it never ran", and "there is
# no such test" cannot be mistaken for one another by a substring.
PASSED = "passed"  # the test ran and held
FAILED = "failed"  # the test ran and did not hold — including an expected failure
ERRORED = "errored"  # no pass/fail result: setup, collection, timeout, launch
SKIPPED = "skipped"  # the test exists and was not run: a skip marker, deselection
MISSING = "missing"  # no such test
NOT_RUN = "not_run"  # Arbiter did not execute it: --no-run-tests, no runner
TEST_OUTCOMES = (PASSED, FAILED, ERRORED, SKIPPED, MISSING, NOT_RUN)

# `verified` stays tri-state. An error or a skip is "could not confirm", which
# is a different fact from "confirmed false".
_VERIFIED: dict[str, bool | None] = {
    PASSED: True,
    FAILED: False,
    MISSING: False,
    ERRORED: None,
    SKIPPED: None,
    NOT_RUN: None,
}

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


def node_id(citation: str) -> str | None:
    """The pytest node id inside a citation, without any prose around it."""
    m = _TEST_CITATION_RE.search(citation or "")
    return m.group(0) if m else None


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

    kind: str  # "file" | "test" | "baseline"
    citation: str
    verified: bool | None
    detail: str
    # For a test check, one of TEST_OUTCOMES: what running it produced. None on
    # a file or baseline check, and on a test check restored from a record
    # written before outcomes were stored.
    outcome: str | None = None

    def as_dict(self) -> dict:
        data = {
            "kind": self.kind,
            "citation": self.citation,
            "verified": self.verified,
            "detail": self.detail,
        }
        # Present only where it means something. A record that predates the
        # field replays to exactly the dict it stored, so an exhaustive
        # comparison needs no special case for it.
        if self.outcome is not None:
            data["outcome"] = self.outcome
        return data

    @classmethod
    def from_dict(cls, data: dict) -> EvidenceCheck:
        """Rebuild a check recorded by an earlier run.

        `verified` is tri-state, so anything that is not exactly True or False
        restores as None — "we could not check" — rather than a coerced boolean.
        """
        verified = data.get("verified")
        outcome = data.get("outcome")
        return cls(
            kind=str(data.get("kind") or ""),
            citation=str(data.get("citation") or ""),
            verified=verified if isinstance(verified, bool) else None,
            detail=str(data.get("detail") or ""),
            outcome=outcome if isinstance(outcome, str) and outcome in TEST_OUTCOMES else None,
        )


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


@dataclass(frozen=True)
class RunReport:
    """What one run of a cited test produced.

    `detail` opens with a one-line statement of the outcome; any lines after it
    are the runner's own account of why.
    """

    outcome: str
    detail: str


# pytest's documented exit statuses. Only consulted when a run leaves no
# structured report behind.
_EXIT_INTERRUPTED = 2
_EXIT_INTERNAL_ERROR = 3
_EXIT_USAGE = 4
_EXIT_NO_TESTS = 5

# An expected failure is reported by pytest as a kind of skip. It is carried
# separately until the wording is settled, then reported as FAILED.
_XFAILED = "xfailed"

_DETAIL_LINES = 5
_DETAIL_WIDTH = 240

_SUMMARY_RE = re.compile(r"\b(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b")


def _excerpt(text: str) -> str:
    """The few lines of runner output that say why.

    pytest marks the lines that carry the actual error with a leading `E`; when
    it has, those are the explanation and the surrounding traceback is not.
    Otherwise keep the end of the text, which is where a failure is summarised.
    """
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    marked = [f"E   {line[1:].strip()}" for line in lines if line.startswith("E ")]
    kept = marked[:_DETAIL_LINES] if marked else lines[-_DETAIL_LINES:]
    return "\n".join(line[:_DETAIL_WIDTH] for line in kept)


def _lead(headline: str, text: str = "") -> str:
    excerpt = _excerpt(text)
    return f"{headline}\n{excerpt}" if excerpt else headline


def _cause(detail: str) -> str:
    """The single line of a report's excerpt that best names what went wrong."""
    lines = detail.splitlines()[1:]
    if not lines:
        return ""
    line = lines[0] if lines[0].startswith("E ") else lines[-1]
    return re.sub(r"^E\s+", "", line.strip())


def _read_report(path: Path) -> list[tuple[str, str, str]] | None:
    """`(kind, test name, runner text)` for each test in a JUnit XML report.

    None means there is no report to read: the command is not pytest, it ran
    somewhere this path does not reach (a container), or it died before writing
    one. An empty list is a different fact — the runner ran and selected nothing.
    """
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError):
        return None

    cases: list[tuple[str, str, str]] = []
    for case in root.iter("testcase"):
        name = case.get("name") or ""
        failure, error, skipped = case.find("failure"), case.find("error"), case.find("skipped")
        if failure is not None:
            cases.append((FAILED, name, failure.text or failure.get("message") or ""))
        elif error is not None:
            # Setup, teardown, and collection failures. The test did not get as
            # far as passing or failing.
            cases.append((ERRORED, name, error.text or error.get("message") or ""))
        elif skipped is not None:
            kind = _XFAILED if "xfail" in (skipped.get("type") or "") else SKIPPED
            cases.append((kind, name, skipped.get("message") or ""))
        else:
            cases.append((PASSED, name, ""))
    return cases


def _from_cases(cases: list[tuple[str, str, str]]) -> RunReport:
    """One outcome for a citation that may cover several tests.

    A class or a parametrized function is one citation and many cases. The
    proof holds only if every case that ran held, so the worst case decides —
    and a skipped case never counts toward a pass.
    """
    by_kind: dict[str, list[tuple[str, str]]] = {}
    for kind, name, text in cases:
        by_kind.setdefault(kind, []).append((name, text))

    def which(name: str) -> str:
        return f" ({name})" if len(cases) > 1 and name else ""

    if FAILED in by_kind:
        name, text = by_kind[FAILED][0]
        return RunReport(FAILED, _lead(f"failed{which(name)}", text))
    if _XFAILED in by_kind:
        # The runner exits 0 for this. It is still a test whose assertion does
        # not hold, which is exactly what a failing proof is.
        name, text = by_kind[_XFAILED][0]
        return RunReport(
            FAILED,
            _lead(
                f"failed{which(name)}: marked as an expected failure (xfail), and it does fail",
                text,
            ),
        )
    if ERRORED in by_kind:
        name, text = by_kind[ERRORED][0]
        return RunReport(ERRORED, _lead(f"errored before it could pass or fail{which(name)}", text))
    if PASSED in by_kind:
        skipped = len(by_kind.get(SKIPPED, []))
        note = f" ({skipped} of {len(cases)} cases skipped)" if skipped else ""
        return RunReport(PASSED, f"passed{note}")

    reason = by_kind[SKIPPED][0][1].strip().splitlines()[:1]
    return RunReport(
        SKIPPED,
        "skipped, so it proves nothing" + (f": {reason[0][:_DETAIL_WIDTH]}" if reason else ""),
    )


def _nothing_ran(out: str) -> RunReport:
    """The runner started and ran no test for this citation."""
    if re.search(r"\b\d+ deselected\b", out):
        return RunReport(
            SKIPPED, "deselected by the project's own test configuration, so it did not run"
        )
    return RunReport(MISSING, "no such test")


def _summary_counts(out: str) -> dict[str, int]:
    """Counts from pytest's closing summary line, when it printed one."""
    for line in reversed(out.splitlines()):
        found = _SUMMARY_RE.findall(line)
        if found and " in " in line:
            return {("error" if name.startswith("error") else name): int(n) for n, name in found}
    return {}


def _from_exit_status(code: int, out: str) -> RunReport:
    """Classify a run that left no structured report behind.

    This is the weak path, taken only for a command that is not pytest or that
    runs where the report cannot be read back. Output text is used here solely
    to LOWER a result: a successful exit is overruled by a summary showing that
    nothing passed, but a failing exit is never talked back up by anything the
    output says, because a test can print whatever it likes.
    """
    if code == 0:
        counts = _summary_counts(out)
        if counts and not (counts.get("passed") or counts.get("xpassed")):
            if counts.get("xfailed"):
                return RunReport(
                    FAILED, "failed: marked as an expected failure (xfail), and it does fail"
                )
            if counts.get("skipped") or counts.get("deselected"):
                return RunReport(SKIPPED, "skipped, so it proves nothing")
        return RunReport(PASSED, "passed")
    if code == _EXIT_NO_TESTS:
        return _nothing_ran(out)
    if code == _EXIT_USAGE:
        # pytest's own words for a node id that matches nothing. Any other
        # usage error — an unreadable module, an option it does not know — is a
        # run that broke, not a test that is absent.
        if "not found:" in out or "no match in any of" in out:
            return RunReport(MISSING, "no such test")
        return RunReport(ERRORED, _lead("errored before it could pass or fail", out))
    if code in (_EXIT_INTERRUPTED, _EXIT_INTERNAL_ERROR):
        return RunReport(ERRORED, _lead("errored before it could pass or fail", out))
    return RunReport(FAILED, _lead("failed", out))


def run_test(
    cmd: list[str],
    node: str,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float = TEST_TIMEOUT,
) -> RunReport:
    """Run one cited test and say what happened to it.

    The runner is asked to write a JUnit XML report, and the outcome is read
    from that. Exit status and terminal output are both unreliable narrators of
    a single test: a skip and an expected failure exit 0, a coverage threshold
    in the project's config exits non-zero for a test that passed, and a
    doubled `-q` (the project's `addopts` plus ours) prints no summary at all.
    """
    with tempfile.TemporaryDirectory(prefix="arbiter-report-") as scratch:
        report = Path(scratch) / "report.xml"
        try:
            proc = subprocess.run(
                [*cmd, node, "-q", "--no-header", f"--junitxml={report}"],
                cwd=cwd,
                # A test that waits on a terminal would wait forever; evidence
                # checks are never interactive.
                stdin=subprocess.DEVNULL,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return RunReport(ERRORED, f"timed out after {timeout:.0f}s")
        except OSError as e:
            return RunReport(ERRORED, f"could not run the test command: {e}")
        cases = _read_report(report)

    out = (proc.stdout + proc.stderr).strip()
    if cases is None:
        return _from_exit_status(proc.returncode, out)
    if cases:
        return _from_cases(cases)
    if proc.returncode in (_EXIT_USAGE, _EXIT_NO_TESTS):
        return _nothing_ran(out)
    return RunReport(
        ERRORED, _lead(f"the runner exited {proc.returncode} without running the test", out)
    )


def check_test_citation(
    citation: str,
    root: Path | None = None,
    test_command: list[str] | None = None,
    run: bool = True,
    scrub_env: bool = True,
    timeout: float = TEST_TIMEOUT,
) -> EvidenceCheck:
    """Confirm a cited test exists and, when permitted, that it passes.

    Executing the test runs code from the branch under review. With `scrub_env`
    the child gets a minimal environment so secrets are not reachable; it is
    still not a sandbox, and untrusted input should use `run=False`.
    """
    root = root or Path.cwd()
    m = _TEST_CITATION_RE.search(citation or "")
    if not m:
        return EvidenceCheck("test", citation, None, "no test node id found", NOT_RUN)

    node = m.group(0)
    path = _safe_path(m.group("path"), root)
    if path is None:
        return EvidenceCheck(
            "test", node, False, f"{m.group('path')} escapes the repository root", MISSING
        )
    if not path.is_file():
        return EvidenceCheck("test", node, False, f"{m.group('path')} does not exist", MISSING)

    if not run:
        return EvidenceCheck(
            "test", node, None, "test file exists; not executed (--no-run-tests)", NOT_RUN
        )

    cmd = test_command or detect_test_command(root)
    if not cmd:
        return EvidenceCheck("test", node, None, "no test runner detected; not executed", NOT_RUN)

    report = run_test(cmd, node, root, scrubbed_env() if scrub_env else None, timeout)
    return EvidenceCheck("test", node, _VERIFIED[report.outcome], report.detail, report.outcome)


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
    timeout: float = TEST_TIMEOUT,
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
        except OSError as e:
            return EvidenceCheck("baseline", node, None, f"could not run against base: {e}")
        report = run_test(cmd, node, base, env, timeout)

    if report.outcome == PASSED:
        return EvidenceCheck(
            "baseline",
            node,
            False,
            f"passes against {base_ref} too, so it does not distinguish this change",
        )
    if report.outcome == FAILED:
        return EvidenceCheck("baseline", node, True, f"fails against {base_ref}, passes here")
    # Anything else is not evidence either way. A test that cannot be collected
    # against the old code usually imports something this change introduced; one
    # that errors in setup, is skipped, or times out never ran its assertions.
    headline, cause = report.detail.splitlines()[0], _cause(report.detail)
    why = f"{headline} — {cause}" if cause else headline
    return EvidenceCheck(
        "baseline", node, None, f"could not run against {base_ref}: {why}"[:_DETAIL_WIDTH]
    )
