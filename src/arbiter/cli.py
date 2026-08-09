#!/usr/bin/env python3
"""arbiter — a falsification-first verification gate for AI-generated changes.

Drives locally installed CLI tools; no API keys needed.

    arbiter plan review plan.md                 # is this plan good enough to build?
    arbiter plan pin plan.md                    # extract + pin acceptance criteria
    arbiter verify --plan plan.md --diff        # the gate
    arbiter replay run.json                     # re-derive a recorded verdict, free
    arbiter eval score case/ --record run.json  # was that verdict right?
    arbiter debate "question"                   # multi-model debate + blinded judge
    arbiter doctor                              # provider install/auth/capability check

Exit codes are stable so `verify` can be wired into pre-commit or CI:
    0  pass          2  usage error
    1  fail          3  unclear          4  runtime error
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import NoReturn

from . import criteria as crit
from . import display, doctor, evals, plan_review, replay, run_records, verify
from .debate import run_debate, run_followup_loop
from .models import (
    DEFAULT_PANEL,
    DEFAULT_TIMEOUT,
    OPT_IN_PROVIDERS,
    PROVIDERS,
    VALID_MODELS,
    IsolationMode,
    ModelCallError,
    call_session,
    make_arbiter_session,
    make_debater_sessions,
    make_session,
)
from .prompts import JUDGE_SCHEMA
from .run_records import RunRecorder, RunTrace, recorded_input
from .schema_files import schema_writer
from .verify import (
    EXIT_FAIL,
    EXIT_PASS,
    EXIT_RUNTIME,
    EXIT_USAGE,
    VerifyInputError,
    make_verify_sessions,
)

# Preferred consolidator when it is installed: it is the one provider with both
# structured output and full config isolation.
DEFAULT_JUDGE = "claude"


def _add_output_flags(p: argparse.ArgumentParser) -> None:
    """For commands that call no models: they need the output flags and nothing
    else `_add_common` provides."""
    p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit a machine-readable JSON result on stdout",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Show more detail")


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "-m",
        "--models",
        default=None,
        metavar="LIST",
        help=f"Comma-separated providers (default: whichever of "
        f"{', '.join(sorted(DEFAULT_PANEL))} are installed; "
        f"{', '.join(sorted(OPT_IN_PROVIDERS))} must be named)",
    )
    p.add_argument(
        "-j",
        "--judge",
        default=None,
        metavar="MODEL",
        help=f"Arbiter/consolidator provider (default: {DEFAULT_JUDGE} if installed)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="SEC",
        help=f"Per-call timeout in seconds (default: {DEFAULT_TIMEOUT:.0f})",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=None,
        metavar="N",
        help="Blinding seed; reproduces an earlier run's shuffle exactly",
    )
    p.add_argument(
        "--allow-tools",
        action="store_true",
        help="Let providers use their full toolset (default: read-only where supported)",
    )
    p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit a machine-readable JSON result on stdout",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true", help="Show raw CLI commands and per-call timing"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="arbiter",
        description="Falsification-first verification gate for AI-generated plans and code.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit codes: 0 pass · 1 fail · 2 usage · 3 unclear · 4 runtime error",
    )
    sub = parser.add_subparsers(dest="command")

    v = sub.add_parser("verify", help="Gate a diff against an approved plan")
    v.add_argument(
        "--plan",
        required=True,
        metavar="FILE",
        help="Approved plan (markdown file, or '-' for stdin)",
    )
    v.add_argument(
        "--diff",
        nargs="?",
        const="staged",
        default="staged",
        metavar="SPEC",
        help="bare, or 'staged' (default), 'working', a revision range like "
        "origin/main...HEAD, a path, or '-' for stdin",
    )
    v.add_argument(
        "--require-tests",
        action="store_true",
        help="Treat an untested criterion as a failure rather than a warning",
    )
    v.add_argument(
        "--no-run-tests",
        action="store_true",
        help="Check that cited tests exist, but do not execute them",
    )
    v.add_argument(
        "--require-pinned-evidence",
        action="store_true",
        help="Require plan-selected, unchanged test evidence (implies --require-tests)",
    )
    v.add_argument(
        "--require-discriminating-tests",
        action="store_true",
        help="Re-run cited tests against the base revision; reject ones that pass there too",
    )
    v.add_argument(
        "--base",
        metavar="REF",
        help="Base revision for discrimination (default: inferred from --diff)",
    )
    v.add_argument(
        "--no-scrub-test-env",
        action="store_true",
        help="Let tests inherit the full environment (scrubbing is not a sandbox)",
    )
    v.add_argument(
        "--test-command",
        metavar="CMD",
        help="Test runner for cited tests (default: autodetect pytest)",
    )
    v.add_argument(
        "--record",
        metavar="FILE",
        help="Write a sensitive, versioned JSON record of this run",
    )
    _add_common(v)

    d = sub.add_parser("debate", help="Run a multi-model debate with a blinded judge")
    d.add_argument("prompt", nargs="?", help="The question (omit to read stdin or prompt)")
    d.add_argument(
        "-r",
        "--rounds",
        type=int,
        default=1,
        metavar="N",
        help="Number of debate rounds (default: 1)",
    )
    d.add_argument("--save", metavar="FILE", help="Write the full transcript (markdown) to FILE")
    d.add_argument(
        "--no-followup",
        action="store_true",
        help="Skip the interactive follow-up loop (implied when not a TTY)",
    )
    _add_common(d)

    pl = sub.add_parser("plan", help="Work with plan files")
    plsub = pl.add_subparsers(dest="plan_command")
    rev = plsub.add_parser("review", help="Critique a plan before it is built from")
    rev.add_argument("file", help="Plan markdown file to critique")
    rev.add_argument(
        "--apply",
        action="store_true",
        help="Write the proposed criteria into the plan (review them first)",
    )
    rev.add_argument(
        "--record",
        metavar="FILE",
        help="Write a sensitive, versioned JSON record of this run",
    )
    _add_common(rev)

    pin = plsub.add_parser("pin", help="Extract acceptance criteria and pin them into the plan")
    pin.add_argument("file", help="Plan markdown file to pin criteria into")
    pin.add_argument("--force", action="store_true", help="Re-extract even if already pinned")
    pin.add_argument("--dry-run", action="store_true", help="Print the criteria, do not write")
    _add_common(pin)

    rp = sub.add_parser(
        "replay", help="Re-derive a recorded run's result without calling any model"
    )
    rp.add_argument("record", help="Run record written by --record")
    rp.add_argument(
        "--check",
        action="store_true",
        help="Exit 0 only if the replay reproduces the recorded result exactly",
    )
    _add_output_flags(rp)

    ev = sub.add_parser("eval", help="Score runs against hand-labelled evaluation cases")
    evsub = ev.add_subparsers(dest="eval_command")
    sc = evsub.add_parser("score", help="Score a run record against one case's answer key")
    sc.add_argument("case", help="Evaluation case directory")
    sc.add_argument(
        "--record",
        required=True,
        metavar="FILE",
        help="Run record produced from this case's plan and diff",
    )
    _add_output_flags(sc)

    val = evsub.add_parser("validate", help="Check that case directories are well formed")
    val.add_argument("cases", nargs="+", help="Evaluation case directories")
    _add_output_flags(val)

    doc = sub.add_parser("doctor", help="Check provider install, auth, and capabilities")
    doc.add_argument(
        "--no-probe", action="store_true", help="Check PATH only; skip the live authentication call"
    )
    doc.add_argument(
        "--all",
        dest="check_all",
        action="store_true",
        help=f"Also check opt-in providers ({', '.join(sorted(OPT_IN_PROVIDERS))})",
    )
    doc.add_argument(
        "--json", dest="as_json", action="store_true", help="Emit a machine-readable JSON report"
    )

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        raise SystemExit(EXIT_USAGE)
    return args


def _usage_error(msg: str) -> NoReturn:
    """Exit 2, not 1. `SystemExit("message")` exits 1, which collides with the
    `fail` verdict — a CI job could not tell a real failure from a typo."""
    display.print_error(msg)
    raise SystemExit(EXIT_USAGE)


def _installed_providers(panel_only: bool = False) -> list[str]:
    candidates = sorted(DEFAULT_PANEL) if panel_only else sorted(VALID_MODELS)
    return [k for k in candidates if shutil.which(PROVIDERS[k].cli)]


def _resolve_models(spec: str | None, judge: str | None) -> tuple[list[str], str]:
    """Validate provider names. Unknown names are a usage error, not silently
    dropped — `--models claude,gpt4` used to run a one-model 'debate'.

    With no `--models`, the panel is the default providers that are actually on
    PATH, and the ones that are not are named rather than sent to fail one by
    one. Ask for a provider explicitly and it stays strict: a reviewer you
    requested and did not get is a missing opinion, and the verdict has to know
    that. An opt-in provider is only ever on the panel because you named it.
    """
    valid = ", ".join(sorted(VALID_MODELS))

    if spec is None:
        keys = _installed_providers(panel_only=True)
        if not keys:
            _usage_error(
                f"No default provider CLI found on PATH. Install one of: "
                f"{', '.join(sorted(DEFAULT_PANEL))}. Run 'arbiter doctor' for install hints."
            )
        if missing := [k for k in sorted(DEFAULT_PANEL) if k not in keys]:
            display.print_note(
                f"Not installed, so not on this panel: {', '.join(missing)}. "
                "Name them with --models to require them."
            )
    else:
        keys, unknown = [], []
        for raw in spec.split(","):
            name = raw.strip().lower()
            if not name:
                continue
            (keys if name in VALID_MODELS else unknown).append(name)
        if unknown:
            _usage_error(f"unknown provider(s): {', '.join(unknown)}. Choose from: {valid}")
        if not keys:
            _usage_error(f"no providers selected. Choose from: {valid}")

    if judge is None:
        # The consolidator is degradable — losing it costs precision, not the
        # run — so an absent default judge picks a peer rather than failing.
        installed = _installed_providers(panel_only=True)
        judge = DEFAULT_JUDGE if DEFAULT_JUDGE in installed else (installed or keys)[0]
    judge = judge.strip().lower()
    if judge not in VALID_MODELS:
        _usage_error(f"unknown judge '{judge}'. Choose from: {valid}")
    return keys, judge


def _read_prompt(arg: str | None) -> str:
    if arg:
        return arg.strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    try:
        return input(display.prompt_label("Your prompt")).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(130) from None


def _write_transcript(path: str, transcript: list[str]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("".join(transcript))
        display.print_note(f"✎  Transcript saved to {path}")
    except OSError as e:
        display.print_error(f"Could not write {path}: {e}")


# ── Commands ──────────────────────────────────────────────────────────────────


async def _extract_acceptance_criteria(
    text: str,
    judge: str,
    isolation: IsolationMode,
    verbose: bool,
    timeout: float,
    trace: RunTrace | None = None,
) -> list[crit.Criterion]:
    """Extract a complete in-memory contract without modifying the plan."""
    session = make_session(
        "extractor",
        judge,
        model="opus" if judge == "claude" else None,
        isolation=isolation,
        output_schema=crit.EXTRACTION_SCHEMA,
    )
    display.print_note(f"Extracting baseline acceptance criteria with {session.label()}...")
    try:
        with schema_writer() as write_schema:
            resp = await call_session(
                session,
                crit.build_extraction_prompt(text),
                on_cmd=(lambda _key, cmd: display.print_verbose_cmd(cmd)) if verbose else None,
                timeout=timeout,
                schema_path_writer=write_schema,
            )
        if trace is not None:
            trace.success("extractor", session, resp)
    except Exception as e:
        if trace is not None:
            trace.failure("extractor", session, e)
        raise
    return crit.parse_extraction(resp.content)


def _finish_record(
    recorder: RunRecorder | None,
    *,
    exit_code: int,
    result: dict | None,
    error: Exception | str | None = None,
) -> None:
    if recorder is None:
        return
    recorder.finish(exit_code=exit_code, result=result, error=error)
    display.print_note(f"Run record written to {recorder.path}")


def _record_error(recorder: RunRecorder | None, error: Exception) -> None:
    """Preserve the original failure if writing its diagnostic record also fails."""
    if recorder is None:
        return
    try:
        _finish_record(recorder, exit_code=EXIT_RUNTIME, result=None, error=error)
    except OSError as record_error:
        display.print_error(f"Could not write run record {recorder.path}: {record_error}")


async def cmd_plan_review(args: argparse.Namespace) -> int:
    """Critique a plan before anything is built from it.

    Exits non-zero while blocking problems remain, so an agent skill can be told
    not to start implementing until this passes.
    """
    keys, judge = _resolve_models(args.models, args.judge)
    path = Path(args.file)
    if not path.is_file():
        _usage_error(f"Plan file not found: {args.file}")

    text = path.read_text(encoding="utf-8")
    if not text.strip():
        _usage_error("The plan is empty.")

    isolation = "open" if args.allow_tools else "readonly"
    recorder = (
        RunRecorder(
            path=Path(args.record),
            command="plan review",
            inputs={"plan": recorded_input(text, args.file)},
            configuration={
                "models": keys,
                "judge": judge,
                "timeout_seconds": args.timeout,
                "seed": args.seed,
                "isolation": isolation,
                "apply": args.apply,
            },
        )
        if args.record
        else None
    )

    try:
        if crit.is_pinned(text):
            criteria = crit.read_criteria(text)
            criteria_source = "pinned"
        else:
            # Reviewers propose edits, so an unpinned plan first needs a complete
            # baseline to edit. This stays in memory until --apply is requested.
            criteria = await _extract_acceptance_criteria(
                text,
                judge,
                isolation,
                args.verbose,
                args.timeout,
                trace=recorder.trace if recorder else None,
            )
            if not criteria:
                message = "No acceptance criteria could be extracted from this plan."
                display.print_error(message)
                _finish_record(recorder, exit_code=EXIT_RUNTIME, result=None, error=message)
                return EXIT_RUNTIME
            criteria_source = "extracted"
        if recorder is not None:
            recorder.configuration["criteria_source"] = criteria_source

        reviewers, consolidator = plan_review.make_review_sessions(keys, judge, isolation)
        names = {k: s.label() for k, s in reviewers.items()}

        if not args.as_json:
            display.print_note(
                f"Critiquing {args.file} ({len(text.splitlines())} lines, "
                f"{len(criteria)} {criteria_source} criteria) · "
                f"reviewers: {', '.join(names.values())}"
            )

        result = await plan_review.run_plan_review(
            plan=text,
            criteria=criteria,
            reviewers=reviewers,
            consolidator=consolidator,
            consolidator_name=consolidator.label(),
            names=names,
            verbose=args.verbose,
            timeout=args.timeout,
            seed=args.seed,
            trace=recorder.trace if recorder else None,
        )

        if args.as_json:
            json.dump(result.as_dict(), sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            plan_review.render(result, show_criteria=not args.apply)

        if args.apply:
            if not result.criteria:
                message = "Nothing to apply: no criteria were produced."
                display.print_error(message)
                _finish_record(
                    recorder, exit_code=EXIT_RUNTIME, result=result.as_dict(), error=message
                )
                return EXIT_RUNTIME
            path.write_text(crit.pin_into(text, result.criteria), encoding="utf-8")
            display.print_note(
                f"✎  Wrote {len(result.criteria)} criteria into {args.file}. "
                "Read them before implementing — they are the contract."
            )
    except Exception as e:
        _record_error(recorder, e)
        raise

    _finish_record(recorder, exit_code=result.exit_code, result=result.as_dict())
    return result.exit_code


async def cmd_plan_pin(args: argparse.Namespace) -> int:
    """Extract acceptance criteria once and pin them into the plan.

    Kept out of `verify` on purpose: this writes to the user's plan file, and a
    verification run should never mutate its own input.
    """
    _, judge = _resolve_models(args.models, args.judge)
    path = Path(args.file)
    if not path.is_file():
        _usage_error(f"Plan file not found: {args.file}")

    text = path.read_text(encoding="utf-8")
    if crit.is_pinned(text) and not args.force:
        existing = crit.read_criteria(text)
        display.print_note(
            f"Already pinned with {len(existing)} criteria. Use --force to re-extract."
        )
        for c in existing:
            display.print(f"  {c.id}  {c.text}")
        return EXIT_PASS

    criteria = await _extract_acceptance_criteria(
        text,
        judge,
        "open" if args.allow_tools else "readonly",
        args.verbose,
        args.timeout,
    )
    if not criteria:
        display.print_error("No acceptance criteria could be extracted from this plan.")
        return EXIT_RUNTIME

    display.print_section(f"{len(criteria)} acceptance criteria")
    for c in criteria:
        display.print(f"  {display._c(c.id, display.BOLD)}  {c.text}")
    display.print()

    if args.dry_run:
        display.print_note("--dry-run: plan not modified.")
        return EXIT_PASS

    path.write_text(crit.pin_into(text, criteria), encoding="utf-8")
    display.print_note(f"✎  Pinned into {args.file}. Review and edit them before verifying.")
    return EXIT_PASS


async def cmd_verify(args: argparse.Namespace) -> int:
    keys, judge = _resolve_models(args.models, args.judge)
    isolation = "open" if args.allow_tools else "readonly"
    base_ref = args.base or verify.resolve_base_ref(args.diff)

    # Reject unwinnable strict configurations before reading the diff or paying
    # for any provider calls. Fail-closed evidence semantics remain the backstop
    # for runtime failures after a valid configuration starts.
    if args.require_discriminating_tests:
        if args.no_run_tests:
            _usage_error(
                "--require-discriminating-tests cannot be used with --no-run-tests; "
                "cited tests must run on both revisions."
            )
        if not base_ref:
            _usage_error(
                "--require-discriminating-tests needs a base revision; pass --base <ref> "
                "or use a --diff that names one."
            )

    plan = verify.read_plan(args.plan)
    try:
        criteria = crit.read_criteria(plan)
    except crit.CriteriaError as e:
        _usage_error(str(e))

    diff = verify.read_diff(args.diff)
    manifest = verify.build_manifest(diff)

    recorder = (
        RunRecorder(
            path=Path(args.record),
            command="verify",
            inputs={
                "plan": recorded_input(plan, args.plan),
                "diff": recorded_input(diff, args.diff),
            },
            configuration={
                "models": keys,
                "judge": judge,
                "timeout_seconds": args.timeout,
                "seed": args.seed,
                "isolation": isolation,
                "base_ref": base_ref,
                "run_tests": not args.no_run_tests,
                "scrub_test_environment": not args.no_scrub_test_env,
                "require_tests": args.require_tests or args.require_pinned_evidence,
                "require_pinned_evidence": args.require_pinned_evidence,
                "require_discriminating_tests": args.require_discriminating_tests,
                "custom_test_command": args.test_command is not None,
            },
        )
        if args.record
        else None
    )

    reviewers, consolidator = make_verify_sessions(keys, judge, isolation)
    names = {k: s.label() for k, s in reviewers.items()}

    if not args.as_json:
        display.print_note(
            f"{len(criteria)} criteria · {len(diff.splitlines())} diff lines · "
            f"{len(manifest)} file(s) in scope · reviewers: {', '.join(names.values())}"
        )

    try:
        result = await verify.run_verify(
            plan=plan,
            diff=diff,
            criteria=criteria,
            reviewers=reviewers,
            consolidator=consolidator,
            consolidator_name=consolidator.label(),
            names=names,
            verbose=args.verbose,
            timeout=args.timeout,
            seed=args.seed,
            manifest=manifest,
            test_command=args.test_command.split() if args.test_command else None,
            run_tests=not args.no_run_tests,
            require_tests=args.require_tests or args.require_pinned_evidence,
            require_pinned_evidence=args.require_pinned_evidence,
            scrub_env=not args.no_scrub_test_env,
            base_ref=base_ref,
            require_discriminating_tests=args.require_discriminating_tests,
            trace=recorder.trace if recorder else None,
        )
    except Exception as e:
        _record_error(recorder, e)
        raise

    if args.as_json:
        json.dump(result.as_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        verify.render(result)
    _finish_record(recorder, exit_code=result.exit_code, result=result.as_dict())
    return result.exit_code


def cmd_replay(args: argparse.Namespace) -> int:
    """Re-derive a recorded run's result from the responses it captured.

    Calls nothing and costs nothing, so a change to parsing or verdict logic can
    be checked against runs that already happened.
    """
    path = Path(args.record)
    try:
        outcome = replay.replay(replay.load_record(path))
    except replay.ReplayError as e:
        _usage_error(str(e))

    if args.as_json:
        json.dump(outcome.as_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        replay.render(outcome, path)

    if not args.check:
        return outcome.exit_code
    if outcome.matches:
        return EXIT_PASS
    if not outcome.comparable:
        display.print_error("--check: the record has no result to reproduce.")
    return EXIT_FAIL


def cmd_eval_score(args: argparse.Namespace) -> int:
    """Score one recorded run against a case's hand-labelled answer key."""
    try:
        case = evals.load_case(Path(args.case))
    except evals.EvalCaseError as e:
        _usage_error(str(e))

    record_path = Path(args.record)
    try:
        record = replay.load_record(record_path)
    except replay.ReplayError as e:
        _usage_error(str(e))

    if record["command"] != case.command:
        _usage_error(
            f"{record_path} records `arbiter {record['command']}`, but "
            f"{case.id} is a {case.command} case."
        )
    _require_same_inputs(case, record, record_path)

    try:
        outcome = replay.replay(record)
    except replay.ReplayError as e:
        _usage_error(str(e))

    result = outcome.result.as_dict()
    scored = evals.score(case, result)

    if args.as_json:
        json.dump({**scored.as_dict(), "drift": outcome.drift}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        display.print_case_score(scored.as_dict())
        if outcome.drift:
            display.print_error(
                f"Scored the replay, which differs from the recorded result in "
                f"{len(outcome.drift)} place(s); run `arbiter replay {record_path}` to see them."
            )
    return EXIT_PASS if scored.passed else EXIT_FAIL


def _require_same_inputs(case: evals.EvalCase, record: dict, record_path: Path) -> None:
    """Refuse to score a record that was produced from different inputs.

    Scoring a run of some other plan against this answer key would silently
    report an accuracy number that means nothing.
    """
    inputs = record.get("inputs") or {}
    for name, content in (("plan", case.plan), ("diff", case.diff)):
        if content is None:
            continue
        recorded = (inputs.get(name) or {}).get("sha256")
        if recorded and recorded != run_records.digest(content):
            _usage_error(
                f"{record_path} was recorded from a different {name} than "
                f"{case.path / (evals.PLAN_FILE if name == 'plan' else evals.DIFF_FILE)}. "
                "Re-record the run from this case's inputs before scoring it."
            )


def cmd_eval_validate(args: argparse.Namespace) -> int:
    """Check every named case directory before it is used to measure anything."""
    reports: list[dict] = []
    for raw in args.cases:
        path = Path(raw)
        try:
            case = evals.load_case(path)
        except evals.EvalCaseError as e:
            reports.append({"case": path.name, "path": str(path), "ok": False, "error": str(e)})
            continue
        reports.append(
            {
                "case": case.id,
                "path": str(path),
                "ok": True,
                "command": case.command,
                "verdict": case.verdict,
                "repo": str(case.repo) if case.repo else None,
                "labelled_criteria": len(case.criteria),
                "must_find": len(case.must_find),
                "must_not_find": len(case.must_not_find),
            }
        )

    if args.as_json:
        json.dump(reports, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        for r in reports:
            if r["ok"]:
                display.print_note(
                    f"✓  {r['case']} ({r['command']}) · {r['labelled_criteria']} labelled "
                    f"criteria · {r['must_find']} must-find · {r['must_not_find']} must-not-find"
                )
            else:
                display.print_error(f"{r['path']}: {r['error']}")
    return EXIT_PASS if all(r["ok"] for r in reports) else EXIT_FAIL


async def cmd_debate(args: argparse.Namespace) -> int:
    keys, judge = _resolve_models(args.models, args.judge)
    if args.rounds < 1:
        _usage_error("--rounds must be at least 1.")

    isolation = "open" if args.allow_tools else "readonly"
    prompt = _read_prompt(args.prompt)
    if not prompt:
        _usage_error("prompt cannot be empty.")

    debaters = make_debater_sessions(keys, isolation=isolation)
    arbiter = make_arbiter_session(judge, isolation=isolation, output_schema=JUDGE_SCHEMA)
    names = {k: s.label() for k, s in debaters.items()}
    arbiter_name = arbiter.label()
    transcript: list[str] = []

    if not args.as_json:
        display.print_banner(names, keys, arbiter_name, args.rounds)

    try:
        result = await run_debate(
            prompt=prompt,
            debaters=debaters,
            arbiter=arbiter,
            arbiter_name=arbiter_name,
            num_rounds=args.rounds,
            verbose=args.verbose,
            transcript=transcript,
            names=names,
            timeout=args.timeout,
            seed=args.seed,
        )
        if args.as_json:
            json.dump(
                {
                    "winner": result.verdict.winner,
                    "reason": result.verdict.reason,
                    "best_answer": result.verdict.best_answer,
                    "blinding": result.verdict.blinding.as_dict()
                    if result.verdict.blinding
                    else None,
                },
                sys.stdout,
                indent=2,
            )
            sys.stdout.write("\n")
            return EXIT_PASS

        display.print_winner(result.verdict, arbiter_name, names)

        interactive = sys.stdin.isatty() and not args.no_followup
        if interactive:
            await run_followup_loop(
                debaters, arbiter, args.verbose, result, transcript, names, args.timeout
            )
        return EXIT_PASS
    finally:
        if args.save and transcript:
            _write_transcript(args.save, transcript)


async def cmd_doctor(args: argparse.Namespace) -> int:
    keys = sorted(PROVIDERS) if args.check_all else sorted(DEFAULT_PANEL)
    results = await doctor.run_doctor(keys, probe=not args.no_probe)
    if args.as_json:
        json.dump([h.as_dict() for h in results], sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        doctor.render(results)
    return EXIT_PASS if all(h.ok for h in results) else EXIT_FAIL


# ── Entry point ───────────────────────────────────────────────────────────────


async def _dispatch(args: argparse.Namespace) -> int:
    # In --json mode stdout carries the result and nothing else; progress and
    # live streaming go to stderr, so `arbiter verify --json | jq` just works.
    display.set_json_mode(getattr(args, "as_json", False))
    if args.command == "verify":
        return await cmd_verify(args)
    if args.command == "plan":
        if args.plan_command == "review":
            return await cmd_plan_review(args)
        if args.plan_command == "pin":
            return await cmd_plan_pin(args)
        _usage_error("usage: arbiter plan review <plan.md> | arbiter plan pin <plan.md>")
    if args.command == "replay":
        return cmd_replay(args)
    if args.command == "eval":
        if args.eval_command == "score":
            return cmd_eval_score(args)
        if args.eval_command == "validate":
            return cmd_eval_validate(args)
        _usage_error(
            "usage: arbiter eval score <case-dir> --record <run.json> | "
            "arbiter eval validate <case-dir>..."
        )
    if args.command == "debate":
        return await cmd_debate(args)
    if args.command == "doctor":
        return await cmd_doctor(args)
    raise SystemExit(EXIT_USAGE)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
    except SystemExit as e:  # argparse errors and --help
        return e.code if isinstance(e.code, int) else EXIT_USAGE
    try:
        return asyncio.run(_dispatch(args))
    except KeyboardInterrupt:
        print()
        display.print_error("Interrupted.")
        return 130
    except VerifyInputError as e:
        display.print_error(str(e))
        return EXIT_USAGE
    except ModelCallError as e:
        display.print_error(str(e))
        return EXIT_RUNTIME
    except SystemExit as e:
        # main() always *returns* its exit code rather than sometimes returning
        # and sometimes raising, so callers (and tests) have one contract.
        return e.code if isinstance(e.code, int) else EXIT_USAGE
    except Exception as e:
        display.print_error(f"{type(e).__name__}: {e}")
        if args.verbose:
            raise
        return EXIT_RUNTIME


if __name__ == "__main__":
    sys.exit(main())
