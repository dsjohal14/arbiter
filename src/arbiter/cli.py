#!/usr/bin/env python3
"""arbiter — a falsification-first verification gate for AI-generated changes.

Drives locally installed CLI tools; no API keys needed.

    arbiter verify --plan plan.md --diff        # the gate
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
import sys
from typing import NoReturn

from . import display, doctor, verify
from .debate import run_debate, run_followup_loop
from .models import (
    DEFAULT_TIMEOUT,
    PROVIDERS,
    VALID_MODELS,
    ModelCallError,
    make_arbiter_session,
    make_debater_sessions,
)
from .prompts import JUDGE_SCHEMA
from .verify import (
    EXIT_FAIL,
    EXIT_PASS,
    EXIT_RUNTIME,
    EXIT_USAGE,
    VerifyInputError,
    make_verify_sessions,
)


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "-m",
        "--models",
        default=",".join(sorted(VALID_MODELS)),
        metavar="LIST",
        help="Comma-separated providers (default: all installed)",
    )
    p.add_argument(
        "-j",
        "--judge",
        default="claude",
        metavar="MODEL",
        help="Arbiter/consolidator provider (default: claude)",
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
        default="staged",
        metavar="SPEC",
        help="'staged' (default), 'working', a path, or '-' for stdin",
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

    doc = sub.add_parser("doctor", help="Check provider install, auth, and capabilities")
    doc.add_argument(
        "--no-probe", action="store_true", help="Check PATH only; skip the live authentication call"
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


def _resolve_models(spec: str, judge: str) -> tuple[list[str], str]:
    """Validate provider names. Unknown names are a usage error, not silently
    dropped — `--models claude,gpt4` used to run a one-model 'debate'."""
    keys, unknown = [], []
    for raw in spec.split(","):
        name = raw.strip().lower()
        if not name:
            continue
        (keys if name in VALID_MODELS else unknown).append(name)

    valid = ", ".join(sorted(VALID_MODELS))
    if unknown:
        _usage_error(f"unknown provider(s): {', '.join(unknown)}. Choose from: {valid}")
    if not keys:
        _usage_error(f"no providers selected. Choose from: {valid}")

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


async def cmd_verify(args: argparse.Namespace) -> int:
    keys, judge = _resolve_models(args.models, args.judge)
    isolation = "open" if args.allow_tools else "readonly"

    plan = verify.read_plan(args.plan)
    diff = verify.read_diff(args.diff)
    manifest = verify.build_manifest(diff)

    reviewers, consolidator = make_verify_sessions(keys, judge, isolation)
    names = {k: s.label() for k, s in reviewers.items()}
    consolidator_name = consolidator.label()

    if not args.as_json:
        display.print_note(
            f"Reviewing {len(diff.splitlines())} diff lines against {args.plan} · "
            f"{len(manifest)} file(s) in scope · reviewers: {', '.join(names.values())}"
        )

    result = await verify.run_verify(
        plan=plan,
        diff=diff,
        reviewers=reviewers,
        consolidator=consolidator,
        consolidator_name=consolidator_name,
        names=names,
        verbose=args.verbose,
        timeout=args.timeout,
        seed=args.seed,
        manifest=manifest,
    )

    if args.as_json:
        json.dump(result.as_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        verify.render(result, names)
    return result.exit_code


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
    results = await doctor.run_doctor(sorted(PROVIDERS), probe=not args.no_probe)
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
    if args.command == "debate":
        return await cmd_debate(args)
    if args.command == "doctor":
        return await cmd_doctor(args)
    raise SystemExit(EXIT_USAGE)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
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
    except SystemExit:
        raise
    except Exception as e:
        display.print_error(f"{type(e).__name__}: {e}")
        if args.verbose:
            raise
        return EXIT_RUNTIME


if __name__ == "__main__":
    sys.exit(main())
