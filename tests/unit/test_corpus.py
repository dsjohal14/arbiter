"""The checked-in evaluation corpus has to load, in CI, on every change.

`make evals` validates the real cases, but nothing in `make check` or the
workflow ran it — so a broken `expected.json` could sit in the repo while the
loader's own synthetic tests stayed green. These tests read the actual files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arbiter import evals, verify

CORPUS = Path(__file__).resolve().parents[2] / "evals"


def case_dirs() -> list[Path]:
    return sorted(p for p in CORPUS.iterdir() if p.is_dir())


def test_the_corpus_exists_and_is_not_empty():
    assert CORPUS.is_dir(), f"no corpus at {CORPUS}"
    assert case_dirs(), "the corpus directory contains no cases"


@pytest.mark.parametrize("path", case_dirs(), ids=lambda p: p.name)
def test_every_checked_in_case_loads(path):
    """Loading is where every integrity rule lives: unknown criterion ids,
    unconstrained expectations, answer keys that assert nothing."""
    case = evals.load_case(path)

    assert case.id == path.name
    assert case.description, f"{path.name} does not say why it exists"


@pytest.mark.parametrize("path", case_dirs(), ids=lambda p: p.name)
def test_a_verify_case_ships_the_tree_its_diff_applies_to(path):
    """Without one, the case only ever measures the model layer: there is
    nothing for the evidence checks to read, and no way to bind a record to it."""
    case = evals.load_case(path)
    if case.command != "verify":
        return

    assert case.repo is not None, f"{case.id} is a verify case with no {evals.REPO_DIR}/"
    manifest = verify.build_manifest(case.diff or "", case.repo)
    assert manifest, f"{case.id}: none of the diff's paths exist under {evals.REPO_DIR}/"
