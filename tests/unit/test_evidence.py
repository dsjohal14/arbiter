"""Mechanical evidence checking, and the downgrades it forces.

This is the load-bearing part of the tool: a reviewer's status is a ceiling, and
facts on disk bring it down. A confident assertion — or an injected one — cannot
produce a pass on its own.
"""

from __future__ import annotations

import sys
import time

import pytest

from arbiter import evidence
from arbiter.evidence import (
    EvidenceCheck,
    check_file_citation,
    check_test_citation,
    detect_test_command,
)
from arbiter.verify import (
    CriterionResult,
    apply_downgrades,
    apply_evidence_checks,
    derive_verdict,
)

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


def test_a_path_escaping_the_repo_is_refused(repo):
    """A citation must not be able to read files outside the repository.

    The target must genuinely EXIST, or the test passes for the wrong reason:
    a non-existent outside path is refused by the does-not-exist branch even
    with the containment check deleted, so it proves nothing.
    """
    outside = repo.parent / "outside_secret.py"
    outside.write_text("SECRET = 1\n")
    assert outside.is_file()

    check = check_file_citation(f"../{outside.name}:1", repo)
    assert check.verified is False
    assert "escapes" in check.detail, "must be refused for containment, not absence"
    assert "SECRET" not in check.detail, "the outside file must never be read"


def test_a_test_path_escaping_the_repo_is_refused(repo):
    outside = repo.parent / "outside_test.py"
    outside.write_text("def test_x():\n    pass\n")
    check = check_test_citation(f"../{outside.name}::test_x", repo, PYTEST)
    assert check.verified is False
    assert "escapes" in check.detail


# ── Runner detection ─────────────────────────────────────────────────────────


def test_pytest_is_detected_in_a_python_project(repo):
    assert detect_test_command(repo) is not None


def test_a_project_with_no_python_test_markers_is_not_guessed_at(tmp_path):
    """Guessing another project's runner wrong is worse than admitting we do not
    know, so anything non-pytest must come from --test-command."""
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'x'\n")
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}\n')
    assert detect_test_command(tmp_path) is None


def test_detection_falls_back_to_python_m_pytest(repo, monkeypatch):
    """pytest is often importable without being on PATH — a venv whose scripts
    were never rewritten, for instance."""
    monkeypatch.setattr("arbiter.evidence.shutil.which", lambda _: None)
    cmd = detect_test_command(repo)
    assert cmd == [sys.executable, "-m", "pytest"]


def test_detection_gives_up_when_pytest_is_absent_entirely(repo, monkeypatch):
    monkeypatch.setattr("arbiter.evidence.shutil.which", lambda _: None)
    monkeypatch.setattr("arbiter.evidence.importlib.util.find_spec", lambda _: None)
    assert detect_test_command(repo) is None


def test_an_undetectable_runner_leaves_the_test_unchecked(repo, monkeypatch):
    """No runner must mean 'could not check', never 'checked and passed'."""
    monkeypatch.setattr("arbiter.evidence.detect_test_command", lambda _root=None: None)
    check = check_test_citation("tests/test_app.py::test_passes", repo, None)
    assert check.verified is None
    assert "no test runner" in check.detail


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


# ── What actually happened to the test ───────────────────────────────────────
#
# An exit status and a block of output are both unreliable narrators of one
# test. pytest exits 0 for a skip and for an expected failure, exits non-zero
# for a passing test when a coverage threshold trips, and a failing assertion
# may print anything it likes. Every case here was a wrong verdict before the
# outcome was read from the runner's own report: a clean pass for the first
# five, a false failure for the coverage threshold.

OUTCOME_TESTS = """\
import time

import pytest


def test_passes():
    assert True


def test_fails_saying_not_found():
    assert 1 == 2, "user not found in directory"


@pytest.mark.skip(reason="flaky, will fix later")
def test_skipped():
    assert False


@pytest.mark.xfail
def test_expected_failure():
    assert False


@pytest.mark.xfail
def test_unexpected_pass():
    assert True


def test_needs_a_missing_fixture(no_such_fixture):
    assert True


@pytest.mark.parametrize("n", [1, 2])
def test_cases(n):
    assert n == 1


@pytest.mark.parametrize("n", [1, pytest.param(2, marks=pytest.mark.skip(reason="later"))])
def test_partly_skipped(n):
    assert n == 1


@pytest.mark.slow
def test_slow():
    assert True


def test_hangs():
    time.sleep(60)
"""


