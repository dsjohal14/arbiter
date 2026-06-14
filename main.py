#!/usr/bin/env python3
"""
arbiter — multi-LLM debate arena (CLI edition)

Debaters argue, then an Opus 4.8 arbiter picks the winner. Uses locally
installed CLI tools — no API keys needed:
  claude  →  Claude Code  (Sonnet debater / Opus arbiter)
  codex   →  Codex CLI
  gemini  →  Gemini CLI    (Gemini Pro)

Usage:
    python main.py
    python main.py --rounds 2
    python main.py --models claude,codex --save debate.md --verbose
"""

import argparse
import asyncio
import sys

import display
from debate import run_debate, run_followup_loop
from models import (
    VALID_MODELS,
    ARBITER_NAMES,
    make_debater_sessions,
    make_arbiter_session,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="arbiter",
        description="Multi-LLM debate using local CLI tools (Claude, Codex, Gemini).",
    )
    parser.add_argument("-r", "--rounds", type=int, default=1, metavar="N",
                        help="Number of debate rounds (default: 1)")
    parser.add_argument("-j", "--judge", default="claude", metavar="MODEL",
                        help="Arbiter model: claude | codex | gemini (default: claude → Opus 4.8)")
    parser.add_argument("-m", "--models", default="claude,codex,gemini", metavar="LIST",
                        help="Comma-separated debaters (default: claude,codex,gemini)")
    parser.add_argument("--save", metavar="FILE",
                        help="Write the full transcript (markdown) to FILE on exit")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show raw CLI commands and per-call timing")
    return parser.parse_args()


def _write_transcript(path: str, transcript: list[str]) -> None:
    try:
        with open(path, "w") as f:
            f.write("".join(transcript))
        print(display._c(f"  ✎  Transcript saved to {path}", display.DIM))
    except OSError as e:
        display.print_error(f"Could not write {path}: {e}")


async def main() -> None:
    args = parse_args()

    judge = args.judge.lower().strip()
    if judge not in VALID_MODELS:
        print(f"  Unknown judge '{judge}'. Choose: {', '.join(sorted(VALID_MODELS))}", file=sys.stderr)
        sys.exit(1)

    debater_keys = [
        m.strip().lower()
        for m in args.models.split(",")
        if m.strip().lower() in VALID_MODELS
    ]
    if not debater_keys:
        print("  No valid models specified.", file=sys.stderr)
        sys.exit(1)

    if args.rounds < 1:
        print("  --rounds must be at least 1.", file=sys.stderr)
        sys.exit(1)

    arbiter_name = ARBITER_NAMES[judge]
    display.print_banner(debater_keys, arbiter_name, args.rounds)

    try:
        prompt = input("\033[1m\033[96m  Your prompt:\033[0m ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    if not prompt:
        print("  Prompt cannot be empty.", file=sys.stderr)
        sys.exit(1)

    debaters = make_debater_sessions(debater_keys)
    arbiter = make_arbiter_session(judge)
    transcript: list[str] = []

    try:
        result = await run_debate(
            prompt, debaters, arbiter, judge, args.rounds, args.verbose, transcript
        )
        display.print_winner(result.winner, result.reason, result.best_answer, result.arbiter_name)
        await run_followup_loop(debaters, arbiter, args.verbose, result, transcript)
    except KeyboardInterrupt:
        print("\n  Interrupted.")
    except Exception as e:
        display.print_error(str(e))
    finally:
        if args.save and transcript:
            _write_transcript(args.save, transcript)


if __name__ == "__main__":
    asyncio.run(main())
