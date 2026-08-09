"""Replay has one job: reach the recorded result from the recorded responses.

These tests build records by hand rather than through the CLI, because what is
under test is the reconstruction — which criteria the run worked from, which
responses count, and what has to be restored rather than recomputed.
"""

from __future__ import annotations

import json

import pytest

from arbiter import replay
from arbiter.replay import ReplayError

PINNED_PLAN = """# Plan: fix the totals bug

<!-- arbiter:criteria -->

## Acceptance criteria

- **AC-1** — total() sums every item in the list
- **AC-2** — total([]) returns 0

<!-- /arbiter:criteria -->
"""

RAW_PLAN = "# Plan: fix the totals bug\n\nMake total() count every element.\n"

DIFF = """diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def total(items):
-    return sum(items[1:])
+    return sum(items)
"""


def _ac(cid, status, impl=None, test=None):
    return {
        "id": cid,
        "status": status,
        "assessment": "checked",
        "implementation_evidence": impl,
        "test_evidence": test,
    }


def _review(*criteria, findings=()):
    return json.dumps({"criteria": list(criteria), "ungrounded_findings": list(findings)})


def _call(phase, role, content, status="completed"):
    return {
        "phase": phase,
        "role": role,
        "provider": role,
        "cli": role,
        "model": None,
        "status": status,
        "duration_ms": 12,
        "content": content,
        "error": None if status == "completed" else {"type": "ModelCallError", "message": "boom"},
        "usage": None,
    }


def verify_record(*, calls=None, result=None, configuration=None, plan=PINNED_PLAN) -> dict:
    report = _review(_ac("AC-1", "met", "app.py:2", "tests/test_app.py::t"), _ac("AC-2", "met"))
    return {
        "schema_version": 1,
        "sensitive": True,
        "command": "verify",
        "status": "completed",
        "inputs": {
            "plan": {"source": "plan.md", "sha256": "x", "content": plan},
            "diff": {"source": "change.diff", "sha256": "y", "content": DIFF},
        },
        "configuration": {"models": ["claude"], "judge": "claude", **(configuration or {})},
        "provider_calls": calls
        if calls is not None
        else [_call("reviewer", "claude", report), _call("consolidator", "consolidator", report)],
        "result": result,
        "exit_code": 0,
        "error": None,
    }


def recorded_result(*criteria, status="pass", findings=()):
    return {
        "status": status,
        "exit_code": {"pass": 0, "fail": 1, "unclear": 3}[status],
        "criteria": list(criteria),
        "ungrounded_findings": list(findings),
        "reviewers": ["claude"],
        "failed_reviewers": [],
        "blinding": {"seed": 7, "label_to_model": {"A": "claude"}},
    }


def recorded_criterion(cid, status, **extra):
    row = {
        "id": cid,
        "text": "",
        "status": status,
        "assessment": "checked",
        "implementation_evidence": None,
        "test_evidence": None,
        "impl_check": None,
        "test_check": None,
        "baseline_check": None,
        "evidence_source": "model_assessed",
        "downgraded_from": None,
        "downgrade_reason": None,
    }
    row.update(extra)
    return row


# ── Reading records ───────────────────────────────────────────────────────────


def test_a_newer_schema_version_is_refused_rather_than_guessed_at(tmp_path):
    path = tmp_path / "run.json"
    path.write_text(json.dumps({"schema_version": 99, "command": "verify"}))

    with pytest.raises(ReplayError, match="schema v99"):
        replay.load_record(path)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"command": "verify"}, "no schema_version"),
        ({"schema_version": 1, "command": "debate"}, "Cannot replay"),
        ([], "not a run record"),
    ],
)
def test_records_this_build_cannot_replay_are_rejected(tmp_path, payload, message):
    path = tmp_path / "run.json"
    path.write_text(json.dumps(payload))

    with pytest.raises(ReplayError, match=message):
        replay.load_record(path)


def test_unparseable_json_names_the_file(tmp_path):
    path = tmp_path / "run.json"
    path.write_text("{not json")

    with pytest.raises(ReplayError, match="not valid JSON"):
        replay.load_record(path)


