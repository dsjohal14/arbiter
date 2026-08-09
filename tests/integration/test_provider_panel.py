"""Which providers run, and what a run costs.

Two behaviours that only show up end to end: the default panel adapting to what
is actually installed, and a CLI that blocks on an interactive question being
diagnosed as that rather than as a slow model.
"""

from __future__ import annotations

import json
import sys

import pytest

from arbiter.cli import main
from arbiter.models import DEFAULT_PANEL, OPT_IN_PROVIDERS
from arbiter.verify import EXIT_PASS, EXIT_RUNTIME, EXIT_UNCLEAR, EXIT_USAGE
from conftest import agy_events, claude_events, codex_events, gemini_events

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

<!-- /arbiter:criteria -->
"""


def _review(*criteria):
    return json.dumps({"criteria": list(criteria), "ungrounded_findings": []})


def _ac(cid, status, impl=None, test=None):
    return {
        "id": cid,
        "status": status,
        "assessment": "checked",
        "implementation_evidence": impl,
        "test_evidence": test,
    }


REPORT = _review(_ac("AC-1", "met", "app.py:2", "tests/test_app.py::test_sums_all"))


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


def _verify(*extra: str) -> int:
    return main(
        ["verify", "--plan", "plan.md", "--diff", "change.diff", "--test-command", PYTEST, *extra]
    )


# ── antigravity end to end ───────────────────────────────────────────────────


def test_antigravity_can_carry_a_whole_verification(repo, shim_path, capsys):
    """Its stream is keyed on `event` rather than `type`, so nothing about
    reading it is shared with the other three."""
    shim_path(per_provider={"agy": agy_events(REPORT)})

    code = _verify("--models", "antigravity", "--judge", "antigravity", "--json")
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_PASS
    assert payload["reviewers"] == ["antigravity"]
    assert payload["criteria"][0]["status"] == "met"


# ── Token usage ──────────────────────────────────────────────────────────────


def test_usage_reaches_the_run_record(repo, shim_path):
    record = repo / "run.json"
    shim_path(per_provider={"agy": agy_events(REPORT)})

    _verify("--models", "antigravity", "--judge", "antigravity", "--record", str(record))

    calls = json.loads(record.read_text())["provider_calls"]
    assert calls, "nothing was recorded"
    for call in calls:
        assert call["usage"] == {
            "input_tokens": 1200,
            "output_tokens": 42,
            "cached_input_tokens": 0,
            "cache_write_tokens": None,
            "reasoning_tokens": 7,
            "total_tokens": 1242,
            "cost_usd": None,
        }


def test_a_provider_reporting_no_usage_records_null_not_zero(repo, shim_path):
    """Nothing measured and nothing consumed have to stay distinguishable, or a
    cost report quietly averages in runs it knows nothing about."""
    record = repo / "run.json"
    shim_path(per_provider={"gemini": gemini_events(REPORT)})

    _verify("--models", "gemini", "--judge", "gemini", "--record", str(record))

    calls = json.loads(record.read_text())["provider_calls"]
    assert calls and all(call["usage"] is None for call in calls)


# ── The default panel ────────────────────────────────────────────────────────


ALL_SHIMMED = {
    "claude": claude_events(REPORT),
    "codex": codex_events(REPORT),
    "gemini": gemini_events(REPORT),
    "agy": agy_events(REPORT),
}


def test_the_default_panel_is_the_default_providers_that_are_installed(repo, shim_path, capsys):
    shim_path(per_provider=ALL_SHIMMED)

    code = _verify("--json")
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_PASS
    assert sorted(payload["reviewers"]) == sorted(DEFAULT_PANEL)


def test_an_opt_in_provider_stays_off_the_panel_even_when_installed(repo, shim_path, capsys):
    """Installed is not the same as wanted. Gemini answers here and still must
    not be reviewing anything nobody asked it to review."""
    shim_path(per_provider=ALL_SHIMMED)

    _verify("--json")
    payload = json.loads(capsys.readouterr().out)

    for key in OPT_IN_PROVIDERS:
        assert key not in payload["reviewers"]
        assert key not in payload["failed_reviewers"]


def test_an_opt_in_provider_still_works_when_you_name_it(repo, shim_path, capsys):
    """Opt-in means not assumed, not unsupported."""
    shim_path(per_provider=ALL_SHIMMED)

    code = _verify("--models", "gemini", "--judge", "claude", "--json")
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_PASS
    assert payload["reviewers"] == ["gemini"]


def test_a_provider_that_is_not_installed_is_named_and_skipped(repo, shim_path, capsys):
    """Before this, the default panel was every known provider, so the ones you
    had never installed each failed a call and turned every default run
    unclear — which is what adding a fourth provider would have done to everyone."""
    shim_path(
        per_provider={"claude": claude_events(REPORT), "codex": codex_events(REPORT)},
        installed=("claude", "codex"),
    )

    code = _verify("--json")
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == EXIT_PASS
    assert sorted(payload["reviewers"]) == ["claude", "codex"]
    assert payload["failed_reviewers"] == []
    assert "not on this panel" in captured.err
    assert "antigravity" in captured.err
    # An opt-in provider is absent by choice, not by accident; nagging about it
    # on every run would train the user to ignore the line.
    assert "gemini" not in captured.err


def test_naming_a_provider_you_do_not_have_still_fails_loudly(repo, shim_path, capsys):
    """A reviewer you asked for and did not get is a missing opinion, so the
    verdict must not be a clean pass."""
    shim_path(per_provider={"claude": claude_events(REPORT)}, installed=("claude",))

    code = _verify("--models", "claude,antigravity", "--judge", "claude", "--json")
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_UNCLEAR
    assert payload["failed_reviewers"] == ["antigravity"]


def test_no_provider_at_all_is_a_usage_error(repo, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    assert _verify() == EXIT_USAGE
    err = capsys.readouterr().err
    assert "No default provider CLI found" in err
    # Name what to install, not every provider that exists.
    assert all(k in err for k in DEFAULT_PANEL)


# ── Diagnosing a CLI that is waiting for a human ─────────────────────────────


def test_a_cli_blocked_on_a_login_prompt_says_so_instead_of_timing_out(repo, shim_path, capsys):
    """The real report was `gemini exceeded the 60s timeout`, which sends you
    looking for a slow model. It was sitting on an unanswerable auth question."""
    shim_path(
        per_provider={"claude": claude_events(REPORT)},
        prose="Opening authentication page in your browser. Do you want to continue? [Y/n]:",
        hang=30,
    )

    code = _verify("--models", "gemini", "--judge", "claude", "--timeout", "2")
    captured = capsys.readouterr()

    assert code == EXIT_RUNTIME
    combined = captured.out + captured.err
    assert "authentication page" in combined
    assert "waiting for an answer" in combined
    assert "interactively" in combined
