"""Retrying a provider call that may have failed for a passing reason.

Without this, one overloaded provider turns a whole verification into `unclear`
— and rerunning it pays again for every call that worked. The limits matter as
much as the retry: a failure that cannot end differently must not be repeated.
"""

from __future__ import annotations

import json
import sys
import time

import pytest

from arbiter.cli import main
from arbiter.models import ModelCallError, call_session, make_session, worth_retrying
from arbiter.verify import EXIT_PASS, EXIT_RUNTIME, EXIT_USAGE
from conftest import claude_events, codex_events

PYTEST = f"{sys.executable} -m pytest"


def _session(provider="claude", retries=1):
    session = make_session(provider, provider)
    session.retries = retries
    return session


def _calls(argv_log):
    return [json.loads(line) for line in argv_log.read_text().splitlines()]


# ── The transport ────────────────────────────────────────────────────────────


async def test_a_call_that_fails_once_is_tried_again_and_succeeds(shim_path):
    argv_log = shim_path(claude_events("recovered"), fail_first=1)

    response = await call_session(_session(), "q")

    assert response.content == "recovered"
    assert len(_calls(argv_log)) == 2
    assert [str(e) for e in response.failed_attempts] == [
        "claude exited 1: upstream overloaded, try again"
    ]


async def test_without_retries_the_first_failure_stands(shim_path):
    argv_log = shim_path(claude_events("never seen"), fail_first=1)

    with pytest.raises(ModelCallError, match="upstream overloaded"):
        await call_session(_session(retries=0), "q")
    assert len(_calls(argv_log)) == 1


async def test_retries_are_bounded_and_the_final_failure_remembers_the_rest(shim_path):
    argv_log = shim_path(claude_events("never seen"), fail_first=10)

    with pytest.raises(ModelCallError) as failure:
        await call_session(_session(retries=2), "q")

    assert len(_calls(argv_log)) == 3
    assert len(failure.value.earlier) == 2


async def test_a_timeout_is_not_retried(shim_path):
    """It is not a failure to answer but an answer that took longer than it was
    given. Trying again spends the whole budget twice for the same outcome."""
    argv_log = shim_path(hang=60)
    started = time.monotonic()

    with pytest.raises(ModelCallError, match="timeout"):
        await call_session(_session(retries=3), "q", timeout=1.0)

    assert len(_calls(argv_log)) == 1
    assert time.monotonic() - started < 10


async def test_a_provider_that_wants_a_login_is_not_retried(shim_path):
    argv_log = shim_path(claude_events("x"), exit_code=1, stderr="Please log in to continue")

    with pytest.raises(ModelCallError, match="log in"):
        await call_session(_session(retries=3), "q")
    assert len(_calls(argv_log)) == 1


async def test_a_missing_cli_is_not_retried(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    attempts = []

    with pytest.raises(ModelCallError, match="not installed"):
        await call_session(_session(retries=3), "q", on_retry=lambda *event: attempts.append(event))
    assert attempts == []


async def test_a_retry_does_not_reuse_a_session_id_the_provider_may_have_taken(shim_path):
    """The id was handed over for a call that then failed. Offering the same one
    again risks "session already exists" turning one failure into two."""
    argv_log = shim_path(claude_events("ok"), fail_first=1)

    await call_session(_session(), "q")

    first, second = (c[c.index("--session-id") + 1] for c in _calls(argv_log))
    assert first != second


async def test_a_session_already_under_way_keeps_its_id_across_a_retry(shim_path):
    """Its context lives under that id; a new one would start the conversation over."""
    argv_log = shim_path(codex_events("first"))
    session = _session("codex")
    await call_session(session, "q1")

    shim_path(codex_events("second"), fail_first=1)
    await call_session(session, "q2")

    resumed = [c for c in _calls(argv_log) if "resume" in c]
    assert len(resumed) == 2
    assert {c[c.index("resume") + 1] for c in resumed} == {"thread-1"}


async def test_the_caller_is_told_about_each_retry(shim_path):
    shim_path(claude_events("ok"), fail_first=2)
    seen = []

    await call_session(
        _session(retries=2), "q", on_retry=lambda key, n, error: seen.append((key, n, str(error)))
    )

    assert [(key, n) for key, n, _ in seen] == [("claude", 1), ("claude", 2)]
    assert all("upstream overloaded" in message for _, _, message in seen)


@pytest.mark.parametrize(
    ("message", "retry"),
    [
        ("codex exited 1: upstream overloaded", True),
        ("claude: 529 overloaded_error", True),
        ("claude returned no content.", True),
        ("claude: Invalid API key · Please run /login", False),
        ("codex: 401 Unauthorized", False),
        ("gemini exited 1: Please sign in to continue", False),
        ("codex: requires a newer version of Codex", False),
        # "log in" inside another word is not a login prompt.
        ("claude exited 1: could not read catalog index", True),
    ],
)
def test_only_a_failure_that_could_end_differently_is_retried(message, retry):
    assert worth_retrying(ModelCallError(message)) is retry


# ── Through the command line ─────────────────────────────────────────────────

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
def repo(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("def total(items):\n    return sum(items)\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "from app import total\n\n\ndef test_sums_all():\n    assert total([1, 2]) == 3\n"
    )
    (tmp_path / "plan.md").write_text(PLAN)
    (tmp_path / "change.diff").write_text(DIFF)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _verify(*extra):
    return main(
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
            *extra,
        ]
    )


def test_a_reviewer_that_fails_once_does_not_cost_the_run_its_verdict(repo, shim_path):
    shim_path(claude_events(REPORT), fail_first=1)

    assert _verify("--record", "run.json") == EXIT_PASS

    calls = json.loads((repo / "run.json").read_text())["provider_calls"]
    reviewer = [(c["attempt"], c["status"]) for c in calls if c["phase"] == "reviewer"]
    assert reviewer == [(1, "failed"), (2, "completed")], "the failed attempt is still a call"


def test_retries_can_be_switched_off(repo, shim_path):
    shim_path(claude_events(REPORT), fail_first=1)
    assert _verify("--retries", "0") == EXIT_RUNTIME


def test_the_project_file_can_set_retries(repo, shim_path):
    shim_path(claude_events(REPORT), fail_first=1)
    (repo / ".arbiter.toml").write_text("retries = 0\n")
    assert _verify() == EXIT_RUNTIME
    assert _verify("--retries", "1") == EXIT_PASS


def test_a_negative_retry_count_is_a_usage_error(repo, shim_path, capsys):
    shim_path(claude_events(REPORT))
    assert _verify("--retries", "-1") == EXIT_USAGE
    assert "--retries cannot be negative" in capsys.readouterr().err
