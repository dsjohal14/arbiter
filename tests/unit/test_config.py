"""Project settings.

The property under test is that a settings file can only ever mean what it
says. A gate's configuration that is silently ignored — a typo, a wrong type, a
second file that quietly wins — is worse than no configuration, because it is
believed.
"""

from __future__ import annotations

import pytest

from arbiter import cli, config
from arbiter.config import ConfigError


def _pyproject(tmp_path, body: str):
    path = tmp_path / "pyproject.toml"
    path.write_text(f'[project]\nname = "demo"\n\n[tool.arbiter]\n{body}')
    return path


def _dedicated(tmp_path, body: str):
    path = tmp_path / ".arbiter.toml"
    path.write_text(body)
    return path


# ── Finding the settings ─────────────────────────────────────────────────────


def test_a_project_with_no_settings_has_none(tmp_path):
    loaded = config.load(root=tmp_path)
    assert loaded.source is None
    assert loaded.values == {}


def test_settings_are_read_from_tool_arbiter_in_pyproject(tmp_path):
    path = _pyproject(tmp_path, 'plan = "docs/plan.md"\nrequire-tests = true\ntimeout = 90\n')

    loaded = config.load(root=tmp_path)

    assert loaded.source == path
    assert loaded.values == {"plan": "docs/plan.md", "require-tests": True, "timeout": 90.0}


def test_settings_are_read_from_a_dedicated_file(tmp_path):
    """A project with no pyproject.toml still needs one place to define its gate."""
    path = _dedicated(tmp_path, 'judge = "codex"\nrun-tests = false\n')

    loaded = config.load(root=tmp_path)

    assert loaded.source == path
    assert loaded.values == {"judge": "codex", "run-tests": False}


def test_a_pyproject_that_does_not_mention_arbiter_is_not_a_settings_file(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")
    assert config.load(root=tmp_path).source is None


def test_two_settings_files_are_refused_rather_than_ranked(tmp_path):
    """Picking a winner would let a setting tightened in one file be silently
    undone by the other."""
    _pyproject(tmp_path, "require-tests = true\n")
    _dedicated(tmp_path, "require-tests = false\n")

    with pytest.raises(ConfigError, match="Keep one of them"):
        config.load(root=tmp_path)


def test_only_the_directory_arbiter_runs_in_is_searched(tmp_path):
    """Citations resolve against the run directory, so a file found further up
    would configure a run whose evidence is read from somewhere else."""
    _dedicated(tmp_path, "require-tests = true\n")
    nested = tmp_path / "service"
    nested.mkdir()

    assert config.load(root=nested).source is None


def test_an_explicit_file_must_exist(tmp_path):
    with pytest.raises(ConfigError, match="Config file not found"):
        config.load(tmp_path / "absent.toml")


def test_an_explicit_pyproject_must_actually_configure_arbiter(tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "demo"\n')

    with pytest.raises(ConfigError, match=r"no \[tool.arbiter\] table"):
        config.load(path)


def test_a_file_that_is_not_toml_is_named_in_the_error(tmp_path):
    path = _dedicated(tmp_path, "require-tests = = true\n")

    with pytest.raises(ConfigError, match=r"\.arbiter\.toml is not valid TOML"):
        config.load(root=tmp_path)
    assert path.is_file()


# ── Failing closed ───────────────────────────────────────────────────────────


def test_a_misspelled_setting_is_an_error_not_a_warning(tmp_path):
    """`require-test = true` enforcing nothing, quietly, is the worst outcome a
    gate's configuration can have."""
    _dedicated(tmp_path, "require-test = true\n")

    with pytest.raises(ConfigError) as error:
        config.load(root=tmp_path)

    assert "unknown setting(s): require-test" in str(error.value)
    assert "require-tests" in str(error.value), "the valid spelling should be on offer"


@pytest.mark.parametrize(
    ("line", "complaint"),
    [
        ('require-tests = "true"', "must be true or false"),
        ("require-tests = 1", "must be true or false"),
        ("timeout = 0", "greater than zero"),
        ("timeout = -5", "greater than zero"),
        ('timeout = "30"', "greater than zero"),
        ("timeout = true", "greater than zero"),
        ("test-timeout = 0", "greater than zero"),
        ("models = []", "names no providers"),
        ("models = [1, 2]", "list of provider names"),
        ("models = 3", "list of provider names"),
        ("test-command = []", "command string or a list"),
        ('test-command = ["pytest", ""]', "command string or a list"),
        ('plan = ""', "non-empty string"),
        ("plan = 7", "non-empty string"),
    ],
)
def test_a_value_of_the_wrong_kind_is_refused(tmp_path, line, complaint):
    _dedicated(tmp_path, line + "\n")

    with pytest.raises(ConfigError, match=complaint):
        config.load(root=tmp_path)


def test_the_same_setting_under_two_spellings_is_refused(tmp_path):
    _dedicated(tmp_path, "require-tests = true\nrequire_tests = false\n")

    with pytest.raises(ConfigError, match="set twice"):
        config.load(root=tmp_path)


def test_underscores_are_read_as_hyphens(tmp_path):
    _dedicated(tmp_path, "require_pinned_evidence = true\n")
    assert config.load(root=tmp_path).values == {"require-pinned-evidence": True}


# ── What a repository may not decide ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("line", "flag"),
    [
        ("allow-tools = true", "--allow-tools"),
        ("allow_tools = true", "--allow-tools"),
        ("scrub-test-env = false", "--no-scrub-test-env"),
        ("no-scrub-test-env = true", "--no-scrub-test-env"),
    ],
)
def test_a_repository_cannot_grant_itself_access_to_the_host(tmp_path, line, flag):
    """These widen what a run can touch on the machine running it. A file that
    arrives with the code under review does not get to make that call."""
    _dedicated(tmp_path, line + "\n")

    with pytest.raises(ConfigError) as error:
        config.load(root=tmp_path)

    assert "cannot be set in a config file" in str(error.value)
    assert flag in str(error.value)


# ── Value shapes ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ['["claude", "codex"]', '"claude, codex"', '" claude ,codex,"'])
def test_models_may_be_a_list_or_a_comma_separated_string(tmp_path, value):
    _dedicated(tmp_path, f"models = {value}\n")
    assert config.load(root=tmp_path).values["models"] == ["claude", "codex"]


def test_a_test_command_keeps_the_shape_it_was_written_in(tmp_path):
    """A string is split like a shell would, later, exactly as the flag is; a
    list is already an argv and must not be re-split."""
    _dedicated(tmp_path, 'test-command = ["uv", "run", "pytest", "-k", "a b"]\n')
    assert config.load(root=tmp_path).values["test-command"] == ["uv", "run", "pytest", "-k", "a b"]

    _dedicated(tmp_path, 'test-command = "uv run pytest"\n')
    assert config.load(root=tmp_path).values["test-command"] == "uv run pytest"


# ── The file and the command line describe the same options ──────────────────


def test_every_setting_has_a_flag_and_a_built_in_default():
    """A setting with no flag could not be overridden, and one with no default
    would reach the command as None."""
    assert set(cli._BUILT_IN) == set(config.SETTINGS)

    flags = vars(cli.parse_args(["verify", "--plan", "plan.md"]))
    for key in config.SETTINGS:
        assert key.replace("-", "_") in flags, f"`{key}` has no command-line flag"
