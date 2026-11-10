"""Project settings, through `main()`.

Precedence is the contract: a flag beats the project file, which beats the
built-in default. And nothing a file says may be quietly dropped.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

import arbiter
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

- **AC-1** — total() sums every item in the list

<!-- /arbiter:criteria -->
"""


def _report(status, test=None):
    row = {
        "id": "AC-1",
        "status": status,
        "assessment": "checked",
        "implementation_evidence": "app.py:2",
        "test_evidence": test,
    }
    return json.dumps({"criteria": [row], "ungrounded_findings": []})


UNTESTED = _report("untested")
MET = _report("met", "tests/test_app.py::test_sums_all")
MET_BY_A_FAILING_TEST = _report("met", "tests/test_app.py::test_broken")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("def total(items):\n    return sum(items)\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "from app import total\n\n\n"
        "def test_sums_all():\n    assert total([1, 2]) == 3\n\n\n"
        "def test_broken():\n    assert total([1, 2]) == 99\n"
    )
    (tmp_path / "plan.md").write_text(PINNED_PLAN)
    (tmp_path / "change.diff").write_text(DIFF)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _settings(repo, body: str, name: str = ".arbiter.toml"):
    (repo / name).write_text(body)


def _verify(*extra):
    return main(["verify", "--diff", "change.diff", "--models", "claude", *extra])


# ── Precedence ───────────────────────────────────────────────────────────────


def test_the_project_file_can_supply_the_plan(repo, shim_path):
    """What lets a pre-commit hook and a CI job be one short line each."""
    shim_path(claude_events(MET))
    _settings(repo, 'plan = "plan.md"\n')

    assert _verify("--test-command", PYTEST) == EXIT_PASS


def test_verify_with_no_plan_from_anywhere_is_a_usage_error(repo, shim_path, capsys):
    shim_path(claude_events(MET))

    assert _verify() == EXIT_USAGE
    assert "verify needs a plan" in capsys.readouterr().err


def test_a_setting_in_the_project_file_is_enforced_without_any_flag(repo, shim_path):
    shim_path(claude_events(UNTESTED))
    assert _verify("--plan", "plan.md") == EXIT_PASS

    _settings(repo, "require-tests = true\n")
    assert _verify("--plan", "plan.md") == EXIT_FAIL


def test_a_flag_overrides_the_project_file(repo, shim_path):
    """Including downwards: a flag has to be able to switch off what the file
    switched on, or the file becomes the only way to run the tool."""
    shim_path(claude_events(UNTESTED))
    _settings(repo, "require-tests = true\n")

    assert _verify("--plan", "plan.md", "--no-require-tests") == EXIT_PASS


def test_a_file_that_turns_test_execution_off_can_be_overridden(repo, shim_path):
    shim_path(claude_events(MET_BY_A_FAILING_TEST))
    _settings(repo, f'plan = "plan.md"\nrun-tests = false\ntest-command = {json.dumps(PYTEST)}\n')

    # Not executed: the citation is merely unconfirmed, which passes by default.
    assert _verify() == EXIT_PASS
    # Executed: the cited test fails, and that overturns the reviewer.
    assert _verify("--run-tests") == EXIT_FAIL


def test_no_config_ignores_the_project_file(repo, shim_path, capsys):
    shim_path(claude_events(UNTESTED))
    _settings(repo, 'plan = "plan.md"\nrequire-tests = true\n')

    assert _verify("--no-config", "--plan", "plan.md") == EXIT_PASS
    # ...and with it ignored, the plan it named is gone too.
    assert _verify("--no-config") == EXIT_USAGE
    assert "Settings from" not in capsys.readouterr().out


def test_an_explicit_config_file_is_used_instead_of_the_discovered_one(repo, shim_path):
    shim_path(claude_events(UNTESTED))
    _settings(repo, 'plan = "plan.md"\n')
    _settings(repo, 'plan = "plan.md"\nrequire-tests = true\n', name="strict.toml")

    assert _verify() == EXIT_PASS
    assert _verify("--config", "strict.toml") == EXIT_FAIL


