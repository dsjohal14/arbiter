"""Terminal display helpers — colors, banners, streaming answer blocks."""

import sys

# ANSI color codes
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
UNDER  = "\033[4m"

BLACK   = "\033[30m"
RED     = "\033[91m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
BLUE    = "\033[94m"
MAGENTA = "\033[95m"
CYAN    = "\033[96m"
WHITE   = "\033[97m"


def _c(text: str, *codes: str) -> str:
    return "".join(codes) + str(text) + RESET


MODEL_COLORS = {
    "claude":  MAGENTA,
    "codex":   CYAN,
    "gemini":  YELLOW,
    "arbiter": GREEN,
}

DISPLAY_NAMES = {
    "claude":  "Claude Sonnet",
    "codex":   "Codex",
    "gemini":  "Gemini Pro",
    "arbiter": "Opus 4.8",
}


def model_label(model: str, name: str | None = None) -> str:
    color = MODEL_COLORS.get(model, WHITE)
    return _c(name or DISPLAY_NAMES.get(model, model), BOLD, color)


def print_banner(debaters: list[str], arbiter_name: str, rounds: int) -> None:
    print()
    print(_c("╔══════════════════════════════════════════╗", BLUE))
    print(_c("║       🤖  LLM DEBATE ARENA  🤖           ║", BLUE))
    print(_c("╚══════════════════════════════════════════╝", BLUE))
    print()
    print(f"  {_c('Debaters:', BOLD)}  {('  vs  ').join(model_label(m) for m in debaters)}")
    print(f"  {_c('Arbiter: ', BOLD)}  {model_label('arbiter', arbiter_name)}")
    print(f"  {_c('Rounds:  ', BOLD)}  {_c(rounds, BOLD, YELLOW)}")
    print()


def print_round_header(round_num: int, total: int) -> None:
    print()
    print(_c(f"━━━  ROUND {round_num}/{total}  " + "━" * 32, BOLD, YELLOW))


def print_judging(arbiter_name: str) -> None:
    print()
    print(_c("━━━  JUDGMENT  " + "━" * 36, BOLD, MAGENTA))
    print(f"  {model_label('arbiter', arbiter_name)} {_c('is deliberating...', DIM)}")
    print()


# ── Live streaming blocks ─────────────────────────────────────────────────────
# A block is one model's answer, rendered live behind a colored "│" bar. Because
# chunks arrive without respect to line boundaries, we track whether we're at the
# start of a line so the bar is prefixed exactly once per line.

_block = {"at_line_start": True, "bar": ""}


def begin_block(model: str, round_num: int | None = None, name: str | None = None) -> None:
    print()
    suffix = f" (round {round_num})" if round_num is not None else ""
    print(f"  {_c('▶ ' + (name or DISPLAY_NAMES.get(model, model)) + suffix, BOLD, MODEL_COLORS.get(model, WHITE))}")
    _block["bar"] = _c("  │ ", MODEL_COLORS.get(model, WHITE))
    _block["at_line_start"] = True


def stream_chunk(text: str) -> None:
    parts: list[str] = []
    for ch in text:
        if _block["at_line_start"]:
            parts.append(_block["bar"])
            _block["at_line_start"] = False
        parts.append(ch)
        if ch == "\n":
            _block["at_line_start"] = True
    sys.stdout.write("".join(parts))
    sys.stdout.flush()


def end_block() -> None:
    if not _block["at_line_start"]:
        sys.stdout.write("\n")
    sys.stdout.write("\n")
    sys.stdout.flush()
    _block["at_line_start"] = True


def print_winner(winner: str, reason: str, best_answer: str, arbiter_name: str) -> None:
    name = DISPLAY_NAMES.get(winner, winner).upper()
    print(_c("╔══════════════════════════════════════════╗", GREEN))
    print(_c(f"║  🏆  WINNER: {name:<29}║", BOLD, GREEN))
    print(_c("╚══════════════════════════════════════════╝", GREEN))
    print()
    print(f"  {_c('Judged by:', BOLD)} {model_label('arbiter', arbiter_name)}")
    print()
    print(f"  {_c('Why this answer won:', BOLD, UNDER)}")
    for line in reason.strip().splitlines():
        print(f"  {line}")
    print()
    print(_c("━━━  BEST ANSWER  " + "━" * 33, BOLD, GREEN))
    print()
    bar = _c("  │ ", MODEL_COLORS.get(winner, WHITE))
    for line in best_answer.strip().splitlines():
        print(f"{bar}{line}")
    print()


def print_arbiter_reply(text: str, arbiter_name: str) -> None:
    print()
    print(_c(f"━━━  {arbiter_name.upper()}  " + "━" * 34, BOLD, GREEN))
    print()
    bar = _c("  │ ", GREEN)
    for line in text.strip().splitlines():
        print(f"{bar}{line}")
    print()


def print_repoll(question: str) -> None:
    print()
    print(_c(f"  ↻  Re-polling debaters: {question}", BOLD, YELLOW))


def print_prompt_hint() -> None:
    print(_c("  (type a follow-up for the arbiter, or press Enter to exit)", DIM))


# ── Verbose helpers ───────────────────────────────────────────────────────────

def print_verbose_cmd(model: str, cmd: list[str]) -> None:
    print(f"  {_c('$', DIM)} {_c(' '.join(cmd), DIM)}")


def print_timing(model: str, seconds: float, name: str | None = None) -> None:
    print(f"  {_c('⟳', DIM)} {model_label(model, name)} {_c(f'{seconds:.1f}s', DIM)}")


def print_error(msg: str) -> None:
    print(_c(f"  ✗  {msg}", RED))
