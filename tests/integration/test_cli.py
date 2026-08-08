"""End-to-end tests of the CLI, driven through the fake provider CLIs.

The exit-code contract is the product here — it is what lets `arbiter verify`
sit in a pre-commit hook or a CI job — so it is tested through `main()` rather
than by calling internals.
"""

from __future__ import annotations

import json

import pytest

from arbiter.cli import main
from arbiter.verify import EXIT_FAIL, EXIT_PASS, EXIT_UNCLEAR, EXIT_USAGE
from conftest import claude_events

DIFF = """diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1,3 +1,3 @@
 def total(items):
-    return sum(items)
+    return sum(items[1:])
"""


def _report(findings=(), unevaluable=()):
    return json.dumps({"findings": list(findings), "unevaluable_criteria": list(unevaluable)})


BLOCKING = {
    "severity": "blocking",
    "claim": "total() now skips the first item",
    "acceptance_criterion": "Sum every item",
    "evidence": "app.py:3",
    "validation_command": "pytest tests/test_total.py",
    "status": "confirmed",
    "raised_by": ["A", "B"],
}

UNRESOLVED_BLOCKING = {**BLOCKING, "status": "unresolved", "raised_by": ["A"]}


@pytest.fixture
def gate(tmp_path, shim_path, monkeypatch):
    """Run `arbiter verify` with every provider replying with `report`."""
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n- Sum every item in the list.\n")
    diff = tmp_path / "change.diff"
    diff.write_text(DIFF)

    def run(report: str, *extra: str) -> int:
        shim_path(claude_events(report))
        return main(
            ["verify", "--plan", str(plan), "--diff", str(diff), "--models", "claude", *extra]
        )

    return run


def test_clean_change_passes_with_exit_0(gate, capsys):
    assert gate(_report()) == EXIT_PASS
    assert "PASS" in capsys.readouterr().out


def test_confirmed_blocking_finding_fails_with_exit_1(gate, capsys):
    assert gate(_report([BLOCKING])) == EXIT_FAIL
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "skips the first item" in out
    assert "app.py:3" in out  # evidence is surfaced, not just the claim


def test_unresolved_blocking_is_unclear_with_exit_3(gate):
    """A serious finding nobody could settle must not silently pass."""
    assert gate(_report([UNRESOLVED_BLOCKING])) == EXIT_UNCLEAR


def test_unevaluable_criteria_are_unclear_with_exit_3(gate):
    assert gate(_report(unevaluable=["Criterion 2: performance under load"])) == EXIT_UNCLEAR


def test_unevidenced_finding_cannot_fail_the_gate(gate):
    """A blocking claim with no evidence is dropped, so it cannot block a merge."""
    bare = {"severity": "blocking", "claim": "feels wrong", "evidence": "", "status": "confirmed"}
    assert gate(_report([bare])) == EXIT_PASS


def test_json_output_is_machine_readable(gate, capsys):
    code = gate(_report([BLOCKING]), "--json")
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_FAIL
    assert payload["status"] == "fail"
    assert payload["exit_code"] == EXIT_FAIL
    assert payload["counts"]["blocking"] == 1
    assert payload["findings"][0]["evidence"] == "app.py:3"
    # The blinding record makes the run reproducible and attributable.
    assert payload["blinding"]["seed"] is not None
    assert payload["blinding"]["label_to_model"]


def test_seed_makes_a_run_reproducible(gate, capsys):
    gate(_report([BLOCKING]), "--json", "--seed", "4242")
    first = json.loads(capsys.readouterr().out)
    gate(_report([BLOCKING]), "--json", "--seed", "4242")
    second = json.loads(capsys.readouterr().out)
    assert first["blinding"] == second["blinding"]


# ── Usage errors ─────────────────────────────────────────────────────────────


def test_unknown_provider_is_a_usage_error_not_a_silent_drop(tmp_path, capsys):
    """`--models claude,gpt4` used to quietly run a one-model 'debate'. It must
    now exit 2 — not 1, which would be indistinguishable from a `fail` verdict."""
    plan = tmp_path / "p.md"
    plan.write_text("# Plan\n")
    with pytest.raises(SystemExit) as e:
        main(["verify", "--plan", str(plan), "--models", "claude,gpt4"])
    assert e.value.code == EXIT_USAGE
    assert "gpt4" in capsys.readouterr().err


def test_unknown_judge_is_a_usage_error(tmp_path):
    plan = tmp_path / "p.md"
    plan.write_text("# Plan\n")
    with pytest.raises(SystemExit) as e:
        main(["verify", "--plan", str(plan), "--judge", "bogus"])
    assert e.value.code == EXIT_USAGE


def test_missing_plan_file_is_a_usage_error(tmp_path):
    assert main(["verify", "--plan", str(tmp_path / "nope.md")]) == EXIT_USAGE


def test_empty_diff_is_a_usage_error(tmp_path):
    plan = tmp_path / "p.md"
    plan.write_text("# Plan\n")
    empty = tmp_path / "empty.diff"
    empty.write_text("")
    assert main(["verify", "--plan", str(plan), "--diff", str(empty)]) == EXIT_USAGE


def test_no_subcommand_exits_with_usage(capsys):
    with pytest.raises(SystemExit) as e:
        main([])
    assert e.value.code == EXIT_USAGE


# ── Debate ───────────────────────────────────────────────────────────────────


def test_debate_runs_non_interactively_and_emits_json(shim_path, capsys):
    verdict = "WINNER: A\n\nREASON: Clearer.\n\nBEST ANSWER:\nUse a heap."
    shim_path(claude_events(verdict))
    code = main(["debate", "sorting?", "--models", "claude", "--json", "--no-followup"])
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_PASS
    assert payload["best_answer"] == "Use a heap."
    # The winner is reported as a model key, resolved back through the blinding.
    assert payload["winner"] == "claude"


def test_debate_transcript_is_written_as_utf8(shim_path, tmp_path):
    shim_path(claude_events("WINNER: A\n\nREASON: r — em dash\n\nBEST ANSWER:\nx ✓"))
    out = tmp_path / "t.md"
    main(["debate", "q", "--models", "claude", "--no-followup", "--save", str(out)])
    assert "✓" in out.read_text(encoding="utf-8")
