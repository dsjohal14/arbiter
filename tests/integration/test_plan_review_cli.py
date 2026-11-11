"""`arbiter plan review` end to end, and the loop it completes."""

from __future__ import annotations

import json

from arbiter.cli import main
from arbiter.criteria import read_criteria
from conftest import claude_events, codex_events

RAW_PLAN = """# Plan: fix the totals bug

`total()` should be robust and handle the list properly.
"""


def _report(*findings):
    return json.dumps({"findings": list(findings)})


AMBIGUOUS = {
    "kind": "ambiguous",
    "action": "revise",
    "target": "AC-1",
    "problem": "'handle the list properly' admits several implementations",
    "proposed_criterion": "total() returns the sum of every element",
    "proposed_test": "tests/test_app.py::test_sums_all",
    "quote": "handle the list properly",
}

MISSING = {
    "kind": "missing",
    "action": "add",
    "problem": "the empty-list case is never specified",
    "proposed_criterion": "total([]) returns 0",
    "proposed_test": "tests/test_app.py::test_empty",
}

ADVISORY = {
    "kind": "unnecessary",
    "action": "remove",
    "target": "AC-2",
    "problem": "belongs in a separate change",
}

PINNED = """# Plan

<!-- arbiter:criteria -->

- **AC-1** — total() handles the list properly
- **AC-2** — logging is added

<!-- /arbiter:criteria -->
"""


def test_a_plan_with_blocking_problems_exits_nonzero(tmp_path, shim_path, monkeypatch, capsys):
    """Non-zero while blocking problems remain, so an agent skill can be told not
    to start implementing until this passes."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.md").write_text(PINNED)
    shim_path(claude_events(_report(AMBIGUOUS)))
    code = main(["plan", "review", "plan.md", "--models", "claude"])
    out = capsys.readouterr().out
    assert code == 1
    assert "BLOCKING" in out
    assert "handle the list properly" in out  # the offending plan text is quoted


def test_advisory_findings_alone_do_not_block(tmp_path, shim_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.md").write_text(PINNED)
    shim_path(claude_events(_report(ADVISORY)))
    assert main(["plan", "review", "plan.md", "--models", "claude"]) == 0
    assert "READY TO PIN" in capsys.readouterr().out


def test_a_clean_plan_is_ready(tmp_path, shim_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.md").write_text(PINNED)
    shim_path(claude_events(_report()))
    assert main(["plan", "review", "plan.md", "--models", "claude"]) == 0
    assert "ready to pin" in capsys.readouterr().out.lower()


def test_findings_are_proposed_edits_not_prose(tmp_path, shim_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.md").write_text(PINNED)
    shim_path(claude_events(_report(AMBIGUOUS, MISSING)))
    main(["plan", "review", "plan.md", "--models", "claude"])
    out = capsys.readouterr().out
    assert "REVISE" in out and "ADD" in out
    # Each carries the replacement text and the test that would prove it.
    assert "total() returns the sum of every element" in out
    assert "tests/test_app.py::test_sums_all" in out


def test_apply_writes_the_revised_contract_into_the_plan(tmp_path, shim_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.md"
    plan.write_text(PINNED)
    shim_path(claude_events(_report(AMBIGUOUS, MISSING, ADVISORY)))
    main(["plan", "review", "plan.md", "--models", "claude", "--apply"])

    criteria = read_criteria(plan.read_text())
    texts = [c.text for c in criteria]
    assert "total() returns the sum of every element" in texts  # revised
    assert "total([]) returns 0" in texts  # added
    assert "logging is added" not in texts  # removed
    assert [c.id for c in criteria] == ["AC-1", "AC-2"]  # renumbered


def test_apply_carries_the_proving_tests_into_the_contract(tmp_path, shim_path, monkeypatch):
    """This is what makes `verify --require-pinned-evidence` affordable: the
    proof is named at plan time, before any code exists to pick it to fit."""
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.md"
    plan.write_text(PINNED)
    shim_path(claude_events(_report(AMBIGUOUS, MISSING)))
    main(["plan", "review", "plan.md", "--models", "claude", "--apply"])

    proofs = {c.test_evidence for c in read_criteria(plan.read_text())}
    assert "tests/test_app.py::test_sums_all" in proofs
    assert "tests/test_app.py::test_empty" in proofs


def test_an_unpinned_plan_can_still_be_reviewed(tmp_path, shim_path, monkeypatch):
    """The common case extracts a complete baseline before proposing edits."""
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "raw.md"
    plan.write_text(RAW_PLAN)
    combined = json.dumps(
        {
            "criteria": ["total() handles every supplied list"],
            "findings": [MISSING],
        }
    )
    # The fake returns the same content to extraction, review, and consolidation;
    # each phase reads only the key its schema requested.
    shim_path(claude_events(combined))
    assert main(["plan", "review", "raw.md", "--models", "claude", "--apply"]) == 0
    texts = [c.text for c in read_criteria(plan.read_text())]
    assert texts == ["total() handles every supplied list", "total([]) returns 0"]


def test_plan_review_record_includes_extraction_and_final_result(
    tmp_path, shim_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "raw.md"
    plan.write_text(RAW_PLAN)
    record_path = tmp_path / "review.json"
    combined = json.dumps(
        {
            "criteria": ["total() handles every supplied list"],
            "findings": [MISSING],
        }
    )
    shim_path(claude_events(combined))

    code = main(
        [
            "plan",
            "review",
            "raw.md",
            "--models",
            "claude",
            "--json",
            "--record",
            str(record_path),
        ]
    )
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    record = json.loads(record_path.read_text())

    assert code == 0
    assert record["command"] == "plan review"
    assert record["inputs"]["plan"]["content"] == RAW_PLAN
    # What was printed is the recorded result, plus what the run took.
    assert record["usage"] == output.pop("usage")
    assert record["result"] == output
    # One reviewer, so nothing was sent to be merged with itself.
    assert [call["phase"] for call in record["provider_calls"]] == ["extractor", "reviewer"]
    assert all(call["content"] == combined for call in record["provider_calls"])


def test_unparseable_reviewer_output_cannot_report_a_plan_ready(tmp_path, shim_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.md").write_text(PINNED)
    shim_path(claude_events("I reviewed it; looks fine."))
    assert main(["plan", "review", "plan.md", "--models", "claude"]) == 4


def test_a_valid_empty_consolidation_can_clear_raw_findings(
    tmp_path, shim_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.md").write_text(PINNED)
    shim_path(
        per_provider={
            "codex": codex_events(_report(AMBIGUOUS)),
            "claude": claude_events(_report()),
        }
    )
    always = ["--consolidate", "always"]
    code = main(["plan", "review", "plan.md", "--models", "codex", "--judge", "claude", *always])
    assert code == 0
    assert "ready to pin" in capsys.readouterr().out.lower()


def test_one_reviewer_is_not_sent_to_be_merged_with_itself(tmp_path, shim_path, monkeypatch):
    """With one critique there are no two phrasings to merge. The call was made
    anyway, to the most expensive model on the panel, after the reviewer had
    already finished. Skipping it never hides a finding: the reviewer's own are
    kept as given."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.md").write_text(PINNED)
    argv_log = shim_path(
        per_provider={
            "codex": codex_events(_report(AMBIGUOUS)),
            "claude": claude_events(_report()),
        }
    )

    code = main(["plan", "review", "plan.md", "--models", "codex", "--judge", "claude"])

    assert code == 1, "the reviewer's blocking finding stands"
    assert len(argv_log.read_text().splitlines()) == 1


