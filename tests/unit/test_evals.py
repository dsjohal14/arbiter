"""An answer key that cannot mean what it says is worse than no answer key.

Loading is strict on purpose: a criterion id the plan never promised, or an
expectation that constrains nothing, would quietly inflate the accuracy number
the corpus exists to produce.
"""

from __future__ import annotations

import json

import pytest

from arbiter import evals
from arbiter.evals import EvalCaseError

PINNED_PLAN = """# Plan: fix the totals bug

<!-- arbiter:criteria -->

## Acceptance criteria

- **AC-1** — total() sums every item in the list
- **AC-2** — total([]) returns 0

<!-- /arbiter:criteria -->
"""

DIFF = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-a\n+b\n"


def write_case(path, expected, *, plan=PINNED_PLAN, diff=DIFF, with_repo=True):
    path.mkdir(parents=True, exist_ok=True)
    (path / "plan.md").write_text(plan)
    if diff is not None:
        (path / "change.diff").write_text(diff)
    if expected.get("command") == "verify" and with_repo:
        (path / "repo").mkdir(exist_ok=True)
    (path / "expected.json").write_text(json.dumps(expected))
    return path


def verify_case(tmp_path, **overrides):
    expected = {
        "schema_version": 1,
        "command": "verify",
        "description": "a seeded defect",
        "verdict": "fail",
        "criteria": {"AC-1": "unmet", "AC-2": "met"},
        **overrides,
    }
    return write_case(tmp_path / "totals", expected)


def verify_result(*criteria, status="fail", findings=()):
    return {
        "status": status,
        "criteria": [{"id": cid, "status": st} for cid, st in criteria],
        "ungrounded_findings": list(findings),
    }


# ── Loading ───────────────────────────────────────────────────────────────────


def test_a_complete_case_loads_with_its_inputs(tmp_path):
    case = evals.load_case(verify_case(tmp_path))

    assert case.id == "totals"
    assert case.command == "verify"
    assert case.verdict == "fail"
    assert case.criteria == {"AC-1": "unmet", "AC-2": "met"}
    assert case.plan == PINNED_PLAN
    assert case.diff == DIFF


def test_labelling_a_criterion_the_plan_never_pinned_is_an_error(tmp_path):
    path = verify_case(tmp_path, criteria={"AC-9": "unmet"})

    with pytest.raises(EvalCaseError, match="AC-9"):
        evals.load_case(path)


def test_an_unknown_status_is_an_error(tmp_path):
    path = verify_case(tmp_path, criteria={"AC-1": "broken"})

    with pytest.raises(EvalCaseError, match="allowed"):
        evals.load_case(path)


def test_a_verify_case_needs_its_diff(tmp_path):
    path = write_case(
        tmp_path / "no-diff",
        {"schema_version": 1, "command": "verify"},
        diff=None,
    )

    with pytest.raises(EvalCaseError, match=r"change\.diff"):
        evals.load_case(path)


def test_a_verify_case_needs_a_fixture_repo(tmp_path):
    path = write_case(
        tmp_path / "no-repo",
        {"schema_version": 1, "command": "verify", "verdict": "fail"},
        with_repo=False,
    )

    with pytest.raises(EvalCaseError, match=r"no repo/"):
        evals.load_case(path)


def test_per_criterion_labels_are_rejected_on_a_plan_review_case(tmp_path):
    """A plan review proposes edits; it never returns a status per criterion."""
    path = write_case(
        tmp_path / "plan-case",
        {"schema_version": 1, "command": "plan review", "criteria": {"AC-1": "met"}},
        diff=None,
    )

    with pytest.raises(EvalCaseError, match="must_find"):
        evals.load_case(path)


def test_a_newer_case_schema_is_refused(tmp_path):
    path = verify_case(tmp_path, schema_version=99)

    with pytest.raises(EvalCaseError, match="case schema v99"):
        evals.load_case(path)


