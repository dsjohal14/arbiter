"""Terminal display helpers — colors, banners, answer cards."""

from dataclasses import dataclass

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
}

def model_label(model: str) -> str:
    names = {"claude": "Claude", "codex": "Codex", "gemini": "Gemini"}
    color = MODEL_COLORS.get(model, WHITE)
    return _c(names.get(model, model), BOLD, color)


def print_banner(debaters: list[str], judge: str, rounds: int) -> None:
    print()
    print(_c("╔══════════════════════════════════════════╗", BLUE))
    print(_c("║       🤖  LLM DEBATE ARENA  🤖           ║", BLUE))
    print(_c("╚══════════════════════════════════════════╝", BLUE))
    print()
    print(f"  {_c('Debaters:', BOLD)}  {('  vs  ').join(model_label(m) for m in debaters)}")
    print(f"  {_c('Judge:   ', BOLD)}  {model_label(judge)}")
    print(f"  {_c('Rounds:  ', BOLD)}  {_c(rounds, BOLD, YELLOW)}")
    print()


def print_round_header(round_num: int, total: int) -> None:
    print()
    print(_c(f"━━━  ROUND {round_num}/{total}  " + "━" * 32, BOLD, YELLOW))


def print_thinking(model: str) -> None:
    print(f"  {model_label(model)} {_c('thinking...', DIM)}")


def print_model_answer(model: str, content: str, round_num: int) -> None:
    print()
    color = MODEL_COLORS.get(model, WHITE)
    header = _c(f"▶ {model_label(model)} (round {round_num})", BOLD)
    print(f"  {header}")
    bar = _c("  │ ", color)
    for line in content.strip().splitlines():
        print(f"{bar}{line}")
    print()


def print_judging(judge: str) -> None:
    print()
    print(_c("━━━  JUDGMENT  " + "━" * 36, BOLD, MAGENTA))
    print(f"  {model_label(judge)} {_c('is deliberating...', DIM)}")
    print()


def print_winner(winner: str, reason: str, best_answer: str, judge: str) -> None:
    name = winner.upper()
    print(_c("╔══════════════════════════════════════════╗", GREEN))
    print(_c(f"║  🏆  WINNER: {name:<29}║", BOLD, GREEN))
    print(_c("╚══════════════════════════════════════════╝", GREEN))
    print()
    print(f"  {_c('Judged by:', BOLD)} {model_label(judge)}")
    print()
    print(f"  {_c('Why this answer won:', BOLD, UNDER)}")
    for line in reason.strip().splitlines():
        print(f"  {line}")
    print()
    print(_c("━━━  BEST ANSWER  " + "━" * 33, BOLD, GREEN))
    print()
    color = MODEL_COLORS.get(winner.lower(), WHITE)
    bar = _c("  │ ", color)
    for line in best_answer.strip().splitlines():
        print(f"{bar}{line}")
    print()


def print_error(msg: str) -> None:
    print(_c(f"  ✗  {msg}", RED))
