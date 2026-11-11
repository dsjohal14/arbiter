"""`arbiter init` — set a project up to be verified.

The first run of a tool decides whether there is a second. Left to the README,
getting started is four separate things to find and get right: a settings file,
a plan in a shape Arbiter can read, an ignore rule for run records (they hold
the plan, the diff, and everything the reviewers said), and knowing which
provider CLIs are present. This does those, says what it did, and stops.

It never overwrites. A file that exists is left exactly as it is and reported,
so running it twice — or in a project that is already set up — changes nothing.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .models import DEFAULT_PANEL, PROVIDERS

RECORDS_IGNORE = ".arbiter/runs/"

SETTINGS_TEMPLATE = """\
# Arbiter settings for this project. A flag on the command line overrides them.

# The plan this project's changes are verified against.
plan = "{plan}"

# Stricter gates, off by default. Uncomment what should hold everywhere —
# in CI, in a pre-commit hook, and at anyone's terminal alike.
# require-tests = true              # an untested criterion fails the gate
# require-pinned-evidence = true    # only a test named in the plan counts as proof
# test-command = ["uv", "run", "pytest"]
"""

PLAN_TEMPLATE = """\
# Plan: <what this change does>

<!--
Write the change in plain prose: what it does, what it must not do, and how you
will know it worked. Then let Arbiter find the holes while they are cheap:

    arbiter plan review {plan}            # is this good enough to build from?
    arbiter plan review {plan} --apply    # write the proposed criteria in
    arbiter verify --diff                 # did the staged change keep them?

Delete this comment once the plan says something.
-->

## What and why

## What must hold when this is done

## Out of scope
"""


@dataclass
class Scaffold:
    """What `init` did, in the order it did it."""

    created: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    providers: dict[str, bool] = field(default_factory=dict)


def _ignores_records(text: str) -> bool:
    wanted = RECORDS_IGNORE.strip("/")
    return any(line.strip().strip("/") in (wanted, ".arbiter") for line in text.splitlines())


def init(root: Path, plan: str = "PLAN.md") -> Scaffold:
    done = Scaffold()

    # Settings. Two files in one directory are refused at run time, so an
    # existing [tool.arbiter] table is as much "already set up" as a file.
    try:
        existing = config.load(root=root).source
    except config.ConfigError as e:
        existing = None
        done.problems.append(f"{e} Fix it, then run `arbiter init` again.")
    if existing is not None:
        done.kept.append(f"{existing.name} (settings)")
    elif not done.problems:
        (root / config.DEDICATED_FILE).write_text(
            SETTINGS_TEMPLATE.format(plan=plan), encoding="utf-8"
        )
        done.created.append(f"{config.DEDICATED_FILE} (settings)")

    plan_path = root / plan
    if plan_path.exists():
        done.kept.append(f"{plan} (plan)")
    else:
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(PLAN_TEMPLATE.format(plan=plan), encoding="utf-8")
        done.created.append(f"{plan} (plan)")

    # Run records are sensitive. Ignoring them before the first one exists is
    # the only moment it cannot be forgotten.
    ignore = root / ".gitignore"
    current = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    if _ignores_records(current):
        done.kept.append(f".gitignore (already ignores {RECORDS_IGNORE})")
    else:
        separator = "" if not current or current.endswith("\n") else "\n"
        ignore.write_text(f"{current}{separator}{RECORDS_IGNORE}\n", encoding="utf-8")
        done.created.append(f".gitignore (now ignores {RECORDS_IGNORE})")

    # PATH only. Probing a provider costs a real call; `arbiter doctor` does that.
    done.providers = {key: shutil.which(PROVIDERS[key].cli) is not None for key in DEFAULT_PANEL}
    return done
