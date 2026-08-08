"""Mechanical evidence checking, and the downgrades it forces.

This is the load-bearing part of the tool: a reviewer's status is a ceiling, and
facts on disk bring it down. A confident assertion — or an injected one — cannot
produce a pass on its own.
"""

from __future__ import annotations

import sys

import pytest

from arbiter.evidence import check_file_citation, check_test_citation
from arbiter.verify import CriterionResult, apply_evidence_checks

# pytest is importable here but not necessarily on PATH.
PYTEST = [sys.executable, "-m", "pytest"]


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "app.py").write_text("def total(items):\n    return sum(items)\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "def test_passes():\n    assert True\n\n\ndef test_fails():\n    assert False\n"
    )
    return tmp_path


# ── File citations ───────────────────────────────────────────────────────────


def test_a_real_line_is_verified_and_quoted_back(repo):
    check = check_file_citation("app.py:2", repo)
    assert check.verified is True
    assert "return sum(items)" in check.detail


def test_a_nonexistent_file_is_refuted(repo):
    check = check_file_citation("nope.py:1", repo)
    assert check.verified is False
    assert "does not exist" in check.detail


def test_a_line_past_the_end_of_the_file_is_refuted(repo):
    """The classic hallucinated citation: a real file, an invented line."""
    check = check_file_citation("app.py:900", repo)
    assert check.verified is False
    assert "900 does not exist" in check.detail


def test_a_path_escaping_the_repo_is_refused(repo, tmp_path_factory):
    """A citation must not be able to read files outside the repository."""
    outside = tmp_path_factory.mktemp("outside") / "secret.py"
    outside.write_text("SECRET = 1\n")
    check = check_file_citation("../../../../etc/hosts.conf:1", repo)
    assert check.verified is False
    assert "escapes" in check.detail or "does not exist" in check.detail


def test_uncheckable_citation_is_none_not_false(repo):
    """'We could not check this' and 'we checked and it is false' are different
    results and must not collapse into each other."""
    assert check_file_citation("see the parser somewhere", repo).verified is None


@pytest.mark.parametrize("citation", ["app.py:1", "app.py line 1", "app.py:1-2", "./app.py:1"])
def test_citation_formats_models_actually_write(citation, repo):
    assert check_file_citation(citation, repo).verified is True


# ── Test citations ───────────────────────────────────────────────────────────


def test_a_passing_test_is_verified(repo):
    check = check_test_citation("tests/test_app.py::test_passes", repo, PYTEST)
    assert check.verified is True
    assert check.detail == "passed"


def test_a_failing_test_is_refuted_with_output(repo):
    check = check_test_citation("tests/test_app.py::test_fails", repo, PYTEST)
    assert check.verified is False
    assert check.detail.startswith("failed")


def test_a_test_that_does_not_exist_is_refuted(repo):
    check = check_test_citation("tests/test_app.py::test_imaginary", repo, PYTEST)
    assert check.verified is False
    assert check.detail == "no such test"


def test_no_run_checks_existence_without_executing(repo):
    check = check_test_citation("tests/test_app.py::test_fails", repo, PYTEST, run=False)
    assert check.verified is None
    assert "not executed" in check.detail


# ── Downgrades ───────────────────────────────────────────────────────────────


def _result(status="met", impl=None, test=None):
    return CriterionResult(
        id="AC-1", text="t", status=status, implementation_evidence=impl, test_evidence=test
    )


def test_met_with_a_passing_test_survives(repo):
    r = apply_evidence_checks(
        _result(impl="app.py:2", test="tests/test_app.py::test_passes"), repo, PYTEST
    )
    assert r.status == "met"
    assert r.downgraded_from is None


def test_met_without_any_test_becomes_untested(repo):
    """'Implemented' and 'implemented and proven' are different risks, and
    collapsing them is what hides missing coverage."""
    r = apply_evidence_checks(_result(impl="app.py:2"), repo, PYTEST)
    assert r.status == "untested"
    assert r.downgrade_reason == "no test cited"


def test_a_cited_test_that_fails_makes_the_criterion_unmet(repo):
    """The strongest signal available: the reviewer said met, the test says no."""
    r = apply_evidence_checks(
        _result(impl="app.py:2", test="tests/test_app.py::test_fails"), repo, PYTEST
    )
    assert r.status == "unmet"
    assert "fails" in r.downgrade_reason


def test_a_cited_test_that_does_not_exist_becomes_untested(repo):
    r = apply_evidence_checks(
        _result(impl="app.py:2", test="tests/test_app.py::test_imaginary"), repo, PYTEST
    )
    assert r.status == "untested"
    assert "does not exist" in r.downgrade_reason


def test_a_fabricated_code_citation_makes_the_claim_unverifiable(repo):
    r = apply_evidence_checks(
        _result(impl="app.py:900", test="tests/test_app.py::test_passes"), repo, PYTEST
    )
    assert r.status == "unverifiable"
    assert "does not resolve" in r.downgrade_reason


def test_status_is_never_raised_only_lowered(repo):
    """A reviewer's verdict is a ceiling. Real evidence cannot promote `unmet`."""
    r = apply_evidence_checks(
        _result(status="unmet", impl="app.py:2", test="tests/test_app.py::test_passes"),
        repo,
        PYTEST,
    )
    assert r.status == "unmet"


def test_no_run_tests_still_downgrades_met_to_untested(repo):
    """Without executing the test we cannot confirm it, so `met` is not earned."""
    r = apply_evidence_checks(
        _result(impl="app.py:2", test="tests/test_app.py::test_passes"),
        repo,
        PYTEST,
        run_tests=False,
    )
    assert r.status == "untested"
