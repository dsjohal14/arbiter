"""Scoring a recorded run against a hand-labelled case, through `main()`.

Scoring replays the record rather than calling a model, so labelling a case
costs one paid run and every scoring after it is free.
"""

from __future__ import annotations

import json
import sys

import pytest

from arbiter.cli import main
from arbiter.verify import EXIT_FAIL, EXIT_PASS, EXIT_USAGE
from conftest import claude_events

PYTEST = f"{sys.executable} -m pytest"

DIFF = """diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def total(items):
-    return sum(items[1:])
+    return sum(items)
"""

PINNED_PLAN = """# Plan: fix the totals bug

<!-- arbiter:criteria -->

## Acceptance criteria

- **AC-1** — total() sums every item in the list
- **AC-2** — an empty list still returns 0

<!-- /arbiter:criteria -->
"""


def _review(criteria=(), findings=()):
    return json.dumps({"criteria": list(criteria), "ungrounded_findings": list(findings)})


def _ac(cid, status, impl=None, test=None, assessment="checked"):
    return {
        "id": cid,
        "status": status,
        "assessment": assessment,
        "implementation_evidence": impl,
        "test_evidence": test,
    }


REPORT = _review(
    [
        _ac("AC-1", "met", "app.py:2", "tests/test_app.py::test_sums_all"),
        _ac("AC-2", "unmet", "app.py:2", assessment="nothing handles the empty list"),
    ]
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "app.py").write_text("def total(items):\n    return sum(items)\n")
    (worktree / "tests").mkdir()
    (worktree / "tests" / "test_app.py").write_text(
        "from app import total\n\n\ndef test_sums_all():\n    assert total([1, 2]) == 3\n"
    )
    (tmp_path / "plan.md").write_text(PINNED_PLAN)
    (tmp_path / "change.diff").write_text(DIFF)
    monkeypatch.chdir(worktree)
    return tmp_path


@pytest.fixture
def recorded(repo, shim_path):
    """One recorded verify run, and the exit code it produced."""

    def run(report: str = REPORT, *extra: str):
        shim_path(claude_events(report))
        path = repo / "run.json"
        code = main(
            [
                "verify",
                "--plan",
                "../plan.md",
                "--diff",
                "../change.diff",
                "--models",
                "claude",
                "--test-command",
                PYTEST,
                "--record",
                str(path),
                *extra,
            ]
        )
        return path, code

    return run


def write_case(repo, expected, *, plan=PINNED_PLAN, diff=DIFF):
    case = repo / "case"
    case.mkdir(exist_ok=True)
    (case / "plan.md").write_text(plan)
    (case / "change.diff").write_text(diff)
    (case / "expected.json").write_text(json.dumps(expected))
    fixture = case / "repo"
    fixture.mkdir(exist_ok=True)
    (fixture / "app.py").write_text((repo / "worktree" / "app.py").read_text())
    (fixture / "tests").mkdir(exist_ok=True)
    (fixture / "tests" / "test_app.py").write_text(
        (repo / "worktree" / "tests" / "test_app.py").read_text()
    )
    return case


ANSWER_KEY = {
    "schema_version": 1,
    "command": "verify",
    "description": "the empty-list promise is never kept",
    "verdict": "fail",
    "criteria": {"AC-1": "met", "AC-2": "unmet"},
}


