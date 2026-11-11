"""What a run prints, on a terminal and into a pipe.

Reviewers answer in JSON meant for a parser. Streaming that at a person told
them nothing, and it filled CI logs with it. These run the real program, once
attached to a pseudo-terminal and once to a pipe, because which of the two it
is talking to is the whole decision.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from conftest import claude_events

PYTEST = f"{sys.executable} -m pytest"

PLAN = """# Plan

<!-- arbiter:criteria -->

- **AC-1** — total() sums every item in the list

<!-- /arbiter:criteria -->
"""

DIFF = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-a\n+b\n"

REPORT = json.dumps(
    {
        "criteria": [
            {
                "id": "AC-1",
                "status": "met",
                "assessment": "checked",
                "implementation_evidence": "app.py:2",
                "test_evidence": "tests/test_app.py::test_sums_all",
            }
        ],
        "ungrounded_findings": [],
    }
)


@pytest.fixture
def repo(tmp_path, monkeypatch, shim_path):
    (tmp_path / "app.py").write_text("def total(items):\n    return sum(items)\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "from app import total\n\n\ndef test_sums_all():\n    assert total([1, 2]) == 3\n"
    )
    (tmp_path / "plan.md").write_text(PLAN)
    (tmp_path / "change.diff").write_text(DIFF)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    shim_path(claude_events(REPORT))
    return tmp_path


def _command(*extra):
    return [
        sys.executable,
        "-m",
        "arbiter",
        "verify",
        "--plan",
        "plan.md",
        "--diff",
        "change.diff",
        "--models",
        "claude",
        "--test-command",
        PYTEST,
        *extra,
    ]


def _piped(*extra):
    return subprocess.run(_command(*extra), capture_output=True, text=True, check=False)


@pytest.mark.skipif(os.name != "posix", reason="needs a pseudo-terminal")
def test_on_a_terminal_the_run_is_a_board_and_the_cursor_comes_back(repo):
    import pty

    controller, terminal = pty.openpty()
    proc = subprocess.Popen(_command(), stdout=terminal, stderr=subprocess.PIPE, close_fds=True)
    os.close(terminal)
    chunks = []
    while True:
        try:
            chunk = os.read(controller, 65536)
        except OSError:  # the far end closed
            break
        if not chunk:
            break
        chunks.append(chunk)
    proc.wait(timeout=60)
    os.close(controller)
    screen = b"".join(chunks).decode("utf-8", errors="replace")

    assert proc.returncode == 0
    assert "\033[?25l" in screen, "the cursor should be hidden while the board is up"
    assert screen.rindex("\033[?25h") > screen.rindex("\033[?25l"), "and given back afterwards"
    assert "reviewed" in screen and "evidence" in screen
    assert "VERDICT: PASS" in screen
    assert '"ungrounded_findings"' not in screen, "raw reviewer JSON is not for people"


def test_into_a_pipe_the_run_is_plain_lines_with_no_escape_codes(repo):
    """A CI log is a pipe. It gets one line per step and nothing that only a
    terminal could interpret."""
    result = _piped()

    assert result.returncode == 0
    assert "\033" not in result.stdout
    assert "claude (sonnet) reviewed in" in result.stdout
    assert "consolidation skipped: one reviewer" in result.stdout
    assert "evidence checked: 1 cited line read, 1 test run" in result.stdout
    assert '"ungrounded_findings"' not in result.stdout


def test_verbose_still_shows_the_raw_streams_and_commands(repo):
    """That is what -v is for: seeing exactly what a provider was sent and said."""
    result = _piped("-v")

    assert result.returncode == 0
    assert '"ungrounded_findings"' in result.stdout
    assert "$ claude -p" in result.stdout


def test_json_mode_keeps_stdout_for_the_result_alone(repo):
    result = _piped("--json")

    payload = json.loads(result.stdout)
    assert payload["status"] == "pass"
    assert "reviewed in" in result.stderr, "progress moves to stderr, it does not vanish"


def test_a_run_ends_by_saying_what_it_took(repo):
    result = _piped()
    assert "1 provider call in " in result.stdout.splitlines()[-2]


def test_json_carries_the_usage_beside_the_result(repo, tmp_path):
    result = _piped("--json", "--record", "run.json")

    payload = json.loads(result.stdout)
    assert payload["usage"]["calls"] == 1
    assert payload["usage"]["providers"][0]["label"] == "claude (sonnet)"
    record = json.loads((tmp_path / "run.json").read_text())
    assert record["usage"] == payload["usage"]
    assert "usage" not in record["result"], "a verdict is replayable; what it cost is not"


def test_a_summary_file_holds_the_verdict_as_markdown(repo, tmp_path):
    result = _piped("--summary", "summary.md")

    assert result.returncode == 0
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert summary.startswith("## ✓ Arbiter verdict: PASS")
    assert "**AC-1** total() sums every item in the list" in summary
    assert "1 provider call in " in summary


def test_a_summary_that_cannot_be_written_does_not_change_the_verdict(repo):
    """The verdict is the contract, and it was reached. The missing file is
    said loudly instead."""
    result = _piped("--summary", "no/such/directory/summary.md")

    assert result.returncode == 0
    assert "Could not write the summary" in result.stderr