def test_config_and_no_config_together_are_a_usage_error(repo, shim_path, capsys):
    shim_path(claude_events(MET))
    _settings(repo, 'plan = "plan.md"\n')

    assert _verify("--config", ".arbiter.toml", "--no-config") == EXIT_USAGE
    assert "cannot be used together" in capsys.readouterr().err


# ── Failing closed, before anything is paid for ──────────────────────────────


def test_a_typo_in_the_project_file_stops_the_run_before_any_review(repo, shim_path, capsys):
    """Run anyway, and the gate enforces nothing while its config says it does."""
    argv_log = shim_path(claude_events(UNTESTED))
    _settings(repo, 'plan = "plan.md"\nrequire-test = true\n')

    assert _verify() == EXIT_USAGE

    err = capsys.readouterr().err
    assert "unknown setting(s): require-test" in err
    assert "--no-config" in err, "the way out should be named"
    assert not argv_log.exists(), "nothing may be sent to a provider under a broken config"


def test_a_repository_cannot_switch_on_full_tool_access(repo, shim_path, capsys):
    argv_log = shim_path(claude_events(MET))
    _settings(repo, 'plan = "plan.md"\nallow-tools = true\n')

    assert _verify() == EXIT_USAGE
    assert "--allow-tools on the command line" in capsys.readouterr().err
    assert not argv_log.exists()


def test_providers_named_in_the_file_are_validated_like_the_flag(repo, shim_path, capsys):
    shim_path(claude_events(MET))
    _settings(repo, 'plan = "plan.md"\nmodels = ["claude", "gpt4"]\n')

    assert main(["verify", "--diff", "change.diff"]) == EXIT_USAGE
    assert "unknown provider(s): gpt4" in capsys.readouterr().err


def test_a_non_positive_timeout_is_a_usage_error(repo, shim_path, capsys):
    shim_path(claude_events(MET))
    assert _verify("--plan", "plan.md", "--timeout", "0") == EXIT_USAGE
    assert "--timeout must be greater than zero" in capsys.readouterr().err


# ── Value shapes and provenance ──────────────────────────────────────────────


def test_a_test_command_given_as_a_list_is_run_as_written(repo, shim_path):
    shim_path(claude_events(MET))
    command = json.dumps([sys.executable, "-m", "pytest"])
    _settings(repo, f'plan = "plan.md"\nrequire-tests = true\ntest-command = {command}\n')

    assert _verify() == EXIT_PASS


def test_settings_in_pyproject_are_read_the_same_way(repo, shim_path):
    shim_path(claude_events(UNTESTED))
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "demo"\n\n[tool.arbiter]\nplan = "plan.md"\nrequire-tests = true\n'
    )

    assert _verify() == EXIT_FAIL


def test_the_settings_file_is_named_in_the_output_and_the_record(repo, shim_path, capsys):
    """A run's strictness has to be traceable to where it was decided."""
    shim_path(claude_events(MET))
    _settings(repo, f'plan = "plan.md"\ntest-command = {json.dumps(PYTEST)}\n')

    assert _verify("--record", "run.json") == EXIT_PASS

    assert ".arbiter.toml" in capsys.readouterr().out
    record = json.loads((repo / "run.json").read_text())
    assert record["configuration"]["settings_file"].endswith(".arbiter.toml")


def test_a_run_with_no_settings_file_records_that_too(repo, shim_path):
    shim_path(claude_events(MET))

    assert _verify("--plan", "plan.md", "--test-command", PYTEST, "--record", "run.json") == 0
    assert json.loads((repo / "run.json").read_text())["configuration"]["settings_file"] is None


# ── Version and module entry point ───────────────────────────────────────────


def test_version_is_reported(capsys):
    assert main(["--version"]) == EXIT_PASS
    assert capsys.readouterr().out.strip() == f"arbiter {arbiter.__version__}"


def test_the_package_runs_as_a_module():
    """For an environment where the console script never made it onto PATH."""
    proc = subprocess.run(
        [sys.executable, "-m", "arbiter", "--version"], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == f"arbiter {arbiter.__version__}"
