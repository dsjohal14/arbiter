"""`arbiter init`.

Two properties. What it writes has to actually work — a settings template that
the loader then rejects would fail every new user on their first command. And
it must never overwrite: it runs in other people's repositories.
"""

from __future__ import annotations

import pytest

from arbiter import config
from arbiter.cli import main
from arbiter.criteria import is_pinned
from arbiter.verify import EXIT_PASS, EXIT_RUNTIME, EXIT_USAGE


@pytest.fixture
def project(tmp_path, monkeypatch, shim_path):
    shim_path(installed=["claude"])
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_a_fresh_project_gets_settings_a_plan_and_an_ignore_rule(project, capsys):
    assert main(["init"]) == EXIT_PASS

    assert (project / ".arbiter.toml").is_file()
    assert (project / "PLAN.md").is_file()
    assert ".arbiter/runs/" in (project / ".gitignore").read_text().splitlines()
    out = capsys.readouterr().out
    assert "created  .arbiter.toml" in out and "created  PLAN.md" in out


def test_the_settings_it_writes_are_settings_arbiter_accepts(project):
    """A template the loader rejects would break every new user's first run."""
    main(["init"])

    loaded = config.load(root=project)

    assert loaded.source == project / ".arbiter.toml"
    assert loaded.values == {"plan": "PLAN.md"}


def test_the_plan_it_writes_is_ready_to_be_reviewed_not_already_a_contract(project, capsys):
    """Placeholder criteria would be verified as though someone had promised
    them. The template has none, so the first step is still a plan review."""
    main(["init"])
    assert not is_pinned((project / "PLAN.md").read_text())

    capsys.readouterr()
    assert main(["verify", "--diff", "working", "--models", "claude"]) == EXIT_USAGE
    assert "arbiter plan pin" in capsys.readouterr().err, "the settings did name the plan"


def test_nothing_that_exists_is_overwritten(project, capsys):
    (project / ".arbiter.toml").write_text('plan = "docs/design.md"\nrequire-tests = true\n')
    (project / "PLAN.md").write_text("# The real plan\n")
    (project / ".gitignore").write_text("venv/\n.arbiter/runs/\n")
    before = {p.name: p.read_text() for p in project.iterdir() if p.is_file()}

    assert main(["init"]) == EXIT_PASS

    assert {p.name: p.read_text() for p in project.iterdir() if p.is_file()} == before
    assert "created" not in capsys.readouterr().out


def test_running_it_twice_changes_nothing_the_second_time(project):
    main(["init"])
    first = {p.name: p.read_text() for p in project.iterdir() if p.is_file()}

    main(["init"])

    assert {p.name: p.read_text() for p in project.iterdir() if p.is_file()} == first


def test_a_project_configured_in_pyproject_is_not_given_a_second_settings_file(project):
    """Two settings files in one directory are refused at run time, so writing
    the second would break a project that was working."""
    (project / "pyproject.toml").write_text('[tool.arbiter]\nplan = "PLAN.md"\n')

    assert main(["init"]) == EXIT_PASS

    assert not (project / ".arbiter.toml").exists()
    assert config.load(root=project).source == project / "pyproject.toml"


def test_the_ignore_rule_is_appended_to_an_existing_gitignore(project):
    (project / ".gitignore").write_text("venv/\n__pycache__/")  # no trailing newline

    main(["init"])

    assert (project / ".gitignore").read_text() == "venv/\n__pycache__/\n.arbiter/runs/\n"


def test_the_plan_can_live_somewhere_else(project):
    assert main(["init", "--plan", "docs/plans/feature.md"]) == EXIT_PASS

    assert (project / "docs" / "plans" / "feature.md").is_file()
    assert config.load(root=project).values["plan"] == "docs/plans/feature.md"


def test_broken_settings_are_reported_and_left_alone(project, capsys):
    (project / ".arbiter.toml").write_text("require-test = true\n")

    assert main(["init"]) == EXIT_RUNTIME

    assert (project / ".arbiter.toml").read_text() == "require-test = true\n"
    assert "unknown setting(s): require-test" in capsys.readouterr().err


def test_it_says_which_providers_are_present_without_calling_any(project, capsys):
    argv_log = project / "argv.jsonl"

    main(["init"])

    out = capsys.readouterr().out
    assert "✓ claude" in out
    assert "✗ codex" in out and "npm install -g @openai/codex" in out
    assert not argv_log.exists(), "init never makes a provider call"
