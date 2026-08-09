"""Record → replay, through `main()`.

The round trip is the product: a run that cost money once has to be re-derivable
for free, with no provider installed and nothing spent.
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


# ── replay ────────────────────────────────────────────────────────────────────


def test_replay_reproduces_the_recorded_verdict_without_calling_anything(
    recorded, capsys, monkeypatch
):
    path, code = recorded()
    capsys.readouterr()
    # Nothing on PATH: a replay that tried to reach a provider would fail here.
    monkeypatch.setenv("PATH", "")

    replayed = main(["replay", str(path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_FAIL
    assert replayed == code
    assert payload["matches"] is True
    assert payload["drift"] == []
    assert payload["replayed"] == payload["recorded"]


def test_replay_renders_the_verdict_and_says_what_it_did_not_recheck(recorded, capsys):
    path, _ = recorded()
    capsys.readouterr()

    main(["replay", str(path)])
    captured = capsys.readouterr()

    assert "VERDICT: FAIL" in captured.out
    assert "AC-1" in captured.out
    assert "no providers were called" in captured.out.lower()
    assert "restored from the record" in captured.out
    assert "reproduces the recorded result" in captured.out


def test_check_exits_zero_only_while_the_replay_still_reproduces_the_record(recorded, capsys):
    path, _ = recorded()
    capsys.readouterr()

    assert main(["replay", str(path), "--check"]) == EXIT_PASS

    record = json.loads(path.read_text())
    record["result"]["status"] = "pass"
    record["result"]["criteria"][0]["status"] = "met"
    path.write_text(json.dumps(record))
    capsys.readouterr()

    assert main(["replay", str(path), "--check"]) == EXIT_FAIL
    captured = capsys.readouterr()
    assert "verdict: recorded pass, replayed fail" in captured.out


def test_replaying_something_that_is_not_a_record_is_a_usage_error(repo, capsys):
    (repo / "notes.txt").write_text("hello")

    assert main(["replay", str(repo / "notes.txt")]) == EXIT_USAGE
    assert "not valid JSON" in capsys.readouterr().err
