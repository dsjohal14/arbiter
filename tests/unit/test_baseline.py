"""Baseline discrimination: does a cited test actually prove the change?

Codex's review correctly noticed that a branch can weaken the very test the plan
selected as proof and coast on the plan author's earlier approval. Its fix — any
change to that test file invalidates it — also rejected the *normal* workflow,
where the agent writes the feature and its test in one diff.

Running the current test against the base revision separates those: a test that
fails there and passes here is proof; one that passes both sides proves nothing.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from arbiter.evidence import (
    base_worktree,
    check_test_discriminates,
)
from arbiter.evidence import (
    test_node_changed as _test_node_changed,
)
from arbiter.verify import (
    CriterionResult,
    added_paths,
    apply_evidence_checks,
    changed_paths,
    derive_verdict,
    refresh_plan_selected_evidence,
    resolve_base_ref,
)

PYTEST = [sys.executable, "-m", "pytest"]


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


@pytest.fixture
def repo(tmp_path):
    """A repo whose base is buggy and whose branch fixes it."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "samplepkg").mkdir(parents=True)
    (tmp_path / "app.py").write_text("def total(items):\n    return sum(items[1:])\n")
    (tmp_path / "src" / "samplepkg" / "__init__.py").write_text(
        "def total(items):\n    return sum(items[1:])\n"
    )
    (tmp_path / "tests" / "test_app.py").write_text(
        "from app import total\n\n\ndef test_old():\n    assert total([9, 1, 2]) == 3\n"
    )
    _git("init", "-q", "-b", "main", ".", cwd=tmp_path)
    _git("config", "user.email", "t@t", cwd=tmp_path)
    _git("config", "user.name", "t", cwd=tmp_path)
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-qm", "base", cwd=tmp_path)

    # The fix lands on a branch, so `main` keeps pointing at the buggy base.
    _git("checkout", "-qb", "feat", cwd=tmp_path)
    (tmp_path / "app.py").write_text("def total(items):\n    return sum(items)\n")
    (tmp_path / "src" / "samplepkg" / "__init__.py").write_text(
        "def total(items):\n    return sum(items)\n"
    )
    (tmp_path / "tests" / "test_app.py").write_text(
        "from app import total\n\n\n"
        "def test_sums_all():\n    assert total([1, 2]) == 3\n\n\n"
        "def test_vacuous():\n    assert True\n\n\n"
        "def test_added_to_existing_file():\n    assert total([1, 2]) == 3\n"
    )
    (tmp_path / "tests" / "test_src_layout.py").write_text(
        "from samplepkg import total\n\n\n"
        "def test_imports_the_checked_out_package():\n    assert total([1, 2]) == 3\n"
    )
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-qm", "fix", cwd=tmp_path)
    return tmp_path


def test_a_real_test_for_the_change_discriminates(repo):
    check = check_test_discriminates("tests/test_app.py::test_sums_all", repo, "main", PYTEST)
    assert check.verified is True
    assert "fails against main" in check.detail


def test_a_vacuous_test_does_not_discriminate(repo):
    """`assert True` passes everywhere, so it proves nothing about the change —
    this is the weakened-test attack, caught mechanically rather than by
    forbidding the diff from touching the file."""
    check = check_test_discriminates("tests/test_app.py::test_vacuous", repo, "main", PYTEST)
    assert check.verified is False
    assert "does not distinguish" in check.detail


def test_the_base_run_does_not_import_the_current_editable_source(repo, monkeypatch):
    """An editable install commonly leaves the current src/ on PYTHONPATH. The
    baseline must replace it with the checked-out src/ or it compares HEAD twice."""
    monkeypatch.setenv("PYTHONPATH", str(repo / "src"))
    check = check_test_discriminates(
        "tests/test_src_layout.py::test_imports_the_checked_out_package", repo, "main", PYTEST
    )
    assert check.verified is True


def test_a_new_node_in_an_existing_test_file_does_not_stale_plan_approval(repo):
    with base_worktree(repo, "main") as base:
        assert base is not None
        assert (
            _test_node_changed("tests/test_app.py::test_added_to_existing_file", repo, base)
            is False
        )
        result = CriterionResult(
            id="AC-1",
            text="sum every item",
            status="met",
            test_evidence="tests/test_app.py::test_added_to_existing_file",
            evidence_source="plan_selected_changed",
        )
        refresh_plan_selected_evidence([result], repo, base)
        assert result.evidence_source == "plan_selected"


def test_strict_discrimination_rejects_an_uncheckable_base(repo):
    result = CriterionResult(
        id="AC-1",
        text="sum every item",
        status="met",
        implementation_evidence="app.py:2",
        test_evidence="tests/test_app.py::test_sums_all",
        evidence_source="plan_selected",
    )
    apply_evidence_checks(
        result,
        repo,
        PYTEST,
        base_ref="definitely-not-a-ref",
        require_discriminating_tests=True,
    )
    assert result.status == "untested"
    assert derive_verdict([result], require_pinned_evidence=True) == "fail"


def test_the_working_tree_is_left_untouched(repo):
    before = (repo / "app.py").read_text()
    check_test_discriminates("tests/test_app.py::test_sums_all", repo, "main", PYTEST)
    assert (repo / "app.py").read_text() == before
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    )
    assert status.stdout.strip() == "", "baseline check must not dirty the repo"
    worktrees = subprocess.run(
        ["git", "worktree", "list"], cwd=repo, capture_output=True, text=True
    )
    assert worktrees.stdout.count("\n") == 1, "the temporary worktree must be removed"


def test_an_unknown_base_ref_is_reported_not_punished(repo):
    check = check_test_discriminates("tests/test_app.py::test_sums_all", repo, "nope", PYTEST)
    assert check.verified is None
    assert "could not check out" in check.detail


def test_a_missing_test_file_is_reported_not_punished(repo):
    check = check_test_discriminates("tests/absent.py::test_x", repo, "main", PYTEST)
    assert check.verified is None


# ── Diff analysis ────────────────────────────────────────────────────────────

ADDED = """diff --git a/tests/test_new.py b/tests/test_new.py
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/tests/test_new.py
@@ -0,0 +1,2 @@
+def test_x():
+    assert True
diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
-old
+new
"""


def test_added_files_are_distinguished_from_modified_ones():
    """A test the diff *creates* was named in the contract before the code
    existed — that is the normal workflow, not a weakened approval."""
    assert added_paths(ADDED) == {"tests/test_new.py"}
    assert changed_paths(ADDED) == {"tests/test_new.py", "app.py"}


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("origin/main...HEAD", "origin/main"),
        ("main..HEAD", "main"),
        ("staged", "HEAD"),
        ("working", "HEAD"),
        ("change.diff", None),
        ("-", None),
    ],
)
def test_base_ref_is_inferred_only_when_knowable(spec, expected):
    """A diff from a file or stdin has no checkout-able base; guessing one would
    silently compare against the wrong tree."""
    assert resolve_base_ref(spec) == expected