def test_an_expectation_that_constrains_nothing_is_refused(tmp_path):
    path = verify_case(tmp_path, must_find=[{"label": "something"}])

    with pytest.raises(EvalCaseError, match="constrains nothing"):
        evals.load_case(path)


def test_an_expectation_cannot_constrain_a_field_the_command_has_no_such_thing_as(tmp_path):
    path = verify_case(tmp_path, must_find=[{"kind": "ambiguous"}])

    with pytest.raises(EvalCaseError, match="unknown field 'kind'"):
        evals.load_case(path)


def test_an_expectation_cannot_use_a_value_outside_the_enum(tmp_path):
    path = verify_case(tmp_path, must_find=[{"severity": "catastrophic"}])

    with pytest.raises(EvalCaseError, match="allowed"):
        evals.load_case(path)


def test_a_plan_review_case_may_constrain_kind_action_and_target(tmp_path):
    path = write_case(
        tmp_path / "rate-limit",
        {
            "schema_version": 1,
            "command": "plan review",
            "verdict": "blocked",
            "must_find": [
                {
                    "label": "no request limit is stated",
                    "kind": "ambiguous",
                    "action": "add",
                    "match": ["limit"],
                }
            ],
        },
        plan="# Plan\n\nAdd a rate limiter.\n",
        diff=None,
    )

    case = evals.load_case(path)

    assert case.verdict == "blocked"
    assert case.must_find[0].constraints == {"kind": "ambiguous", "action": "add"}
    assert case.must_find[0].match == ("limit",)


# ── Scoring ───────────────────────────────────────────────────────────────────


def test_a_run_that_matches_the_key_passes(tmp_path):
    case = evals.load_case(verify_case(tmp_path))

    score = evals.score(case, verify_result(("AC-1", "unmet"), ("AC-2", "met")))

    assert score.passed is True
    assert score.failures == []
    assert score.criteria_accuracy == 1.0
    assert score.verdict_correct is True


def test_every_wrong_answer_is_named(tmp_path):
    case = evals.load_case(verify_case(tmp_path))

    score = evals.score(case, verify_result(("AC-1", "met"), ("AC-2", "met"), status="pass"))

    assert score.passed is False
    assert score.criteria_accuracy == 0.5
    assert "verdict: expected fail, got pass" in score.failures
    assert "AC-1: expected unmet, got met" in score.failures


def test_a_criterion_the_run_never_reported_counts_against_it(tmp_path):
    case = evals.load_case(verify_case(tmp_path))

    score = evals.score(case, verify_result(("AC-1", "unmet")))

    assert score.passed is False
    assert "AC-2: expected met, got no verdict" in score.failures


def test_a_required_finding_is_matched_on_its_fields_and_substrings(tmp_path):
    case = evals.load_case(
        verify_case(
            tmp_path,
            must_find=[
                {
                    "label": "the empty-list path raises",
                    "severity": "blocking",
                    "match": ["ZeroDivisionError"],
                }
            ],
        )
    )
    result = verify_result(
        ("AC-1", "unmet"),
        ("AC-2", "met"),
        findings=[
            {
                "severity": "blocking",
                "claim": "average([]) raises zerodivisionerror",
                "evidence": "app.py:3",
            }
        ],
    )

    score = evals.score(case, result)

    assert score.recall == 1.0
    assert score.passed is True


def test_a_finding_that_misses_one_constraint_does_not_count(tmp_path):
    case = evals.load_case(
        verify_case(
            tmp_path,
            must_find=[{"label": "blocking gap", "severity": "blocking", "match": ["empty"]}],
        )
    )
    result = verify_result(
        ("AC-1", "unmet"),
        ("AC-2", "met"),
        findings=[{"severity": "minor", "claim": "the empty list path is odd", "evidence": "x"}],
    )

    score = evals.score(case, result)

    assert score.recall == 0.0
    assert "missed finding: blocking gap" in score.failures


