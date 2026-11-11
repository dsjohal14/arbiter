"""The step behind the GitHub Action, run for real off GitHub.

The action's logic lives in a script so it can be driven here, against the
fake provider, instead of being discovered broken on a runner. What is under
test is the contract a workflow depends on: the outputs, the job summary, the
exit status, and that an input can never become code.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import claude_events

BASH = shutil.which("bash")
RUN = Path(__file__).parents[2] / "action" / "run.sh"

pytestmark = pytest.mark.skipif(os.name != "posix" or BASH is None, reason="needs bash")

PYTEST = f"{sys.executable} -m pytest"

PLAN = """# Plan

<!-- arbiter:criteria -->

- **AC-1** — total() sums every item in the list  [proof: tests/test_app.py::test_sums_all]

<!-- /arbiter:criteria -->
"""

DIFF = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-a\n+b\n"


def _report(status="met"):
    row = {
        "id": "AC-1",
        "status": status,
        "assessment": "checked",
        "implementation_evidence": "app.py:2",
        "test_evidence": "tests/test_app.py::test_sums_all",
    }
    return json.dumps({"criteria": [row], "ungrounded_findings": []})


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "app.py").write_text("def total(items):\n    return sum(items)\n")
    (root / "tests" / "test_app.py").write_text(
        "from app import total\n\n\ndef test_sums_all():\n    assert total([1, 2]) == 3\n"
    )
    (root / "plan.md").write_text(PLAN)
    (root / "change.diff").write_text(DIFF)
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def step(repo, tmp_path):
    """Run the action's step the way a runner would, and read back what it left."""

    def run(**inputs):
        outputs, summary = tmp_path / "outputs.txt", tmp_path / "step-summary.md"
        (tmp_path / "runner").mkdir(exist_ok=True)
        env = {
            **os.environ,
            "ARBITER_PYTHON": sys.executable,
            "ARBITER_PLAN": "plan.md",
            "ARBITER_DIFF": "change.diff",
            "ARBITER_ARGS": f'--models claude --test-command "{PYTEST}"',
            "GITHUB_OUTPUT": str(outputs),
            "GITHUB_STEP_SUMMARY": str(summary),
            "RUNNER_TEMP": str(tmp_path / "runner"),
            **inputs,
        }
        proc = subprocess.run(
            [BASH, str(RUN)], cwd=repo, capture_output=True, text=True, env=env, check=False
        )
        written = outputs.read_text().splitlines() if outputs.exists() else []
        return proc, dict(line.split("=", 1) for line in written), summary

    return run


def test_a_passing_gate_passes_the_step(step, shim_path):
    shim_path(claude_events(_report()))

    proc, outputs, summary = step()

    assert proc.returncode == 0, proc.stderr
    assert outputs["verdict"] == "pass" and outputs["exit-code"] == "0"
    assert json.loads(Path(outputs["json"]).read_text())["status"] == "pass"
    assert summary.read_text(encoding="utf-8").startswith("## ✓ Arbiter verdict: PASS")


def test_a_failing_gate_fails_the_step_and_says_why_in_the_job_summary(step, shim_path, repo):
    (repo / "app.py").write_text("def total(items):\n    return sum(items[1:])\n")
    shim_path(claude_events(_report()))

    proc, outputs, summary = step()

    assert proc.returncode == 1
    assert outputs["verdict"] == "fail" and outputs["exit-code"] == "1"
    text = summary.read_text(encoding="utf-8")
    assert "Arbiter verdict: FAIL" in text
    assert "cited test fails" in text


def test_the_job_summary_is_added_to_not_replaced(step, shim_path, tmp_path):
    """Other steps write there too."""
    shim_path(claude_events(_report()))
    (tmp_path / "step-summary.md").write_text("## An earlier step\n")

    _, _, summary = step()

    text = summary.read_text(encoding="utf-8")
    assert text.startswith("## An earlier step\n")
    assert "Arbiter verdict: PASS" in text


def test_an_unclear_verdict_fails_the_step_unless_told_otherwise(step, shim_path):
    shim_path(claude_events(_report("unverifiable")))

    proc, outputs, _ = step()
    assert proc.returncode == 3 and outputs["verdict"] == "unclear"

    proc, outputs, _ = step(ARBITER_FAIL_ON_UNCLEAR="false")
    assert proc.returncode == 0
    assert outputs["verdict"] == "unclear", "the output still tells the truth"
    assert "::warning title=Arbiter::" in proc.stdout


def test_a_quoted_argument_reaches_arbiter_as_one_argument(step, shim_path):
    """Word-splitting `--test-command "python -m pytest"` would hand arbiter
    three arguments, and the gate would run with a broken test command."""
    shim_path(claude_events(_report()))

    proc, outputs, _ = step(
        ARBITER_ARGS=f'--models claude --require-tests --test-command "{PYTEST}"'
    )

    assert proc.returncode == 0, proc.stderr
    assert outputs["verdict"] == "pass"


def test_an_input_is_split_not_evaluated(step, shim_path, repo):
    """The input may come from an event payload. It must never become code."""
    shim_path(claude_events(_report()))

    proc, _, _ = step(
        ARBITER_ARGS=f'--models claude --test-command "{PYTEST}" $(touch pwned) `touch pwned`'
    )

    assert not (repo / "pwned").exists()
    assert proc.returncode == 2, "arbiter refuses the stray arguments; nothing ran them"


def test_arguments_that_cannot_be_parsed_stop_the_step_before_any_review(step, shim_path):
    argv_log = shim_path(claude_events(_report()))

    proc, outputs, _ = step(ARBITER_ARGS='--models claude --test-command "unterminated')

    assert proc.returncode == 2
    assert "::error title=Arbiter::The 'args' input could not be parsed" in proc.stdout
    assert outputs == {}
    assert not argv_log.exists()


def test_a_run_that_goes_wrong_is_an_error_not_a_verdict(step, shim_path):
    shim_path(claude_events(_report()))

    proc, outputs, _ = step(ARBITER_PLAN="no-such-plan.md")

    assert proc.returncode == 2
    assert outputs["verdict"] == "error" and outputs["exit-code"] == "2"


def test_a_pull_request_is_verified_over_its_own_range_by_default(step, shim_path, repo):
    """No `diff` input: the step verifies what the pull request changes, from
    where it left its base to its head."""

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()

    (repo / "app.py").write_text("def total(items):\n    return sum(items[1:])\n")
    git("init", "-q", "-b", "main", ".")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "app.py").write_text("def total(items):\n    return sum(items)\n")
    git("commit", "-qam", "fix")
    head = git("rev-parse", "HEAD")
    argv_log = shim_path(claude_events(_report()))

    proc, outputs, _ = step(ARBITER_DIFF="", ARBITER_PR_BASE=base, ARBITER_PR_HEAD=head)

    assert proc.returncode == 0, proc.stderr
    assert outputs["verdict"] == "pass"
    prompt = " ".join(json.loads(argv_log.read_text().splitlines()[0]))
    assert "+    return sum(items)" in prompt, "the reviewer was shown the pull request's diff"
