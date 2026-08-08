"""Terminal display — colors, banners, streaming answer blocks. No logic.

Colour is suppressed when stdout is not a TTY or NO_COLOR is set, so piped
output stays clean for `--json` consumers and CI logs.
"""

from __future__ import annotations

import builtins
import os
import sys

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
UNDER = "\033[4m"

RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
CYAN = "\033[96m"
WHITE = "\033[97m"


_json_mode = False


def set_json_mode(enabled: bool) -> None:
    """In JSON mode all human-facing progress goes to stderr, so stdout carries
    nothing but the machine-readable result and `arbiter … --json | jq` works."""
    global _json_mode
    _json_mode = enabled


def out():
    """Resolved per call, not captured at import: `sys.stdout` is swapped by
    pytest's capture and by anything that redirects output."""
    return sys.stderr if _json_mode else sys.stdout


def print(*args, **kwargs) -> None:
    """Like the builtin, but honours the JSON-mode redirect."""
    kwargs.setdefault("file", out())
    builtins.print(*args, **kwargs)


def color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("ARBITER_FORCE_COLOR"):
        return True
    stream = out()
    return hasattr(stream, "isatty") and stream.isatty()


def _c(text: object, *codes: str) -> str:
    if not color_enabled():
        return str(text)
    return "".join(codes) + str(text) + RESET


# Colours are keyed by role, not by vendor, so adding a provider needs no change
# here and no marketing name is baked into the source.
ROLE_COLORS = {
    "claude": MAGENTA,
    "codex": CYAN,
    "gemini": YELLOW,
    "arbiter": GREEN,
}

_SEVERITY_COLORS = {
    "blocking": RED,
    "major": YELLOW,
    "minor": CYAN,
    "question": DIM,
}


def _role_color(role: str) -> str:
    return ROLE_COLORS.get(role, WHITE)


def prompt_label(text: str) -> str:
    return _c(f"  {text}: ", BOLD, CYAN)


def print_banner(
    names: dict[str, str], debaters: list[str], arbiter_name: str, rounds: int
) -> None:
    print()
    print(_c("╔══════════════════════════════════════════╗", BLUE))
    print(_c("║       🤖  LLM DEBATE ARENA  🤖           ║", BLUE))
    print(_c("╚══════════════════════════════════════════╝", BLUE))
    print()
    labels = "  vs  ".join(_c(names.get(m, m), BOLD, _role_color(m)) for m in debaters)
    print(f"  {_c('Debaters:', BOLD)}  {labels}")
    print(f"  {_c('Arbiter: ', BOLD)}  {_c(arbiter_name, BOLD, GREEN)}")
    print(f"  {_c('Rounds:  ', BOLD)}  {_c(rounds, BOLD, YELLOW)}")
    print()


def print_round_header(round_num: int, total: int) -> None:
    print()
    print(_c(f"━━━  ROUND {round_num}/{total}  " + "━" * 32, BOLD, YELLOW))


def print_judging(arbiter_name: str) -> None:
    print()
    print(_c("━━━  JUDGMENT  " + "━" * 36, BOLD, MAGENTA))
    print(f"  {_c(arbiter_name, BOLD, GREEN)} {_c('is deliberating (answers blinded)...', DIM)}")
    print()


# ── Live streaming blocks ─────────────────────────────────────────────────────
# A block is one model's answer, rendered live behind a colored "│" bar. Chunks
# arrive without respect to line boundaries, so we track whether we're at the
# start of a line and prefix the bar exactly once per line.

_block = {"at_line_start": True, "bar": ""}


def begin_block(role: str, name: str, round_num: int | None = None) -> None:
    print()
    suffix = f" (round {round_num})" if round_num is not None else ""
    print(f"  {_c('▶ ' + name + suffix, BOLD, _role_color(role))}")
    _block["bar"] = _c("  │ ", _role_color(role))
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
    stream = out()
    stream.write("".join(parts))
    stream.flush()


def end_block() -> None:
    stream = out()
    if not _block["at_line_start"]:
        stream.write("\n")
    stream.write("\n")
    stream.flush()
    _block["at_line_start"] = True


def _quoted(text: str, color: str) -> None:
    bar = _c("  │ ", color)
    for line in text.strip().splitlines():
        print(f"{bar}{line}")