def test_a_forbidden_finding_is_a_false_positive(tmp_path):
    case = evals.load_case(
        verify_case(
            tmp_path,
            must_not_find=[{"label": "invented blocker", "severity": "blocking"}],
        )
    )
    result = verify_result(
        ("AC-1", "unmet"),
        ("AC-2", "met"),
        findings=[{"severity": "blocking", "claim": "everything is wrong", "evidence": "x"}],
    )

    score = evals.score(case, result)

    assert score.passed is False
    assert [f.label for f in score.false_positives] == ["invented blocker"]
    assert "unwanted finding: invented blocker" in score.failures


def test_recall_is_none_when_the_case_requires_no_findings(tmp_path):
    case = evals.load_case(verify_case(tmp_path))

    score = evals.score(case, verify_result(("AC-1", "unmet"), ("AC-2", "met")))

    assert score.recall is None
    assert score.as_dict()["recall"] is None


def test_a_plan_review_run_is_scored_on_readiness_and_its_edits(tmp_path):
    case = evals.load_case(
        write_case(
            tmp_path / "rate-limit",
            {
                "schema_version": 1,
                "command": "plan review",
                "verdict": "blocked",
                "must_find": [
                    {"label": "limit is unstated", "kind": "ambiguous", "match": ["limit"]}
                ],
            },
            plan="# Plan\n\nAdd a rate limiter.\n",
            diff=None,
        )
    )
    result = {
        "ready": False,
        "findings": [
            {
                "kind": "ambiguous",
                "action": "add",
                "target": None,
                "problem": "the request limit is never stated",
                "proposed_criterion": "Clients are limited to 100 requests a minute.",
            }
        ],
        "criteria": [],
    }

    score = evals.score(case, result)

    assert score.verdict_actual == "blocked"
    assert score.passed is True
    assert score.criteria_accuracy is None


# ── A case has to assert something ───────────────────────────────────────────


def test_a_case_that_asserts_nothing_is_refused(tmp_path):
    """It has no failures, so it scores `passed` against any result at all —
    including one that got everything wrong. That is worse than having no case:
    it inflates both the corpus and the pass rate."""
    path = write_case(
        tmp_path / "empty",
        {"schema_version": 1, "command": "verify", "description": "nothing asserted"},
    )

    with pytest.raises(EvalCaseError, match="asserts nothing"):
        evals.load_case(path)


@pytest.mark.parametrize(
    "key",
    ["verdict", "criteria", "must_find", "must_not_find"],
)
def test_any_single_assertion_is_enough(tmp_path, key):
    payload = {
        "verdict": "fail",
        "criteria": {"AC-1": "unmet"},
        "must_find": [{"severity": "blocking"}],
        "must_not_find": [{"severity": "question"}],
    }[key]
    path = write_case(tmp_path / "one", {"schema_version": 1, "command": "verify", key: payload})

    assert evals.load_case(path) is not None


def test_an_empty_match_needle_is_refused(tmp_path):
    """ "" is a substring of everything, so it quietly turns the expectation into
    'any finding at all'."""
    path = verify_case(tmp_path, must_find=[{"severity": "blocking", "match": [""]}])

    with pytest.raises(EvalCaseError, match=r"empty `match`"):
        evals.load_case(path)


def test_a_whitespace_only_needle_is_refused(tmp_path):
    path = verify_case(tmp_path, must_find=[{"severity": "blocking", "match": ["   "]}])

    with pytest.raises(EvalCaseError, match=r"empty `match`"):
        evals.load_case(path)


@pytest.mark.parametrize("version", [0, -1, True, "1", 1.0, None])
def test_only_a_known_schema_version_is_read(tmp_path, version):
    """An unknown *older* version is as unreadable as a newer one: there is no
    parser for it and no migration to run. `True` is an int in Python."""
    path = verify_case(tmp_path, schema_version=version)

    with pytest.raises(EvalCaseError, match=r"schema_version|case schema"):
        evals.load_case(path)