# ── Verify ────────────────────────────────────────────────────────────────────


def test_replaying_a_verify_record_reproduces_its_verdict():
    record = verify_record(
        result=recorded_result(
            recorded_criterion("AC-1", "met", test_evidence="tests/test_app.py::t"),
            recorded_criterion("AC-2", "untested"),
        )
    )

    outcome = replay.replay(record)

    assert outcome.command == "verify"
    assert outcome.result.status == "pass"
    assert {c.id: c.status for c in outcome.result.criteria} == {"AC-1": "met", "AC-2": "untested"}
    assert outcome.drift == []
    assert outcome.matches is True


def test_recorded_evidence_is_restored_rather_than_rechecked(tmp_path, monkeypatch):
    """Nothing is re-run, so a citation the current tree cannot satisfy still
    replays as the run found it — replay reproduces a verdict, it does not
    re-derive one from today's repository."""
    monkeypatch.chdir(tmp_path)  # an empty tree: no app.py, no tests
    record = verify_record(
        result=recorded_result(
            recorded_criterion(
                "AC-1",
                "met",
                test_evidence="tests/test_app.py::t",
                impl_check={
                    "kind": "file",
                    "citation": "app.py:2",
                    "verified": True,
                    "detail": "return sum(items)",
                },
                test_check={
                    "kind": "test",
                    "citation": "tests/test_app.py::t",
                    "verified": True,
                    "detail": "passed",
                },
            ),
            recorded_criterion("AC-2", "untested"),
        )
    )

    outcome = replay.replay(record)

    row = next(c for c in outcome.result.criteria if c.id == "AC-1")
    assert row.status == "met"
    assert row.test_check.verified is True
    assert row.impl_check.detail == "return sum(items)"
    assert outcome.drift == []


def test_the_downgrade_rules_run_again_on_the_restored_checks():
    """A cited test that failed at record time still overrules the reviewer,
    which is what makes replay useful for changing the downgrade rules."""
    record = verify_record(
        result=recorded_result(
            recorded_criterion(
                "AC-1",
                "unmet",
                test_evidence="tests/test_app.py::t",
                test_check={
                    "kind": "test",
                    "citation": "tests/test_app.py::t",
                    "verified": False,
                    "detail": "failed\nassert 5 == 6",
                },
                downgraded_from="met",
            ),
            recorded_criterion("AC-2", "met"),
            status="fail",
        )
    )

    outcome = replay.replay(record)

    row = next(c for c in outcome.result.criteria if c.id == "AC-1")
    assert row.status == "unmet"
    assert row.downgraded_from == "met"
    assert row.downgrade_reason.startswith("cited test fails")
    assert outcome.result.status == "fail"


def test_drift_names_the_verdict_and_every_criterion_that_moved():
    record = verify_record(
        result=recorded_result(
            recorded_criterion("AC-1", "unmet"),
            recorded_criterion("AC-2", "met"),
            status="fail",
        )
    )

    outcome = replay.replay(record)

    assert outcome.matches is False
    assert "verdict: recorded fail, replayed pass" in outcome.drift
    assert any(line.startswith("AC-1: recorded unmet, replayed") for line in outcome.drift)


def test_a_reviewer_that_never_answered_still_blocks_a_clean_pass():
    record = verify_record(
        configuration={"models": ["claude", "codex"]},
        calls=[
            _call("reviewer", "claude", _review(_ac("AC-1", "met"), _ac("AC-2", "met"))),
            _call("reviewer", "codex", None, status="failed"),
        ],
    )

    outcome = replay.replay(record)

    assert outcome.result.failed_reviewers == ["codex"]
    assert outcome.result.status == "unclear"


def test_a_record_without_a_consolidator_replays_the_reviewer_fallback():
    record = verify_record(
        calls=[_call("reviewer", "claude", _review(_ac("AC-1", "unmet"), _ac("AC-2", "met")))]
    )

    outcome = replay.replay(record)

    assert outcome.result.status == "fail"
    assert any("No consolidator response" in note for note in outcome.notes)