@pytest.fixture
def outcomes(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_outcomes.py").write_text(OUTCOME_TESTS)
    (tmp_path / "tests" / "test_unimportable.py").write_text(
        "import a_module_that_does_not_exist\n\n\ndef test_x():\n    assert True\n"
    )
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    slow: takes a while\n")
    return tmp_path


def _outcome(repo, name, command=None, **kwargs):
    return check_test_citation(f"tests/test_outcomes.py::{name}", repo, command or PYTEST, **kwargs)


def test_a_failure_whose_output_says_not_found_is_still_a_failure(outcomes):
    """Matching "not found" anywhere in the output read this as a test that does
    not exist — `untested`, which passes — when it had run and failed."""
    check = _outcome(outcomes, "test_fails_saying_not_found")
    assert check.outcome == evidence.FAILED
    assert check.verified is False
    assert check.detail.startswith("failed")
    assert "user not found in directory" in check.detail


def test_a_skipped_test_is_not_evidence_that_it_passes(outcomes):
    """pytest exits 0 for a skip. Marking a red test `skip` must not turn it
    into proof."""
    check = _outcome(outcomes, "test_skipped")
    assert check.outcome == evidence.SKIPPED
    assert check.verified is None
    assert "flaky, will fix later" in check.detail


def test_an_expected_failure_is_a_failing_test(outcomes):
    """pytest also exits 0 for xfail, and reports it as a kind of skip. It is
    neither: the test ran, and what it asserts does not hold."""
    check = _outcome(outcomes, "test_expected_failure")
    assert check.outcome == evidence.FAILED
    assert check.verified is False
    assert check.detail.startswith("failed") and "xfail" in check.detail


def test_a_test_marked_xfail_that_passes_did_pass(outcomes):
    assert _outcome(outcomes, "test_unexpected_pass").outcome == evidence.PASSED


def test_a_test_that_errors_in_setup_is_not_reported_as_missing(outcomes):
    """Its output says "fixture 'no_such_fixture' not found", which used to make
    a broken proof read as an absent one."""
    check = _outcome(outcomes, "test_needs_a_missing_fixture")
    assert check.outcome == evidence.ERRORED
    assert check.verified is None
    assert "no_such_fixture" in check.detail


def test_a_test_module_that_cannot_be_imported_is_not_reported_as_missing(outcomes):
    """pytest exits 4 for this, the same status as a node id that matches
    nothing. They are different facts: this test exists and is broken."""
    check = check_test_citation("tests/test_unimportable.py::test_x", outcomes, PYTEST)
    assert check.outcome == evidence.ERRORED
    assert "a_module_that_does_not_exist" in check.detail


def test_a_test_that_really_is_absent_is_still_reported_as_missing(outcomes):
    check = _outcome(outcomes, "test_imaginary")
    assert check.outcome == evidence.MISSING
    assert check.verified is False


def test_one_failing_case_fails_a_parametrized_citation(outcomes):
    check = _outcome(outcomes, "test_cases")
    assert check.outcome == evidence.FAILED
    assert "test_cases[2]" in check.detail


def test_a_single_case_of_a_parametrized_test_can_be_cited(outcomes):
    assert _outcome(outcomes, "test_cases[1]").outcome == evidence.PASSED


def test_a_skipped_case_neither_fails_a_citation_nor_counts_toward_it(outcomes):
    check = _outcome(outcomes, "test_partly_skipped")
    assert check.outcome == evidence.PASSED
    assert "1 of 2 cases skipped" in check.detail


def test_a_coverage_threshold_does_not_fail_a_test_that_passed(tmp_path):
    """One test never covers a project, so a `--cov-fail-under` in the project's
    own config trips on every single-test run and pytest exits 1. Reading the
    exit status reported every proof in such a project as failing."""
    pytest.importorskip("pytest_cov")
    (tmp_path / "conftest.py").write_text("")
    (tmp_path / "lib.py").write_text(
        "def used():\n    return 1\n\n\ndef unused(a):\n    if a:\n        return 2\n    return 3\n"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_lib.py").write_text(
        "from lib import used\n\n\ndef test_used():\n    assert used() == 1\n"
    )
    strict_coverage = [*PYTEST, "--cov=lib", "--cov-fail-under=100"]

    check = check_test_citation("tests/test_lib.py::test_used", tmp_path, strict_coverage)

    assert check.outcome == evidence.PASSED
    assert check.verified is True


def test_a_project_that_already_runs_quiet_does_not_hide_the_outcome(outcomes):
    """A project's `addopts = -q` plus the `-q` passed here is `-qq`, at which
    pytest prints no summary line at all — so the outcome cannot be read off
    the terminal either."""
    (outcomes / "pytest.ini").write_text("[pytest]\naddopts = -q\nmarkers =\n    slow: slow\n")
    assert _outcome(outcomes, "test_skipped").outcome == evidence.SKIPPED
    assert _outcome(outcomes, "test_passes").outcome == evidence.PASSED


def test_a_test_the_project_deselects_did_not_run(outcomes):
    (outcomes / "pytest.ini").write_text(
        '[pytest]\naddopts = -m "not slow"\nmarkers =\n    slow: takes a while\n'
    )
    check = _outcome(outcomes, "test_slow")
    assert check.outcome == evidence.SKIPPED
    assert "deselected" in check.detail


def test_a_test_that_hangs_is_cut_off_and_is_not_a_pass(outcomes):
    started = time.monotonic()
    check = _outcome(outcomes, "test_hangs", timeout=1.0)
    assert time.monotonic() - started < 20
    assert check.outcome == evidence.ERRORED
    assert "timed out after 1s" in check.detail


def test_a_runner_that_cannot_be_launched_is_an_error_not_a_pass(outcomes):
    check = _outcome(outcomes, "test_passes", ["definitely-not-a-test-runner"])
    assert check.outcome == evidence.ERRORED
    assert "could not run the test command" in check.detail


# A command that is not pytest, or that runs inside a container, leaves no
# report to read. The exit status is then all there is, plus whatever the
# output volunteers — which may only ever lower the result.

REPORTLESS_RUNNER = "import sys\nsys.stdout.write(sys.argv[2])\nsys.exit(int(sys.argv[1]))\n"


@pytest.mark.parametrize(
    ("status", "output", "expected"),
    [
        (0, "1 passed in 0.01s", evidence.PASSED),
        (0, "", evidence.PASSED),
        (0, "1 skipped in 0.01s", evidence.SKIPPED),
        (0, "1 xfailed in 0.01s", evidence.FAILED),
        (0, "2 passed, 1 skipped in 0.01s", evidence.PASSED),
        (1, "1 failed in 0.01s", evidence.FAILED),
        # A failing status is never talked back up by text a test could print.
        (1, "1 passed in 0.01s", evidence.FAILED),
        (
            4,
            "ERROR: not found: /r/t.py::test_y\n(no match in any of [<Module t.py>])",
            evidence.MISSING,
        ),
        (4, "pytest: error: unrecognized arguments: --no-header", evidence.ERRORED),
        (5, "no tests ran in 0.00s", evidence.MISSING),
        (5, "1 deselected in 0.00s", evidence.SKIPPED),
        (2, "Interrupted: 1 error during collection", evidence.ERRORED),
        (3, "INTERNALERROR> boom", evidence.ERRORED),
        (101, "error: test failed, to rerun pass `--lib`", evidence.FAILED),
    ],
)
def test_a_run_that_leaves_no_report_is_classified_by_its_exit_status(
    tmp_path, status, output, expected
):
    runner = tmp_path / "runner.py"
    runner.write_text(REPORTLESS_RUNNER)
    command = [sys.executable, str(runner), str(status), output]

    report = evidence.run_test(command, "tests/t.py::test_y", tmp_path)

    assert report.outcome == expected


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


def test_a_skipped_proof_cannot_keep_a_criterion_met(outcomes):
    """The cheapest way to turn a red proof green is to stop running it."""
    r = apply_evidence_checks(
        _result(test="tests/test_outcomes.py::test_skipped"), outcomes, PYTEST
    )
    assert r.status == "untested"
    assert "skipped" in r.downgrade_reason
    assert derive_verdict([r], require_tests=True) == "fail"


def test_a_proof_marked_as_an_expected_failure_makes_the_criterion_unmet(outcomes):
    r = apply_evidence_checks(
        _result(test="tests/test_outcomes.py::test_expected_failure"), outcomes, PYTEST
    )
    assert r.status == "unmet"
    assert derive_verdict([r]) == "fail"


def test_a_proof_that_errors_is_unverifiable_rather_than_a_quiet_pass(outcomes):
    """A broken proof is not the same as a test nobody wrote. `untested` passes
    by default; a proof that was attempted and blew up must not."""
    r = apply_evidence_checks(_result(test="tests/test_unimportable.py::test_x"), outcomes, PYTEST)
    assert r.status == "unverifiable"
    assert r.downgraded_from == "met"
    assert "did not run to a result" in r.downgrade_reason
    assert derive_verdict([r]) == "unclear"


def test_a_proof_that_times_out_is_unverifiable(outcomes):
    r = apply_evidence_checks(
        _result(test="tests/test_outcomes.py::test_hangs"), outcomes, PYTEST, test_timeout=1.0
    )
    assert r.status == "unverifiable"


def test_an_errored_proof_lowers_a_claim_the_reviewer_only_called_untested(outcomes):
    """Facts about the evidence apply whatever the reviewer said, the same way a
    failing test makes `untested` into `unmet`."""
    r = apply_evidence_checks(
        _result(status="untested", test="tests/test_unimportable.py::test_x"), outcomes, PYTEST
    )
    assert r.status == "unverifiable"


@pytest.mark.parametrize(
    ("detail", "verified", "expected", "reason"),
    [
        ("failed\nassert 1 == 2", False, "unmet", "cited test fails"),
        ("no such test", False, "untested", "cited test does not exist"),
        ("timed out after 120s", None, "untested", "test not confirmed"),
    ],
)
def test_a_check_recorded_before_outcomes_existed_is_read_by_its_prose(
    detail, verified, expected, reason
):
    """Older records never stored an outcome. They have to keep replaying to the
    status they recorded, so their checks go through the rules they were written
    under — and round-trip to exactly the dict they were read from."""
    stored = {"kind": "test", "citation": "tests/t.py::t", "verified": verified, "detail": detail}
    check = EvidenceCheck.from_dict(stored)
    r = _result(test="tests/t.py::t")
    r.test_check = check

    apply_downgrades(r)

    assert check.outcome is None
    assert check.as_dict() == stored
    assert r.status == expected
    assert r.downgrade_reason.startswith(reason)


def test_a_proof_shared_by_several_criteria_runs_once(tmp_path):
    """Each run is a fresh interpreter plus the project's own import time, and
    plans routinely rest several criteria on one test."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_counted.py").write_text(
        "from pathlib import Path\n\n\n"
        "def test_counted():\n"
        "    with Path('runs.log').open('a') as log:\n"
        "        log.write('ran\\n')\n"
    )
    citation = "tests/test_counted.py::test_counted"
    cache: dict = {}

    first = apply_evidence_checks(_result(test=citation), tmp_path, PYTEST, cache=cache)
    second = apply_evidence_checks(
        _result(test=f"proven by {citation}"), tmp_path, PYTEST, cache=cache
    )

    assert (tmp_path / "runs.log").read_text() == "ran\n"
    assert first.status == second.status == "met"
    assert first.test_check is not second.test_check, "criteria must not share one mutable check"


# ── Untrusted test execution ─────────────────────────────────────────────────


def test_scrubbed_environment_omits_direct_secret_inheritance(monkeypatch):
    """Scrubbing reduces accidental inheritance; it is not a malicious-code boundary."""
    from arbiter.evidence import scrubbed_env

    for var in ("AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "NPM_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.setenv(var, "leaked")
    env = scrubbed_env()
    assert not any(v in env for v in ("AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "NPM_TOKEN"))
    assert "PATH" in env  # still runnable
    assert env["ARBITER_EVIDENCE_CHECK"] == "1"


def test_a_child_does_not_directly_inherit_a_secret_through_the_runner(repo, monkeypatch):
    """End to end: the token is absent from the child's own environment only."""
    monkeypatch.setenv("CI_DEPLOY_TOKEN", "super-secret-value")
    (repo / "tests" / "test_evil.py").write_text(
        "import os\n\n\ndef test_steal():\n"
        "    assert os.environ.get('CI_DEPLOY_TOKEN') == 'super-secret-value'\n"
    )
    check = check_test_citation("tests/test_evil.py::test_steal", repo, PYTEST)
    assert check.verified is False, "the secret must not be inherited directly"


def test_scrubbing_can_be_disabled_for_trusted_code(repo, monkeypatch):
    monkeypatch.setenv("CI_DEPLOY_TOKEN", "super-secret-value")
    (repo / "tests" / "test_env.py").write_text(
        "import os\n\n\ndef test_sees_env():\n"
        "    assert os.environ.get('CI_DEPLOY_TOKEN') == 'super-secret-value'\n"
    )
    assert check_test_citation("tests/test_env.py::test_sees_env", repo, PYTEST).verified is False
    permissive = check_test_citation(
        "tests/test_env.py::test_sees_env", repo, PYTEST, scrub_env=False
    )
    assert permissive.verified is True


# ── Stopping a run in flight ─────────────────────────────────────────────────


async def test_an_error_during_evidence_does_not_leave_tests_switched_off(repo):
    """Stopping the tests in flight is how an interrupted run unwinds. The flag
    has to be cleared once it has, or it outlives the run and every test after
    it is reported as never having been run."""
    from arbiter.verify import off_loop

    def work():
        raise RuntimeError("evidence blew up")

    with pytest.raises(RuntimeError, match="evidence blew up"):
        await off_loop(work)

    check = check_test_citation("tests/test_app.py::test_passes", repo, PYTEST)
    assert check.outcome == evidence.PASSED


def test_a_stopped_run_starts_no_further_tests(repo):
    evidence.stop_running_tests()
    try:
        check = check_test_citation("tests/test_app.py::test_passes", repo, PYTEST)
    finally:
        evidence.allow_tests()

    assert check.outcome == evidence.ERRORED
    assert "interrupted" in check.detail
