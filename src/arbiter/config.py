"""Project configuration: one definition of the gate, read by every caller.

A gate is only as strict as its loosest invocation. When the flags live in a CI
workflow, a pre-commit hook, and somebody's shell history, the three drift, and
the loosest is the one that ships. So a project's settings go in one file:

    # pyproject.toml
    [tool.arbiter]
    plan = "docs/plan.md"
    models = ["claude", "codex"]
    require-tests = true
    test-command = ["uv", "run", "pytest"]

or the same keys at the top level of `.arbiter.toml`, for a project with no
pyproject.toml. A flag given on the command line always wins.

Two rules here are deliberate.

Everything fails closed. An unknown key or a value of the wrong type is an
error, never a warning: `require-test = true` quietly enforcing nothing is the
worst thing a gate's configuration can do.

A repository cannot grant itself access. `--allow-tools` and
`--no-scrub-test-env` widen what a run can touch on the machine running it, so
they are accepted only from the person at the command line. A file that arrives
with the code under review does not get to make that decision.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

DEDICATED_FILE = ".arbiter.toml"
PYPROJECT_FILE = "pyproject.toml"

# When the consolidation call is made. `auto` skips it where it would have
# nothing to reconcile; the other two are for wanting it always, or never.
CONSOLIDATE_MODES = ("auto", "always", "never")


class ConfigError(ValueError):
    """The project configuration is unreadable, malformed, or not allowed."""


def _string(key: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"`{key}` must be a non-empty string, not {value!r}.")
    return value.strip()


def _flag(key: str, value: object) -> bool:
    # TOML has real booleans. "true" in quotes, or 1, is a mistake worth
    # stopping for rather than guessing at.
    if not isinstance(value, bool):
        raise ConfigError(f"`{key}` must be true or false, not {value!r}.")
    return value


def _positive(key: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"`{key}` must be a number greater than zero, not {value!r}.")
    return float(value)


def _count(key: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"`{key}` must be a whole number, zero or more, not {value!r}.")
    return value


def _mode(key: str, value: object) -> str:
    if value not in CONSOLIDATE_MODES:
        raise ConfigError(f"`{key}` must be one of {', '.join(CONSOLIDATE_MODES)}, not {value!r}.")
    return str(value)


def _names(key: str, value: object) -> list[str]:
    if isinstance(value, str):
        names = value.split(",")
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        names = value
    else:
        raise ConfigError(f"`{key}` must be a list of provider names, not {value!r}.")
    names = [name.strip() for name in names if name.strip()]
    if not names:
        raise ConfigError(f"`{key}` names no providers.")
    return names


def _command(key: str, value: object) -> str | list[str]:
    """A string is split like a shell would, later; a list is taken as argv."""
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, list) and value and all(isinstance(item, str) and item for item in value):
        return list(value)
    raise ConfigError(f"`{key}` must be a command string or a list of arguments, not {value!r}.")


# Every setting a project file may carry, and how its value is checked. Keys
# are spelled like the flags they stand in for.
SETTINGS: dict[str, Callable[[str, object], object]] = {
    "models": _names,
    "judge": _string,
    "timeout": _positive,
    "retries": _count,
    "consolidate": _mode,
    "plan": _string,
    "base": _string,
    "require-tests": _flag,
    "require-pinned-evidence": _flag,
    "require-discriminating-tests": _flag,
    "run-tests": _flag,
    "test-command": _command,
    "test-timeout": _positive,
}

# Settings that widen what a run can reach on the host, mapped to the flag that
# does it. Naming them specifically beats "unknown setting": the person who
# wrote the line should learn why it is refused, not just that it is.
COMMAND_LINE_ONLY = {
    "allow-tools": "--allow-tools",
    "scrub-test-env": "--no-scrub-test-env",
    "no-scrub-test-env": "--no-scrub-test-env",
}


@dataclass(frozen=True)
class Config:
    """A project's validated settings, and the file they came from."""

    source: Path | None = None
    values: dict[str, object] = field(default_factory=dict)


def _table(path: Path, *, required: bool) -> dict[str, object] | None:
    """The settings table in `path`, or None when a pyproject.toml has none."""
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError as e:
        raise ConfigError(f"Could not read {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}") from e

    if path.name != PYPROJECT_FILE:
        return data

    tool = data.get("tool")
    table = tool.get("arbiter") if isinstance(tool, dict) else None
    if table is None:
        if required:
            raise ConfigError(f"{path} has no [tool.arbiter] table.")
        return None
    if not isinstance(table, dict):
        raise ConfigError(f"{path}: [tool.arbiter] must be a table of settings.")
    return table


def _validate(table: dict[str, object], path: Path) -> dict[str, object]:
    values: dict[str, object] = {}
    unknown: list[str] = []
    for raw_key, value in table.items():
        key = raw_key.replace("_", "-")
        if key in COMMAND_LINE_ONLY:
            raise ConfigError(
                f"{path}: `{raw_key}` cannot be set in a config file. It widens what a run "
                f"can touch on the machine running it, so it is only accepted as "
                f"{COMMAND_LINE_ONLY[key]} on the command line."
            )
        check = SETTINGS.get(key)
        if check is None:
            unknown.append(raw_key)
            continue
        if key in values:
            raise ConfigError(f"{path}: `{key}` is set twice, under two spellings.")
        try:
            values[key] = check(key, value)
        except ConfigError as e:
            raise ConfigError(f"{path}: {e}") from e

    if unknown:
        raise ConfigError(
            f"{path}: unknown setting(s): {', '.join(sorted(unknown))}. "
            f"Known settings: {', '.join(sorted(SETTINGS))}."
        )
    return values


def load(explicit: Path | None = None, root: Path | None = None) -> Config:
    """The settings for the project at `root`, or an empty Config if it has none.

    Only `root` itself is searched. Arbiter resolves every citation against the
    directory it runs in, so a file found further up would configure a run
    whose evidence is read from somewhere else.
    """
    if explicit is not None:
        if not explicit.is_file():
            raise ConfigError(f"Config file not found: {explicit}")
        table = _table(explicit, required=True)
        assert table is not None
        return Config(explicit, _validate(table, explicit))

    root = root or Path.cwd()
    found: list[tuple[Path, dict[str, object]]] = []
    for name in (DEDICATED_FILE, PYPROJECT_FILE):
        path = root / name
        if not path.is_file():
            continue
        table = _table(path, required=False)
        if table is not None:
            found.append((path, table))

    if len(found) > 1:
        # Picking a winner would let a setting tightened in one file be
        # silently undone by the other.
        raise ConfigError(
            f"Both {DEDICATED_FILE} and [tool.arbiter] in {PYPROJECT_FILE} configure "
            "Arbiter. Keep one of them."
        )
    if not found:
        return Config()
    path, table = found[0]
    return Config(path, _validate(table, path))
