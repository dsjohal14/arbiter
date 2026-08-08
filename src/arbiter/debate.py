"""Debate orchestration: persistent sessions, live block-streamed rounds, a
blinded arbiter, and an interactive follow-up loop."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field

from . import display
from .models import ModelCallError, Session, call_session
from .prompts import (
    Blinding,
    blind,
    build_followup_prompt,
    build_judge_prompt,
    build_repair_prompt,
    build_round_prompt,
)
from .schema_files import schema_writer
from .streaming import stream_parallel


@dataclass
class Verdict:
    winner: str | None  # model key, or None when the arbiter gave no clear answer
    reason: str
    best_answer: str
    blinding: Blinding | None = None


@dataclass
class DebateResult:
    verdict: Verdict
    arbiter_name: str
    rounds_history: list[list[tuple[str, str]]] = field(default_factory=list)


# ── Judge parsing ─────────────────────────────────────────────────────────────

# Markers are matched only at the START of a line, case-insensitively, with
# optional markdown bold. Line anchoring is what stops the parser matching the
# format instructions when the arbiter echoes them back — which used to make it
# report the wrong winner.
_MARKER_RE = re.compile(
    r"^[ \t]*\**[ \t]*(WINNER|REASON|BEST[ \t]+ANSWER)[ \t]*\**[ \t]*:[ \t]*\**[ \t]*",
    re.IGNORECASE | re.MULTILINE,
)

# "B", "Answer B", "**B**", "B." — but the token must then match a known label exactly.
_WINNER_TOKEN_RE = re.compile(r"^\**\s*(?:ANSWER\s+)?([A-Za-z0-9_-]+)", re.IGNORECASE)


def _split_sections(raw: str) -> dict[str, list[tuple[int, str]]]:
    """Slice `raw` into labelled sections at line-anchored markers."""
    matches = list(_MARKER_RE.finditer(raw))
    out: dict[str, list[tuple[int, str]]] = {}
    for i, m in enumerate(matches):
        name = re.sub(r"\s+", " ", m.group(1).upper())
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        out.setdefault(name, []).append((m.end(), raw[m.end() : end].strip()))
    return out


def _parse_structured(raw: str) -> tuple[str, str, str] | None:
    """Parse a provider's structured-output response, if that's what this is."""
    text = raw.strip()
    if not text.startswith("{"):
        # Tolerate a fenced block around the JSON.
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if not fence:
            return None
        text = fence.group(1)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "winner" not in obj:
        return None
    return (
        str(obj.get("winner", "")),
        str(obj.get("reason", "")).strip() or "No reason provided.",
        str(obj.get("best_answer", "")).strip(),
    )


def parse_judge_response(raw: str, valid_labels: list[str]) -> tuple[str | None, str, str]:
    """Extract (winner, reason, best_answer) from an arbiter response.

    `winner` is None when the arbiter did not name exactly one known label. The
    caller retries once and then reports honestly rather than guessing — an
    earlier version substring-scanned the label list, which meant an arbiter that
    echoed the format template silently produced the first label in list order.
    """
    valid_upper = {v.upper(): v for v in valid_labels}

    structured = _parse_structured(raw)
    if structured is not None:
        winner_raw, reason, best = structured
        token = _WINNER_TOKEN_RE.match(winner_raw.strip())
        winner = valid_upper.get(token.group(1).upper()) if token else None
        return winner, reason, best or raw.strip()

    sections = _split_sections(raw)

    # Take the first WINNER line that names a *known* label. An arbiter that
    # restates the format template first ("WINNER: <one of: A | B | C>") then
    # answers must not have the template read as its verdict.
    winner: str | None = None
    for _, text in sections.get("WINNER", []):
        token = _WINNER_TOKEN_RE.match(text)
        if token and token.group(1).upper() in valid_upper:
            winner = valid_upper[token.group(1).upper()]
            break

    reason = sections.get("REASON", [(0, "")])[0][1] or "No reason provided."

    # BEST ANSWER is the final section by contract, so take the LAST occurrence
    # and run it to the end of the response. Slicing to the next marker instead
    # used to truncate the answer whenever the arbiter wrote "BEST ANSWER:" in
    # its own prose.
    if "BEST ANSWER" in sections:
        best_start = sections["BEST ANSWER"][-1][0]
        best_answer = raw[best_start:].strip()
    else:
        best_answer = raw.strip()

    return winner, reason, best_answer


# ── Single-stream helper (arbiter follow-ups) ─────────────────────────────────


async def _stream_single(
    sess: Session, prompt: str, name: str, verbose: bool, timeout: float
) -> str:
    def on_cmd(key, cmd):
        if verbose:
            display.print_verbose_cmd(cmd)

    display.begin_block(sess.key, name)

    async def on_delta(key, text):
        display.stream_chunk(text)

    resp = await call_session(sess, prompt, on_delta=on_delta, on_cmd=on_cmd, timeout=timeout)
    display.end_block()
    if verbose:
        display.print_timing(name, resp.duration)
    return resp.content


# ── Orchestration ─────────────────────────────────────────────────────────────


async def _run_rounds(
    prompt: str,
    debaters: dict[str, Session],
    num_rounds: int,
    verbose: bool,
    transcript: list[str],
    names: dict[str, str],
    timeout: float,
) -> list[list[tuple[str, str]]]:
    rounds_history: list[list[tuple[str, str]]] = []

    for round_num in range(1, num_rounds + 1):
        display.print_round_header(round_num, num_rounds)
        prev = rounds_history[-1] if rounds_history else []
        prompts = {}
        for key in debaters:
            others = [(m, a) for m, a in prev if m != key]
            # Blind the peers too, so a debater can't defer to a vendor it knows.
            blinded_others, _ = blind(others) if others else ([], None)
            prompts[key] = build_round_prompt(round_num, prompt, blinded_others)

        answers = await stream_parallel(
            list(debaters.values()), prompts, verbose, names, timeout, round_num=round_num
        )
        if not answers:
            raise ModelCallError(f"All models failed in round {round_num}.")

        transcript.append(f"\n## Round {round_num}\n")
        for key, content in answers:
            transcript.append(f"\n### {names.get(key, key)}\n\n{content}\n")
        rounds_history.append(answers)

    return rounds_history


async def judge(
    prompt: str,
    final_answers: list[tuple[str, str]],
    arbiter: Session,
    arbiter_name: str,
    verbose: bool,
    timeout: float,
    seed: int | None = None,
) -> Verdict:
    """Judge blinded answers, retrying once if the arbiter names no valid label."""
    display.print_judging(arbiter_name)
    blinded, blinding = blind(final_answers, seed=seed)
    labels = [lbl for lbl, _ in blinded]

    def on_cmd(key, cmd):
        if verbose:
            display.print_verbose_cmd(cmd)

    # The writer must wrap BOTH calls: a codex arbiter needs its schema on disk,
    # and omitting it here is what made `--judge codex` crash before reaching a
    # provider at all.
    with schema_writer() as write_schema:
        resp = await call_session(
            arbiter,
            build_judge_prompt(prompt, blinded),
            on_cmd=on_cmd,
            timeout=timeout,
            schema_path_writer=write_schema,
        )
        if verbose:
            display.print_timing(arbiter_name, resp.duration)

        label, reason, best = parse_judge_response(resp.content, labels)

        if label is None:
            # One repair attempt, then report honestly rather than guessing.
            retry = await call_session(
                arbiter,
                build_repair_prompt(labels),
                on_cmd=on_cmd,
                timeout=timeout,
                schema_path_writer=write_schema,
            )
            label, _, _ = parse_judge_response(retry.content, labels)

    return Verdict(
        winner=blinding.resolve(label),
        reason=reason,
        best_answer=best,
        blinding=blinding,
    )


async def run_debate(
    prompt: str,
    debaters: dict[str, Session],
    arbiter: Session,
    arbiter_name: str,
    num_rounds: int,
    verbose: bool,
    transcript: list[str],
    names: dict[str, str],
    timeout: float,
    seed: int | None = None,
) -> DebateResult:
    transcript.append("# Arbiter Debate Transcript\n")
    transcript.append(f"\n**Prompt:** {prompt}\n")
    transcript.append(f"**Debaters:** {', '.join(names.get(k, k) for k in debaters)}\n")
    transcript.append(f"**Arbiter:** {arbiter_name}\n")
    transcript.append(f"**Rounds:** {num_rounds}\n")

    rounds_history = await _run_rounds(
        prompt, debaters, num_rounds, verbose, transcript, names, timeout
    )
    verdict = await judge(prompt, rounds_history[-1], arbiter, arbiter_name, verbose, timeout, seed)

    winner_name = names.get(verdict.winner, verdict.winner) if verdict.winner else "no clear winner"
    transcript.append(
        f"\n## Verdict\n\n**Winner:** {winner_name}\n\n"
        f"**Reason:** {verdict.reason}\n\n**Best answer:**\n\n{verdict.best_answer}\n"
    )
    if verdict.blinding:
        transcript.append(
            f"\n<!-- blinding: {json.dumps(verdict.blinding.as_dict(), sort_keys=True)} -->\n"
        )

    return DebateResult(verdict=verdict, arbiter_name=arbiter_name, rounds_history=rounds_history)


async def run_followup_loop(
    debaters: dict[str, Session],
    arbiter: Session,
    verbose: bool,
    result: DebateResult,
    transcript: list[str],
    names: dict[str, str],
    timeout: float,
) -> None:
    """After the verdict, let the user keep talking to the persistent arbiter.
    The arbiter refines directly, or emits REPOLL: to re-run a debate round."""
    arbiter_name = result.arbiter_name

    while True:
        print()
        display.print_prompt_hint()
        try:
            # to_thread, so the event loop keeps running while we block on the user.
            user_in = (await asyncio.to_thread(input, display.prompt_label("You"))).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not user_in:
            return

        transcript.append(f"\n## Follow-up\n\n**You:** {user_in}\n")

        # Buffered, so a REPOLL directive can be detected before anything is
        # shown — streaming would leak the "REPOLL:" scaffolding to the user.
        # output_schema=None: the arbiter session carries JUDGE_SCHEMA, which
        # would force {winner, reason, best_answer} JSON here and make the
        # REPOLL: directive undetectable. Follow-ups are free-form.
        resp = await call_session(
            arbiter,
            build_followup_prompt(user_in),
            on_cmd=(lambda k, c: display.print_verbose_cmd(c)) if verbose else None,
            timeout=timeout,
            output_schema=None,
        )
        text = resp.content.strip()
        first_line = text.splitlines()[0] if text else ""

        if first_line.upper().startswith("REPOLL:"):
            question = first_line.split(":", 1)[1].strip() or user_in
            display.print_repoll(question)
            # Round-1 framing so debaters answer the new question directly;
            # their sessions still carry the full prior debate as context.
            prompts = {key: build_round_prompt(1, question, []) for key in debaters}
            answers = await stream_parallel(
                list(debaters.values()), prompts, verbose, names, timeout, round_num=1
            )
            transcript.append(f"\n### Re-poll: {question}\n")
            for key, content in answers:
                transcript.append(f"\n#### {names.get(key, key)}\n\n{content}\n")
            if answers:
                verdict = await judge(question, answers, arbiter, arbiter_name, verbose, timeout)
                display.print_winner(verdict, arbiter_name, names)
                wname = (
                    names.get(verdict.winner, verdict.winner)
                    if verdict.winner
                    else "no clear winner"
                )
                transcript.append(
                    f"\n**Winner:** {wname}\n\n**Reason:** {verdict.reason}\n\n"
                    f"**Best answer:**\n\n{verdict.best_answer}\n"
                )
        else:
            display.print_arbiter_reply(text, arbiter_name)
            transcript.append(f"\n**Arbiter:** {text}\n")
