"""Which providers run, end to end through `main()`."""

from __future__ import annotations

import json
import sys

import pytest

from arbiter.cli import main
from arbiter.verify import EXIT_PASS
from conftest import agy_events

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