def test_a_record_with_no_reviewer_responses_cannot_be_replayed():
    record = verify_record(calls=[_call("reviewer", "claude", None, status="failed")])

    with pytest.raises(ReplayError, match="no completed reviewer responses"):
        replay.replay(record)


def test_a_record_of_a_crashed_run_replays_but_says_it_cannot_be_compared():
    record = verify_record(result=None)

    outcome = replay.replay(record)

    assert outcome.comparable is False
    assert outcome.matches is False
    assert outcome.drift == []
    assert any("no result" in note for note in outcome.notes)
    assert any("unchecked" in note for note in outcome.notes)


def test_a_verify_record_whose_plan_lost_its_criteria_is_refused():
    with pytest.raises(ReplayError, match="no usable criteria"):
        replay.replay(verify_record(plan=RAW_PLAN))


# ── Plan review ───────────────────────────────────────────────────────────────


def _finding(kind="ambiguous", action="add", **extra):
    return {
        "kind": kind,
        "action": action,
        "target": None,
        "problem": "the limit is never stated",
        "proposed_criterion": "Clients are limited to 100 requests a minute.",
        "proposed_test": "tests/test_limit.py::test_over_limit",
        "quote": None,
        **extra,
    }


def plan_review_record(*, calls=None, configuration=None, result=None, plan=RAW_PLAN) -> dict:
    critique = json.dumps({"findings": [_finding()]})
    return {
        "schema_version": 1,
        "sensitive": True,
        "command": "plan review",
        "status": "completed",
        "inputs": {"plan": {"source": "plan.md", "sha256": "x", "content": plan}},
        "configuration": {
            "models": ["claude"],
            "judge": "claude",
            "criteria_source": "extracted",
            **(configuration or {}),
        },
        "provider_calls": calls
        if calls is not None
        else [
            _call("extractor", "extractor", json.dumps({"criteria": ["total() sums every item"]})),
            _call("reviewer", "claude", critique),
            _call("consolidator", "consolidator", critique),
        ],
        "result": result,
        "exit_code": 1,
        "error": None,
    }


def test_an_extracted_contract_is_replayed_from_the_recorded_extraction():
    """Re-extracting would call a model and could enumerate different promises,
    which would make the replay a different run."""
    outcome = replay.replay(plan_review_record())

    assert [c.text for c in outcome.result.criteria] == [
        "total() sums every item",
        "Clients are limited to 100 requests a minute.",
    ]
    assert outcome.result.ready is False
    assert outcome.result.exit_code == 1


def test_a_pinned_plan_review_reads_its_criteria_back_out_of_the_plan():
    record = plan_review_record(
        plan=PINNED_PLAN,
        configuration={"criteria_source": "pinned"},
        calls=[
            _call("reviewer", "claude", json.dumps({"findings": []})),
            _call("consolidator", "consolidator", json.dumps({"findings": []})),
        ],
    )

    outcome = replay.replay(record)

    assert [c.id for c in outcome.result.criteria] == ["AC-1", "AC-2"]
    assert outcome.result.ready is True


def test_an_extracted_record_without_its_extraction_is_refused():
    record = plan_review_record(
        calls=[_call("reviewer", "claude", json.dumps({"findings": [_finding()]}))]
    )

    with pytest.raises(ReplayError, match="no extractor response"):
        replay.replay(record)


def test_plan_review_drift_reports_readiness_and_finding_counts():
    record = plan_review_record(
        result={
            "ready": True,
            "exit_code": 0,
            "counts": dict.fromkeys(
                (
                    "contradiction",
                    "ambiguous",
                    "untestable",
                    "missing",
                    "needs_human",
                    "unnecessary",
                ),
                0,
            ),
            "findings": [],
            "criteria": [],
            "reviewers": ["claude"],
            "failed_reviewers": [],
            "blinding": None,
        }
    )

    outcome = replay.replay(record)

    assert outcome.matches is False
    assert "readiness: recorded ready, replayed blocked" in outcome.drift
    assert "ambiguous findings: recorded 0, replayed 1" in outcome.drift
