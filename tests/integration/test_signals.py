"""SIGTERM is how a CI system cancels a job.

Python's default is to die where it stands, running no `finally` — so every
superseded run would leave its provider CLIs running and its temporary worktree
registered. These run the real program in a real process and signal it, because
none of that is observable from inside `main()`.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from conftest import claude_events

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX signals")

PYTEST = f"{sys.executable} -m pytest"

PLAN = """# Plan

<!-- arbiter:criteria -->

- **AC-1** — total() sums every item in the list

<!-- /arbiter:criteria -->
"""

DIFF = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-a\n+b\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("def total(items):\n    return sum(items)\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "plan.md").write_text(PLAN)
    (tmp_path / "change.diff").write_text(DIFF)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _start():
    return subprocess.Popen(
        [
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
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for(path, what, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return int(path.read_text())
        time.sleep(0.05)
    raise AssertionError(f"{what} never started")


def _gone(pid, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_sigterm_during_a_provider_call_reaps_the_provider(repo, shim_path, tmp_path):
    shim_path(hang=120)
    arbiter = _start()
    try:
        provider = _wait_for(tmp_path / "provider.pid", "the provider CLI")

        arbiter.send_signal(signal.SIGTERM)
        _, err = arbiter.communicate(timeout=20)
    finally:
        arbiter.kill()

    assert arbiter.returncode == 128 + signal.SIGTERM
    assert "Terminated." in err
    assert _gone(provider), "the provider CLI was left running"


def test_sigterm_during_a_cited_test_stops_the_test(repo, shim_path):
    """Evidence checks block on the test they run, so nothing but the signal
    itself can interrupt one."""
    (repo / "tests" / "test_slow.py").write_text(
        "import os\nimport time\nfrom pathlib import Path\n\n\n"
        "def test_hangs():\n"
        "    Path('test.pid').write_text(str(os.getpid()))\n"
        "    time.sleep(120)\n"
    )
    report = {
        "criteria": [
            {
                "id": "AC-1",
                "status": "met",
                "assessment": "checked",
                "implementation_evidence": "app.py:2",
                "test_evidence": "tests/test_slow.py::test_hangs",
            }
        ],
        "ungrounded_findings": [],
    }
    shim_path(claude_events(json.dumps(report)))
    arbiter = _start()
    try:
        test = _wait_for(repo / "test.pid", "the cited test")

        arbiter.send_signal(signal.SIGTERM)
        _, err = arbiter.communicate(timeout=20)
    finally:
        arbiter.kill()

    assert arbiter.returncode == 128 + signal.SIGTERM
    assert "Terminated." in err
    assert _gone(test), "the cited test was left running"