def print_winner(verdict, arbiter_name: str, names: dict[str, str]) -> None:
    if verdict.winner is None:
        print(_c("╔══════════════════════════════════════════╗", YELLOW))
        print(_c(f"║  ⚖   {'NO CLEAR WINNER':<36}║", BOLD, YELLOW))
        print(_c("╚══════════════════════════════════════════╝", YELLOW))
        color = WHITE
    else:
        name = names.get(verdict.winner, verdict.winner).upper()
        print(_c("╔══════════════════════════════════════════╗", GREEN))
        print(_c(f"║  🏆  WINNER: {name:<29}║", BOLD, GREEN))
        print(_c("╚══════════════════════════════════════════╝", GREEN))
        color = _role_color(verdict.winner)

    print()
    print(f"  {_c('Judged by:', BOLD)} {_c(arbiter_name, BOLD, GREEN)} {_c('(blinded)', DIM)}")
    print()
    print(f"  {_c('Why this answer won:', BOLD, UNDER)}")
    for line in verdict.reason.strip().splitlines():
        print(f"  {line}")
    print()
    print(_c("━━━  BEST ANSWER  " + "━" * 33, BOLD, GREEN))
    print()
    _quoted(verdict.best_answer, color)
    print()


def print_arbiter_reply(text: str, arbiter_name: str) -> None:
    print()
    print(_c(f"━━━  {arbiter_name.upper()}  " + "━" * 34, BOLD, GREEN))
    print()
    _quoted(text, GREEN)
    print()


def print_repoll(question: str) -> None:
    print()
    print(_c(f"  ↻  Re-polling debaters: {question}", BOLD, YELLOW))


def print_prompt_hint() -> None:
    print(_c("  (type a follow-up for the arbiter, or press Enter to exit)", DIM))


# ── Verify output ─────────────────────────────────────────────────────────────


def print_verdict_banner(status: str, counts: dict[str, int]) -> None:
    palette = {"pass": GREEN, "fail": RED, "unclear": YELLOW}
    glyph = {"pass": "✓", "fail": "✗", "unclear": "?"}
    color = palette.get(status, WHITE)
    print()
    print(_c("╔══════════════════════════════════════════╗", color))
    print(_c(f"║  {glyph.get(status, '·')}  VERDICT: {status.upper():<28}║", BOLD, color))
    print(_c("╚══════════════════════════════════════════╝", color))
    print()
    summary = "  ".join(
        f"{_c(sev, _SEVERITY_COLORS.get(sev, WHITE))} {counts.get(sev, 0)}"
        for sev in ("blocking", "major", "minor", "question")
    )
    print(f"  {summary}")
    print()


def print_finding(n: int, f: dict) -> None:
    sev = str(f.get("severity", "minor")).lower()
    color = _SEVERITY_COLORS.get(sev, WHITE)
    print(f"  {_c(f'{n}.', BOLD)} {_c(sev.upper(), BOLD, color)}  {f.get('claim', '')}")
    if f.get("acceptance_criterion"):
        print(f"     {_c('criterion:', DIM)} {f['acceptance_criterion']}")
    if f.get("evidence"):
        print(f"     {_c('evidence: ', DIM)} {f['evidence']}")
    if f.get("validation_command"):
        print(f"     {_c('validate: ', DIM)} {_c(f['validation_command'], CYAN)}")
    print()


def print_section(title: str) -> None:
    print()
    print(_c(f"━━━  {title.upper()}  " + "━" * max(4, 40 - len(title)), BOLD, BLUE))
    print()


# ── Verbose helpers ───────────────────────────────────────────────────────────


def print_verbose_cmd(cmd: list[str]) -> None:
    print(f"  {_c('$', DIM)} {_c(' '.join(cmd), DIM)}")


def print_timing(name: str, seconds: float) -> None:
    print(f"  {_c('⟳', DIM)} {_c(name, BOLD)} {_c(f'{seconds:.1f}s', DIM)}")


def print_error(msg: str) -> None:
    print(_c(f"  ✗  {msg}", RED), file=sys.stderr)


def print_note(msg: str) -> None:
    print(_c(f"  {msg}", DIM))
