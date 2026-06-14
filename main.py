#!/usr/bin/env python3
"""
debate — multi-LLM debate arena (CLI edition)

Uses locally installed CLI tools — no API keys needed:
  claude  →  Claude Code  (claude -p "...")
  codex   →  Codex CLI    (codex exec "...")
  gemini  →  Gemini CLI   (gemini -p "...")

Usage:
    python main.py
    python main.py --rounds 3 --judge gemini
    python main.py --rounds 1 --models claude,codex
"""

import argparse
import asyncio
import sys

import display
from debate import run_debate

VALID_MODELS = {"claude", "codex", "gemini"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="debate",
        description="Multi-LLM debate using local CLI tools (Claude Code, Codex, Gemini CLI).",
    )
    parser.add_argument(
        "-r", "--rounds",
        type=int,
        default=2,
        metavar="N",
        help="Number of debate rounds (default: 2)",
    )
    parser.add_argument(
        "-j", "--judge",
        default="claude",
        metavar="MODEL",
        help="Judge model: claude | codex | gemini (default: claude)",
    )
    parser.add_argument(
        "-m", "--models",
        default="claude,codex,gemini",
        metavar="LIST",
        help="Comma-separated debaters (default: claude,codex,gemini)",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    judge = args.judge.lower().strip()
    if judge not in VALID_MODELS:
        print(f"  Unknown judge '{judge}'. Choose: {', '.join(sorted(VALID_MODELS))}", file=sys.stderr)
        sys.exit(1)

    debaters = [
        m.strip().lower()
        for m in args.models.split(",")
        if m.strip().lower() in VALID_MODELS
    ]
    if not debaters:
        print("  No valid models specified.", file=sys.stderr)
        sys.exit(1)

    if args.rounds < 1:
        print("  --rounds must be at least 1.", file=sys.stderr)
        sys.exit(1)

    display.print_banner(debaters, judge, args.rounds)

    try:
        prompt = input("\033[1m\033[96m  Your prompt:\033[0m ")
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    prompt = prompt.strip()
    if not prompt:
        print("  Prompt cannot be empty.", file=sys.stderr)
        sys.exit(1)

    try:
        result = await run_debate(prompt, debaters, judge, args.rounds)
        display.print_winner(result.winner, result.reason, result.best_answer, result.judge)
    except KeyboardInterrupt:
        print("\n  Interrupted.")
    except Exception as e:
        display.print_error(str(e))
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