def test_a_run_matching_the_answer_key_scores_a_pass(recorded, repo, capsys):
    path, _ = recorded()
    case = write_case(repo, ANSWER_KEY)
    capsys.readouterr()

    code = main(["eval", "score", str(case), "--record", str(path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_PASS
    assert payload["passed"] is True
    assert payload["criteria_accuracy"] == 1.0
    assert payload["verdict"] == {"expected": "fail", "actual": "fail", "correct": True}
    assert payload["drift"] == []


def test_a_wrong_run_is_scored_against_the_key_not_against_itself(recorded, repo, capsys):
    """The reviewer calls both criteria met; the key says AC-2 is unmet. The
    case is what decides whether Arbiter was right."""
    path, _ = recorded(
        _review(
            [
                _ac("AC-1", "met", "app.py:2", "tests/test_app.py::test_sums_all"),
                _ac("AC-2", "met", "app.py:2", "tests/test_app.py::test_sums_all"),
            ]
        )
    )
    case = write_case(repo, ANSWER_KEY)
    capsys.readouterr()

    code = main(["eval", "score", str(case), "--record", str(path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_FAIL
    assert payload["criteria_accuracy"] == 0.5
    assert "AC-2: expected unmet, got met" in payload["failures"]
    assert payload["verdict"]["correct"] is False


def test_a_record_of_a_different_plan_cannot_be_scored_against_this_case(recorded, repo, capsys):
    path, _ = recorded()
    case = write_case(repo, ANSWER_KEY, plan=PINNED_PLAN.replace("empty list", "empty tuple"))
    capsys.readouterr()

    code = main(["eval", "score", str(case), "--record", str(path)])

    assert code == EXIT_USAGE
    assert "recorded from a different plan" in capsys.readouterr().err


def test_scoring_a_plan_review_record_against_a_verify_case_is_refused(recorded, repo, capsys):
    path, _ = recorded()
    record = json.loads(path.read_text())
    record["command"] = "plan review"
    path.write_text(json.dumps(record))
    case = write_case(repo, ANSWER_KEY)
    capsys.readouterr()

    assert main(["eval", "score", str(case), "--record", str(path)]) == EXIT_USAGE
    assert "is a verify case" in capsys.readouterr().err


def test_validate_reports_every_case_and_fails_on_a_broken_one(repo, capsys):
    good = write_case(repo, ANSWER_KEY)
    bad = repo / "broken"
    bad.mkdir()
    (bad / "repo").mkdir()
    (bad / "plan.md").write_text(PINNED_PLAN)
    (bad / "change.diff").write_text(DIFF)
    (bad / "expected.json").write_text(
        json.dumps({"schema_version": 1, "command": "verify", "criteria": {"AC-7": "met"}})
    )

    code = main(["eval", "validate", str(good), str(bad), "--json"])
    reports = json.loads(capsys.readouterr().out)

    assert code == EXIT_FAIL
    assert [r["ok"] for r in reports] == [True, False]
    assert "AC-7" in reports[1]["error"]


# ── Binding a record to its case ─────────────────────────────────────────────
#
# Every check here fails closed. An accuracy number computed from a run of
# something else is the one output this layer exists to prevent.


def test_a_record_with_no_recorded_plan_cannot_be_scored(recorded, repo, capsys):
    path, _ = recorded()
    record = json.loads(path.read_text())
    del record["inputs"]["plan"]
    path.write_text(json.dumps(record))
    case = write_case(repo, ANSWER_KEY)
    capsys.readouterr()

    assert main(["eval", "score", str(case), "--record", str(path)]) == EXIT_USAGE
    assert "no recorded plan" in capsys.readouterr().err


def test_a_claimed_digest_is_recomputed_not_trusted(recorded, repo, capsys):
    """A hash sitting next to the content proves nothing about that content. A
    record whose plan was swapped while its digest was left alone must not be
    scored just because the digest still matches the case."""
    path, _ = recorded()
    case = write_case(repo, ANSWER_KEY)
    record = json.loads(path.read_text())
    record["inputs"]["plan"]["content"] = "# A completely different plan\n"
    path.write_text(json.dumps(record))
    capsys.readouterr()

    assert main(["eval", "score", str(case), "--record", str(path)]) == EXIT_USAGE
    assert "does not match its own sha256" in capsys.readouterr().err


def test_a_record_with_no_digest_at_all_is_still_checked(recorded, repo, capsys):
    """Absence of a hash used to be read as permission to proceed."""
    path, _ = recorded()
    case = write_case(repo, ANSWER_KEY)
    record = json.loads(path.read_text())
    del record["inputs"]["plan"]["sha256"]
    record["inputs"]["plan"]["content"] = "# A completely different plan\n"
    path.write_text(json.dumps(record))
    capsys.readouterr()

    assert main(["eval", "score", str(case), "--record", str(path)]) == EXIT_USAGE
    assert "recorded from a different plan" in capsys.readouterr().err


# ── Binding a record to the tree its evidence came from ──────────────────────


def _case_with_repo(repo, contents: str):
    case = write_case(repo, ANSWER_KEY)
    (case / "repo" / "app.py").write_text(contents)
    return case


def test_a_case_shipping_a_repo_requires_a_matching_workspace(recorded, repo, capsys):
    path, _ = recorded()
    case = _case_with_repo(repo, "def total(items):\n    return sum(items)\n")
    capsys.readouterr()

    assert main(["eval", "score", str(case), "--record", str(path), "--json"]) == EXIT_PASS


def test_a_record_from_another_tree_is_refused(recorded, repo, capsys):
    """Same plan, same diff, different code — so the cited test could pass in
    one tree and fail in the other, and the answer key means nothing here."""
    path, _ = recorded()
    case = _case_with_repo(repo, "def total(items):\n    return 0  # not the same code\n")
    capsys.readouterr()

    assert main(["eval", "score", str(case), "--record", str(path)]) == EXIT_USAGE
    assert "ran against a different tree" in capsys.readouterr().err


def test_an_unchanged_cited_test_is_part_of_the_workspace_binding(recorded, repo, capsys):
    path, _ = recorded()
    case = write_case(repo, ANSWER_KEY)
    test_file = case / "repo" / "tests" / "test_app.py"
    test_file.write_text(test_file.read_text().replace("== 3", "== 999"))
    capsys.readouterr()

    assert main(["eval", "score", str(case), "--record", str(path)]) == EXIT_USAGE
    error = capsys.readouterr().err
    assert "ran against a different tree" in error
    assert "tests/test_app.py" in error


def test_non_git_pytest_cache_created_after_recording_does_not_break_scoring(
    recorded, repo, capsys
):
    path, _ = recorded()
    case = write_case(repo, ANSWER_KEY)
    cache = case / "repo" / ".pytest_cache" / "v" / "cache"
    cache.mkdir(parents=True)
    (cache / "nodeids").write_text("[]")
    (cache / "lastfailed").write_text("{}")
    capsys.readouterr()

    assert main(["eval", "score", str(case), "--record", str(path)]) == EXIT_PASS


def test_workspace_scope_mismatch_is_refused_explicitly(recorded, repo, capsys):
    path, _ = recorded()
    case = write_case(repo, ANSWER_KEY)
    record = json.loads(path.read_text())
    assert record["workspace"]["scope"] == "tree"
    record["workspace"]["scope"] = "git-visible"
    path.write_text(json.dumps(record))
    capsys.readouterr()

    assert main(["eval", "score", str(case), "--record", str(path)]) == EXIT_USAGE
    error = capsys.readouterr().err
    assert "workspace scope 'git-visible'" in error
    assert "resolves as 'tree'" in error


def test_a_record_without_workspace_identity_cannot_be_bound_to_a_repo(recorded, repo, capsys):
    path, _ = recorded()
    record = json.loads(path.read_text())
    record.pop("workspace", None)
    record["schema_version"] = 1
    path.write_text(json.dumps(record))
    case = _case_with_repo(repo, "def total(items):\n    return sum(items)\n")
    capsys.readouterr()

    assert main(["eval", "score", str(case), "--record", str(path)]) == EXIT_USAGE
    assert "no complete workspace identity" in capsys.readouterr().err