def test_reviewers_with_nothing_to_say_are_not_consolidated(tmp_path, shim_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.md").write_text(PINNED)
    argv_log = shim_path(
        per_provider={"codex": codex_events(_report()), "claude": claude_events(_report())}
    )

    assert main(["plan", "review", "plan.md", "--models", "claude,codex"]) == 0
    assert len(argv_log.read_text().splitlines()) == 2


def test_reviewers_who_each_found_something_are_still_merged(tmp_path, shim_path, monkeypatch):
    """Two reviewers phrase one problem two ways; telling them apart is a judgement."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.md").write_text(PINNED)
    argv_log = shim_path(
        per_provider={
            "codex": codex_events(_report(AMBIGUOUS)),
            "claude": claude_events(_report(MISSING)),
        }
    )

    main(["plan", "review", "plan.md", "--models", "claude,codex", "--judge", "claude"])

    assert len(argv_log.read_text().splitlines()) == 3


def test_an_incomplete_reviewer_keeps_its_usable_findings(tmp_path, shim_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.md").write_text(PINNED)
    bad_target = {
        **AMBIGUOUS,
        "target": "AC-999",
        "problem": "this target was hallucinated",
    }
    shim_path(
        per_provider={
            "codex": codex_events(_report(AMBIGUOUS, MISSING, bad_target)),
            "claude": claude_events(_report(AMBIGUOUS, MISSING)),
        }
    )
    code = main(
        [
            "plan",
            "review",
            "plan.md",
            "--models",
            "codex",
            "--judge",
            "claude",
            "--json",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 1  # the incomplete reviewer still prevents READY
    assert payload["failed_reviewers"] == ["codex"]
    assert {finding["problem"] for finding in payload["findings"]} == {
        AMBIGUOUS["problem"],
        MISSING["problem"],
    }
    assert "incomplete review" in captured.err


def test_json_output_is_machine_readable(tmp_path, shim_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.md").write_text(PINNED)
    shim_path(claude_events(_report(AMBIGUOUS, MISSING)))
    code = main(["plan", "review", "plan.md", "--models", "claude", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["ready"] is False
    assert payload["counts"]["ambiguous"] == 1
    assert payload["findings"][0]["blocking"] is True
    assert [c["text"] for c in payload["criteria"]]


def test_a_missing_plan_file_is_a_usage_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["plan", "review", "nope.md"]) == 2


def test_an_empty_plan_is_a_usage_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "empty.md").write_text("   \n")
    assert main(["plan", "review", "empty.md"]) == 2


def test_a_failed_reviewer_prevents_a_ready_verdict(tmp_path, shim_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.md").write_text(PINNED)
    shim_path(per_provider={"claude": claude_events(_report())})
    record_path = tmp_path / "failed-reviewer.json"
    code = main(
        [
            "plan",
            "review",
            "plan.md",
            "--models",
            "claude,codex",
            "--json",
            "--record",
            str(record_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["ready"] is False
    assert payload["failed_reviewers"] == ["codex"]
    record = json.loads(record_path.read_text())
    failed = [call for call in record["provider_calls"] if call["status"] == "failed"]
    # Tried, then tried once more, and both attempts are on the record.
    assert [call["attempt"] for call in failed] == [1, 2]
    assert {call["phase"] for call in failed} == {"reviewer"}
    assert {call["role"] for call in failed} == {"codex"}
    assert failed[-1]["error"]["type"] == "ModelCallError"
