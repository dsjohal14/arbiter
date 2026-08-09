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
    (tmp_path / "app.py").write_text("def total(items):\n    return sum(items)\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "from app import total\n\n\ndef test_sums_all():\n    assert total([1, 2]) == 3\n"
    )
    (tmp_path / "plan.md").write_text(PINNED_PLAN)
    (tmp_path / "change.diff").write_text(DIFF)
    monkeypatch.chdir(tmp_path)
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
                "plan.md",
                "--diff",
                "change.diff",
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
