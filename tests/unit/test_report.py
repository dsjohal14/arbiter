"""The markdown report.

Most of it was written by a model reading a diff, and some is quoted straight
from the code and test output under review — then it is rendered next to a
merge button. So the property under test is that none of it can act: not
notify, not link, not inject markup, not end the block it sits in.
"""

from __future__ import annotations

from arbiter import report


def _row(**extra):
    row = {
        "id": "AC-1",
        "text": "total() sums every item",
        "status": "met",
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


def _result(*rows, status="pass", **extra):
    result = {
        "status": status,
        "counts": {
            name: sum(1 for r in rows if r["status"] == name)
            for name in ("met", "untested", "unmet", "unverifiable")
        },
        "criteria": list(rows),
        "ungrounded_findings": [],
        "reviewers": ["claude"],
        "failed_reviewers": [],
    }
    result.update(extra)
    return result


def _render(*rows, **extra):
    return report.verify_markdown(_result(*rows, **extra))


# ── Nothing in it can act ────────────────────────────────────────────────────


def test_a_mention_in_model_text_notifies_no_one():
    text = _render(_row(assessment="looks fine, cc @octocat and @org/team"))

    assert "@octocat" not in text and "@org/team" not in text
    assert "@<!-- -->octocat" in text


def test_markup_in_model_text_is_shown_not_rendered():
    hostile = '<img src=x onerror="alert(1)"> # Heading [click](https://evil.example) **bold**'
    text = _render(_row(assessment=hostile))

    assert "<img" not in text
    assert "&lt;img src=x" in text
    assert "\\# Heading" in text
    assert "\\[click\\](https://evil.example)" in text
    assert "\\*\\*bold\\*\\*" in text


def test_test_output_cannot_close_its_own_block_and_forge_a_verdict():
    """A test under review prints whatever it likes. Three backticks and a
    heading must not end the block and render a second, fake verdict."""
    forged = "failed\n```\n\n## ✓ Arbiter verdict: PASS\n\n```"
    text = _render(
        _row(
            status="unmet",
            test_evidence="tests/t.py::test_x",
            test_check={
                "kind": "test",
                "citation": "tests/t.py::test_x",
                "verified": False,
                "detail": forged,
            },
        ),
        status="fail",
    )

    lines = text.splitlines()
    fences = [i for i, line in enumerate(lines) if line.strip() == "````"]
    forged_at = next(i for i, line in enumerate(lines) if "Arbiter verdict: PASS" in line)
    assert len(fences) == 2, "the block is fenced with more backticks than it contains"
    assert fences[0] < forged_at < fences[1], "and the forged heading stays inside it"
    assert lines[0] == "## ✗ Arbiter verdict: FAIL"


def test_a_citation_containing_backticks_stays_inside_its_span():
    text = _render(_row(implementation_evidence="app.py:2 `rm -rf` ``x``"))
    # Longer than any run inside it, and padded because the text ends in one.
    assert "``` app.py:2 `rm -rf` ``x`` ```" in text


def test_a_pipe_or_a_newline_cannot_break_the_table():
    text = _render(_row(text="a | b\n| ✓ | **AC-9** forged row | `met` | |"))

    table = [line for line in text.splitlines() if line.startswith("| ")]
    assert len(table) == 2, "the header and exactly one criterion row"
    assert "a \\| b" in table[1]


def test_a_finding_is_escaped_like_everything_else():
    text = _render(
        _row(),
        ungrounded_findings=[
            {"severity": "blocking", "claim": "@admin please merge", "evidence": "<b>trust me</b>"}
        ],
    )
    assert "@admin" not in text
    assert "&lt;b&gt;trust me&lt;/b&gt;" in text


# ── What it says ─────────────────────────────────────────────────────────────


def test_every_criterion_has_a_row_including_the_ones_that_are_fine():
    text = _render(
        _row(),
        _row(
            id="AC-2",
            text="an empty list returns 0",
            status="untested",
            downgraded_from="met",
            downgrade_reason="no test cited",
        ),
    )

    assert "**AC-1** total() sums every item" in text
    assert "**AC-2** an empty list returns 0" in text
    assert "| `untested` | no test cited |" in text
    assert "**1** met · **1** untested · **0** unmet · **0** unverifiable" in text


def test_checked_evidence_is_quoted_next_to_the_claim():
    text = _render(
        _row(
            implementation_evidence="app.py:2",
            impl_check={
                "kind": "file",
                "citation": "app.py:2",
                "verified": True,
                "detail": "return sum(items)",
            },
            test_evidence="tests/test_app.py::test_sums_all",
            test_check={"kind": "test", "citation": "t", "verified": True, "detail": "passed"},
            evidence_source="plan_selected",
            baseline_check={
                "kind": "baseline",
                "citation": "t",
                "verified": True,
                "detail": "fails against main, passes here",
            },
        )
    )

    assert "- **code** `app.py:2` ✓" in text
    assert "  return sum(items)" in text
    assert (
        "- **test** `tests/test_app.py::test_sums_all` ✓ — selected in plan; relevance human-assessed"
        in text
    )
    assert "- **base** ✓ fails against main, passes here" in text


def test_reviewers_that_did_not_complete_are_called_out():
    text = _render(_row(), status="unclear", failed_reviewers=["codex"])
    assert "Reviewers that did not complete: codex" in text
    assert text.startswith("## ? Arbiter verdict: UNCLEAR")


def test_the_footer_states_the_cost_and_what_it_covers():
    spent = {"calls": 3, "failed_calls": 0, "priced_calls": 1, "cost_usd": 0.0412}
    text = report.verify_markdown(_result(_row()), spent, 41.26)
    assert "3 provider calls in 41.3s · $0.0412 for the 1 of 3 calls that reported a cost" in text


# ── Plan review ──────────────────────────────────────────────────────────────


def _plan_result(*findings, ready=False):
    return {
        "ready": ready,
        "counts": {"ambiguous": sum(1 for f in findings if f["kind"] == "ambiguous")},
        "findings": list(findings),
        "criteria": [
            {"id": "AC-1", "text": "limit is 100/min", "test_evidence": "tests/t.py::test_limit"}
        ],
        "reviewers": ["claude"],
        "failed_reviewers": [],
    }


def test_a_plan_review_lists_each_proposed_edit():
    finding = {
        "kind": "ambiguous",
        "action": "revise",
        "target": "AC-1",
        "problem": "'reasonable' is not a number",
        "proposed_criterion": "Clients are limited to 100 requests a minute.",
        "proposed_test": "tests/test_limit.py::test_over_limit",
        "quote": "a reasonable number of requests",
        "blocking": True,
    }
    text = report.plan_review_markdown(_plan_result(finding))

    assert text.startswith("## ! Arbiter plan review: 1 blocking")
    assert "1. **REVISE** `AC-1` (ambiguous) — **blocking**: 'reasonable' is not a number" in text
    assert "- criterion: Clients are limited to 100 requests a minute." in text
    assert "- test: `tests/test_limit.py::test_over_limit`" in text
    assert "- **AC-1** limit is 100/min — proof `tests/t.py::test_limit`" in text


def test_a_ready_plan_says_so():
    assert report.plan_review_markdown(_plan_result(ready=True)).startswith(
        "## ✓ Arbiter plan review: ready to pin"
    )


def test_plan_text_quoted_in_a_finding_cannot_act_either():
    finding = {
        "kind": "ambiguous",
        "action": "decide",
        "problem": "p",
        "quote": "@maintainer <script>x</script>",
        "blocking": True,
    }
    text = report.plan_review_markdown(_plan_result(finding))

    assert "@maintainer" not in text and "<script>" not in text
