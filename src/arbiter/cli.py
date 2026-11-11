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
import os
import shlex
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import NoReturn

from . import (
    __version__,
    config,
    display,
    doctor,
    evals,
    plan_review,
    progress,
    replay,
    run_records,
    usage,
    verify,
)
from . import criteria as crit
from .debate import run_debate, run_followup_loop
from .evidence import TEST_TIMEOUT
from .models import (
    DEFAULT_PANEL,
    DEFAULT_TIMEOUT,
    OPT_IN_PROVIDERS,
    PROVIDERS,
    VALID_MODELS,
    IsolationMode,
    ModelCallError,
    Session,
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


def _add_consolidate(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--consolidate",
        choices=config.CONSOLIDATE_MODES,
        default=None,
        help="When to make the consolidation call: auto (the default) skips it when "
        "there is nothing to reconcile — one reviewer, or reviewers that agree",
    )


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
        default=None,
        metavar="SEC",
        help=f"Per-call timeout in seconds (default: {DEFAULT_TIMEOUT:.0f})",
    )
    p.add_argument(
        "--retries",
        type=int,
        default=None,
        metavar="N",
        help="Times a failed provider call is tried again (default: 1; a timeout is not retried)",
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
        "--config",
        metavar="FILE",
        help=f"Project settings to use (default: {config.DEDICATED_FILE}, or "
        f"[tool.arbiter] in {config.PYPROJECT_FILE}, in this directory)",
    )
    p.add_argument(
        "--no-config",
        action="store_true",
        help="Ignore the project's settings file; use only flags and built-in defaults",
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
    parser.add_argument("-V", "--version", action="version", version=f"arbiter {__version__}")
    sub = parser.add_subparsers(dest="command")

    v = sub.add_parser("verify", help="Gate a diff against an approved plan")
    v.add_argument(
        "--plan",
        metavar="FILE",
        help="Approved plan (markdown file, or '-' for stdin). Required unless the "
        "project settings name one.",
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
    # Each of these can also come from the project settings, so each has a
    # negative form: a flag has to be able to switch off what a file switched on.
    v.add_argument(
        "--require-tests",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Treat an untested criterion as a failure rather than a warning",
    )
    v.add_argument(
        "--run-tests",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Execute cited tests (the default); --no-run-tests only checks they exist",
    )
    v.add_argument(
        "--require-pinned-evidence",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Require plan-selected, unchanged test evidence (implies --require-tests)",
    )
    v.add_argument(
        "--require-discriminating-tests",
        action=argparse.BooleanOptionalAction,
        default=None,
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
        "--test-timeout",
        type=float,
        default=None,
        metavar="SEC",
        help=f"Seconds one cited test may run before it is cut off (default: {TEST_TIMEOUT:.0f})",
    )
    v.add_argument(
        "--record",
        metavar="FILE",
        help="Write a sensitive, versioned JSON record of this run",
    )
    _add_consolidate(v)
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
    _add_consolidate(rev)
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


def _resolve_models(spec: str | list[str] | None, judge: str | None) -> tuple[list[str], str]:
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
        for raw in spec.split(",") if isinstance(spec, str) else spec:
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


def _parse_test_command(raw: str | list[str] | None) -> list[str] | None:
    """Split --test-command the way a shell would, and check it can start.

    `str.split` broke every argument that contained a space. And a command that
    could not be launched was only discovered after the reviewers had been paid
    for — as a run in which no cited test could be executed.

    A list, which only the settings file can supply, is already split.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            command = shlex.split(raw, posix=os.name != "nt")
        except ValueError as e:
            _usage_error(f"--test-command could not be parsed ({e}): {raw}")
    else:
        command = list(raw)
    if not command:
        _usage_error("--test-command is empty.")
    if shutil.which(command[0]) is None:
        _usage_error(
            f"--test-command: {command[0]!r} is not an executable on PATH, so no cited "
            "test could be run."
        )
    return command


# What each configurable setting falls back to when neither the command line
# nor the project file supplies it. argparse leaves an option that was not
# given at None, which is how "not given" stays distinguishable from a default.
_BUILT_IN: dict[str, object] = {
    "models": None,
    "judge": None,
    "timeout": DEFAULT_TIMEOUT,
    "retries": 1,
    "consolidate": "auto",
    "plan": None,
    "base": None,
    "require-tests": False,
    "require-pinned-evidence": False,
    "require-discriminating-tests": False,
    "run-tests": True,
    "test-command": None,
    "test-timeout": TEST_TIMEOUT,
}


def _apply_settings(args: argparse.Namespace) -> config.Config:
    """Fill in whatever the command line left unsaid: project file, then default.

    Precedence is flag, then file, then built-in. Only commands that call a
    model read the project file; the rest have nothing in it to read.
    """
    if not hasattr(args, "no_config"):
        return config.Config()
    if args.no_config and args.config:
        _usage_error("--config and --no-config cannot be used together.")
    try:
        settings = (
            config.Config()
            if args.no_config
            else config.load(Path(args.config) if args.config else None)
        )
    except config.ConfigError as e:
        _usage_error(f"{e} (Pass --no-config to run without a settings file.)")

    for key, built_in in _BUILT_IN.items():
        name = key.replace("-", "_")
        if hasattr(args, name) and getattr(args, name) is None:
            setattr(args, name, settings.values.get(key, built_in))

    if args.timeout <= 0:
        _usage_error("--timeout must be greater than zero.")
    if args.retries < 0:
        _usage_error("--retries cannot be negative.")
    if settings.source is not None:
        display.print_note(
            f"Settings from {settings.source} (flags on the command line override it)."
        )
    return settings


def _progress(args: argparse.Namespace) -> progress.Progress:
    """How this run reports what it is doing.

    `-v` keeps the raw provider streams and commands, which are for debugging
    and would fight a board for the terminal; everything else gets a live board
    where one can be drawn and one line per step where it cannot.
    """
    return progress.PlainProgress() if args.verbose else progress.for_terminal()


def _set_retries(sessions: list[Session], retries: int) -> None:
    for session in sessions:
        session.retries = retries


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
    retries: int = 0,
) -> list[crit.Criterion]:
    """Extract a complete in-memory contract without modifying the plan."""
    session = make_session(
        "extractor",
        judge,
        model="opus" if judge == "claude" else None,
        isolation=isolation,
        output_schema=crit.EXTRACTION_SCHEMA,
    )
    session.retries = retries
    display.print_note(f"Extracting baseline acceptance criteria with {session.label()}...")
    try:
        with schema_writer() as write_schema:
            resp = await call_session(
                session,
                crit.build_extraction_prompt(text),
                on_cmd=(lambda _key, cmd: display.print_verbose_cmd(cmd)) if verbose else None,
                timeout=timeout,
                schema_path_writer=write_schema,
                on_retry=display.retry_notice(session.label()),
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
    started = time.monotonic()
    # Kept whether or not a record is written: it is also what the run cost.
    trace = RunTrace()
    recorder = (
        RunRecorder(
            trace=trace,
            path=Path(args.record),
            command="plan review",
            inputs={"plan": recorded_input(text, args.file)},
            configuration={
                "models": keys,
                "judge": judge,
                "timeout_seconds": args.timeout,
                "retries": args.retries,
                "consolidate": args.consolidate,
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
                trace=trace,
                retries=args.retries,
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
        _set_retries([*reviewers.values(), consolidator], args.retries)
        names = {k: s.label() for k, s in reviewers.items()}

        if not args.as_json:
            display.print_note(
                f"Critiquing {args.file} ({len(text.splitlines())} lines, "
                f"{len(criteria)} {criteria_source} criteria) · "
                f"reviewers: {', '.join(names.values())}"
            )

        with _progress(args) as watch:
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
                trace=trace,
                consolidate=args.consolidate,
                progress=watch,
            )

        spent = usage.summarize(trace.calls)
        if args.as_json:
            json.dump({**result.as_dict(), "usage": spent}, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            plan_review.render(result, show_criteria=not args.apply)
            display.print_footer(usage.headline(spent, time.monotonic() - started))

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
        retries=args.retries,
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
    if not args.plan:
        _usage_error(
            "verify needs a plan: pass --plan FILE, or set `plan` in the project settings."
        )
    keys, judge = _resolve_models(args.models, args.judge)
    isolation = "open" if args.allow_tools else "readonly"
    base_ref = args.base or verify.resolve_base_ref(args.diff)

    # Reject unwinnable strict configurations before reading the diff or paying
    # for any provider calls. Fail-closed evidence semantics remain the backstop
    # for runtime failures after a valid configuration starts.
    if args.require_discriminating_tests:
        if not args.run_tests:
            _usage_error(
                "--require-discriminating-tests cannot be used with --no-run-tests; "
                "cited tests must run on both revisions."
            )
        if not base_ref:
            _usage_error(
                "--require-discriminating-tests needs a base revision; pass --base <ref> "
                "or use a --diff that names one."
            )
    if args.test_timeout <= 0:
        _usage_error("--test-timeout must be greater than zero.")
    # Unused when tests are not run, so a stale value there is not an error.
    test_command = _parse_test_command(args.test_command) if args.run_tests else None

    plan = verify.read_plan(args.plan)
    try:
        criteria = crit.read_criteria(plan)
    except crit.CriteriaError as e:
        _usage_error(str(e))

    diff = verify.read_diff(args.diff)
    manifest = verify.build_manifest(diff)

    workspace = None
    if args.record:
        try:
            workspace = run_records.workspace_identity(exclude=[Path(args.record)])
        except run_records.WorkspaceIdentityError as e:
            _usage_error(f"Could not safely record this workspace: {e}")

    started = time.monotonic()
    # Kept whether or not a record is written: it is also what the run cost.
    trace = RunTrace()
    recorder = (
        RunRecorder(
            trace=trace,
            path=Path(args.record),
            command="verify",
            inputs={
                "plan": recorded_input(plan, args.plan),
                "diff": recorded_input(diff, args.diff),
            },
            # Evidence can read unchanged tests, fixtures, helpers, and imports
            # outside the diff. The complete Git-visible tree (or complete
            # non-Git directory) is therefore the binding unit.
            workspace=workspace,
            configuration={
                "models": keys,
                "judge": judge,
                "timeout_seconds": args.timeout,
                "retries": args.retries,
                "consolidate": args.consolidate,
                "seed": args.seed,
                "isolation": isolation,
                "base_ref": base_ref,
                "run_tests": args.run_tests,
                "scrub_test_environment": not args.no_scrub_test_env,
                "require_tests": args.require_tests or args.require_pinned_evidence,
                "require_pinned_evidence": args.require_pinned_evidence,
                "require_discriminating_tests": args.require_discriminating_tests,
                "custom_test_command": args.test_command is not None,
                "test_timeout_seconds": args.test_timeout,
                "settings_file": str(args.settings.source) if args.settings.source else None,
            },
        )
        if args.record
        else None
    )

    reviewers, consolidator = make_verify_sessions(keys, judge, isolation)
    _set_retries([*reviewers.values(), consolidator], args.retries)
    names = {k: s.label() for k, s in reviewers.items()}

    if not args.as_json:
        display.print_note(
            f"{len(criteria)} criteria · {len(diff.splitlines())} diff lines · "
            f"{len(manifest)} file(s) in scope · reviewers: {', '.join(names.values())}"
        )

    try:
        with _progress(args) as watch:
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
                test_command=test_command,
                run_tests=args.run_tests,
                require_tests=args.require_tests or args.require_pinned_evidence,
                require_pinned_evidence=args.require_pinned_evidence,
                scrub_env=not args.no_scrub_test_env,
                base_ref=base_ref,
                require_discriminating_tests=args.require_discriminating_tests,
                trace=trace,
                test_timeout=args.test_timeout,
                consolidate=args.consolidate,
                progress=watch,
            )
    except Exception as e:
        _record_error(recorder, e)
        raise

    spent = usage.summarize(trace.calls)
    if args.as_json:
        json.dump({**result.as_dict(), "usage": spent}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        verify.render(result)
        display.print_footer(usage.headline(spent, time.monotonic() - started))
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
    elif not outcome.exact_comparison:
        display.print_error(
            "--check: this legacy record does not separate replay observations from "
            "expected output; record it again with the current schema."
        )
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
    """Refuse to score a record that was not produced from this case.

    Every check here fails closed. Scoring a run of some other plan, or of the
    same plan in some other tree, silently reports an accuracy number that means
    nothing — and an accuracy number that means nothing is the one output this
    whole layer exists to prevent.
    """
    inputs = record.get("inputs") or {}
    for name, content in (("plan", case.plan), ("diff", case.diff)):
        if content is None:
            continue
        filename = evals.PLAN_FILE if name == "plan" else evals.DIFF_FILE
        item = inputs.get(name)
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            _usage_error(
                f"{record_path} has no recorded {name}, so it cannot be shown to be a run "
                f"of {case.path / filename}."
            )

        # Recompute from the content rather than trusting the stored digest: a
        # claimed hash proves nothing about the bytes sitting next to it.
        actual = run_records.digest(item["content"])
        claimed = item.get("sha256")
        if claimed is not None and claimed != actual:
            _usage_error(
                f"{record_path} is corrupt: its recorded {name} does not match its own "
                f"sha256. Do not score it."
            )
        if actual != run_records.digest(content):
            _usage_error(
                f"{record_path} was recorded from a different {name} than "
                f"{case.path / filename}. "
                "Re-record the run from this case's inputs before scoring it."
            )

    _require_same_workspace(case, record, record_path)


def _require_same_workspace(case: evals.EvalCase, record: dict, record_path: Path) -> None:
    """Refuse to score a record whose evidence came from another checkout.

    The plan and diff do not identify the tree the evidence checks read, and two
    trees can hold the same diff and still make a cited test pass in one and
    fail in the other. A case that ships a fixture repo is asserting which tree
    the answer key was written against, so the record has to name the same one.
    """
    if case.repo is None:
        _usage_error(
            f"{case.path} is a verify case without {evals.REPO_DIR}/; there is no tree "
            "to bind its evidence to. Add the fixture directory and validate the case again."
        )

    workspace = record.get("workspace") or {}
    recorded = workspace.get("files")
    if (
        int(record.get("schema_version", 0)) < 3
        or workspace.get("scope") not in ("git-visible", "tree")
        or not isinstance(recorded, dict)
    ):
        _usage_error(
            f"{record_path} carries no complete workspace identity, so it cannot be shown to have "
            f"run against {case.path / evals.REPO_DIR}. Re-record it with this arbiter "
            "from inside that directory."
        )

    try:
        expected_workspace = run_records.workspace_identity(root=case.repo)
    except run_records.WorkspaceIdentityError as e:
        _usage_error(f"Could not safely identify {case.path / evals.REPO_DIR}: {e}")
    expected_scope = expected_workspace["scope"]
    if workspace["scope"] != expected_scope:
        _usage_error(
            f"{record_path} used workspace scope {workspace['scope']!r}, but "
            f"{case.path / evals.REPO_DIR} resolves as {expected_scope!r}. Record and "
            "score the case under the same Git/non-Git scope."
        )
    expected = expected_workspace["files"]
    if expected != recorded:
        differing = sorted(set(expected) ^ set(recorded)) or sorted(
            path for path, d in expected.items() if recorded.get(path) != d
        )
        _usage_error(
            f"{record_path} ran against a different tree than {case.path / evals.REPO_DIR} "
            f"({', '.join(differing[:5]) or 'contents differ'}). The evidence cannot be "
            "bound to this fixture, so the result cannot be scored against it."
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
    _set_retries([*debaters.values(), arbiter], args.retries)
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
    args.settings = _apply_settings(args)
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


class _Terminated(KeyboardInterrupt):
    """SIGTERM, delivered into the same cleanup path as Ctrl-C.

    Python's default for SIGTERM is to die where it stands, running no
    `finally`. A CI system cancels a job with exactly that signal, so every
    superseded run would leave its provider CLIs running and its temporary
    worktree registered. As a KeyboardInterrupt it unwinds instead: children
    are reaped, the worktree is removed, and a cited test in flight is killed.
    """


EXIT_TERMINATED = 128 + signal.SIGTERM


def _on_sigterm(_signum: int, _frame: object) -> None:
    # One is enough. A second, arriving mid-cleanup, would abort the cleanup
    # this handler exists to allow; whoever is impatient still has SIGKILL.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise _Terminated


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
    except SystemExit as e:  # argparse errors and --help
        return e.code if isinstance(e.code, int) else EXIT_USAGE
    try:
        previous = signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:  # not the main thread: the embedding program owns signals
        previous = None
    try:
        return asyncio.run(_dispatch(args))
    except _Terminated:
        display.print_error("Terminated.")
        return EXIT_TERMINATED
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
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(main())
