"""Debate orchestration: persistent sessions, live block-streamed rounds,
an Opus arbiter, and an interactive follow-up loop."""

import asyncio
from dataclasses import dataclass, field

import display
from models import (
    Session,
    ARBITER_NAMES,
    call_session,
    build_round_prompt,
    build_judge_prompt,
    build_followup_prompt,
)


@dataclass
class DebateResult:
    winner: str
    reason: str
    best_answer: str
    arbiter_name: str
    rounds_history: list[list[tuple[str, str]]] = field(default_factory=list)


# ── Judge parsing ─────────────────────────────────────────────────────────────

def _parse_judge_response(raw: str, valid_keys: list[str]) -> tuple[str, str, str]:
    """Extract WINNER, REASON, BEST ANSWER from arbiter output."""
    markers = ["WINNER:", "REASON:", "BEST ANSWER:"]

    def extract(label: str) -> str:
        start = raw.find(label)
        if start == -1:
            return ""
        content_start = start + len(label)
        end = len(raw)
        for other in markers:
            if other == label:
                continue
            pos = raw.find(other, content_start)
            if pos != -1 and pos < end:
                end = pos
        return raw[content_start:end].strip()

    winner_raw  = extract("WINNER:").lower()
    reason      = extract("REASON:") or "No reason provided."
    best_answer = extract("BEST ANSWER:") or raw.strip()

    # Normalise the winner to a known debater key.
    winner = next((k for k in valid_keys if k in winner_raw), winner_raw.strip())
    return winner, reason, best_answer


# ── Parallel, block-streamed round display coordinator ────────────────────────

async def _stream_round(
    sessions: list[Session],
    prompts: dict[str, str],
    round_num: int,
    verbose: bool,
) -> list[tuple[str, str]]:
    """Run all debaters concurrently; render one live block at a time in
    completion order. Returns [(key, answer), ...] for models that succeeded."""
    queue: asyncio.Queue = asyncio.Queue()

    async def on_delta(key: str, text: str) -> None:
        await queue.put(("delta", key, text))

    def on_cmd(key: str, cmd: list[str]) -> None:
        if verbose:
            queue.put_nowait(("cmd", key, cmd))

    async def run_one(sess: Session):
        try:
            resp = await call_session(sess, prompts[sess.key], on_delta=on_delta, on_cmd=on_cmd)
            await queue.put(("done", sess.key, resp.duration))
            return resp
        except Exception as e:  # one model failing shouldn't abort the round
            await queue.put(("fail", sess.key, str(e)))
            return None

    expected = len(sessions)
    coordinator = asyncio.create_task(_coordinator(queue, expected, round_num, verbose))
    results = await asyncio.gather(*[run_one(s) for s in sessions])
    await coordinator
    return [(r.model, r.content) for r in results if r is not None]


async def _coordinator(queue: asyncio.Queue, expected: int, round_num: int, verbose: bool) -> None:
    buffers: dict[str, list[str]] = {}
    cmds: dict[str, list[str]] = {}
    durations: dict[str, float] = {}
    order: list[str] = []      # arrival order of first delta
    done: set[str] = set()
    displayed: set[str] = set()
    finished = 0
    focus: str | None = None

    def _begin(key: str) -> None:
        nonlocal focus
        focus = key
        displayed.add(key)
        display.begin_block(key, round_num)
        if verbose and key in cmds:
            display.print_verbose_cmd(key, cmds[key])
        if buffers.get(key):
            display.stream_chunk("".join(buffers[key]))
            buffers[key] = []

    def _end_focus() -> None:
        nonlocal focus
        if buffers.get(focus):
            display.stream_chunk("".join(buffers[focus]))
            buffers[focus] = []
        display.end_block()
        if verbose and focus in durations:
            display.print_timing(focus, durations[focus])
        focus = None

    def advance() -> None:
        # Prefer a completed-but-unshown model (completion order), else the
        # earliest started one. Chain through any already-finished blocks.
        nonlocal focus
        while focus is None:
            remaining = [k for k in order if k not in displayed]
            if not remaining:
                return
            nxt = next((k for k in remaining if k in done), remaining[0])
            _begin(nxt)
            if nxt in done:
                _end_focus()

    while finished < expected or focus is not None or any(k not in displayed for k in order):
        kind, key, payload = await queue.get()

        if kind == "cmd":
            cmds[key] = payload
        elif kind == "delta":
            if key not in buffers:
                buffers[key] = []
                order.append(key)
            buffers[key].append(payload)
            if focus is None:
                advance()
            elif key == focus:
                display.stream_chunk(payload)
                buffers[key] = []
        elif kind == "done":
            done.add(key)
            durations[key] = payload
            finished += 1
            if key == focus:
                _end_focus()
            advance()
        elif kind == "fail":
            finished += 1
            displayed.add(key)
            display.print_error(f"{key} failed: {payload}")
            if key == focus:
                focus = None
            advance()


# ── Single-stream helper (arbiter follow-ups) ─────────────────────────────────

async def _stream_single(sess: Session, prompt: str, name: str, verbose: bool) -> str:
    def on_cmd(key, cmd):
        if verbose:
            display.print_verbose_cmd(key, cmd)
    display.begin_block(sess.key, name=name)

    async def on_delta(key, text):
        display.stream_chunk(text)

    resp = await call_session(sess, prompt, on_delta=on_delta, on_cmd=on_cmd)
    display.end_block()
    if verbose:
        display.print_timing(sess.key, resp.duration, name=name)
    return resp.content


# ── Orchestration ─────────────────────────────────────────────────────────────

async def _run_rounds(
    prompt: str,
    debaters: dict[str, Session],
    num_rounds: int,
    verbose: bool,
    transcript: list[str],
) -> list[list[tuple[str, str]]]:
    rounds_history: list[list[tuple[str, str]]] = []

    for round_num in range(1, num_rounds + 1):
        display.print_round_header(round_num, num_rounds)
        prev = rounds_history[-1] if rounds_history else []
        prompts = {
            key: build_round_prompt(round_num, prompt, [(m, a) for m, a in prev if m != key])
            for key in debaters
        }
        answers = await _stream_round(list(debaters.values()), prompts, round_num, verbose)
        if not answers:
            raise RuntimeError(f"All models failed in round {round_num}.")

        transcript.append(f"\n## Round {round_num}\n")
        for key, content in answers:
            transcript.append(f"\n### {display.DISPLAY_NAMES.get(key, key)}\n\n{content}\n")
        rounds_history.append(answers)

    return rounds_history


async def _judge(
    prompt: str,
    final_answers: list[tuple[str, str]],
    arbiter: Session,
    arbiter_name: str,
    verbose: bool,
) -> tuple[str, str, str]:
    display.print_judging(arbiter_name)
    judge_prompt = build_judge_prompt(prompt, final_answers)

    def on_cmd(key, cmd):
        if verbose:
            display.print_verbose_cmd(key, cmd)

    resp = await call_session(arbiter, judge_prompt, on_cmd=on_cmd)
    if verbose:
        display.print_timing("arbiter", resp.duration, name=arbiter_name)
    return _parse_judge_response(resp.content, [k for k, _ in final_answers])


async def run_debate(
    prompt: str,
    debaters: dict[str, Session],
    arbiter: Session,
    judge: str,
    num_rounds: int,
    verbose: bool,
    transcript: list[str],
) -> DebateResult:
    arbiter_name = ARBITER_NAMES[judge]

    transcript.append(f"# Arbiter Debate Transcript\n")
    transcript.append(f"\n**Prompt:** {prompt}\n")
    transcript.append(f"**Debaters:** {', '.join(display.DISPLAY_NAMES.get(k, k) for k in debaters)}\n")
    transcript.append(f"**Arbiter:** {arbiter_name}\n")
    transcript.append(f"**Rounds:** {num_rounds}\n")

    rounds_history = await _run_rounds(prompt, debaters, num_rounds, verbose, transcript)
    final_answers = rounds_history[-1]
    winner, reason, best_answer = await _judge(prompt, final_answers, arbiter, arbiter_name, verbose)

    transcript.append(
        f"\n## Verdict\n\n**Winner:** {display.DISPLAY_NAMES.get(winner, winner)}\n\n"
        f"**Reason:** {reason}\n\n**Best answer:**\n\n{best_answer}\n"
    )

    return DebateResult(
        winner=winner,
        reason=reason,
        best_answer=best_answer,
        arbiter_name=arbiter_name,
        rounds_history=rounds_history,
    )


async def run_followup_loop(
    debaters: dict[str, Session],
    arbiter: Session,
    verbose: bool,
    result: DebateResult,
    transcript: list[str],
) -> None:
    """After the verdict, let the user keep talking to the persistent arbiter.
    The arbiter refines directly, or emits REPOLL: to re-run a debate round."""
    arbiter_name = result.arbiter_name

    while True:
        print()
        display.print_prompt_hint()
        try:
            user_in = input("\033[1m\033[96m  You:\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not user_in:
            return

        transcript.append(f"\n## Follow-up\n\n**You:** {user_in}\n")

        # Ask the arbiter (buffered, so we can detect a REPOLL directive before
        # showing anything — streaming would leak the "REPOLL:" scaffolding).
        resp = await call_session(arbiter, build_followup_prompt(user_in),
                                  on_cmd=(lambda k, c: display.print_verbose_cmd(k, c)) if verbose else None)
        text = resp.content.strip()
        first_line = text.splitlines()[0] if text else ""

        if first_line.upper().startswith("REPOLL:"):
            question = first_line.split(":", 1)[1].strip() or user_in
            display.print_repoll(question)
            # Round-1 framing so debaters answer the new question directly
            # (their sessions still carry the full prior debate as context).
            prompts = {key: build_round_prompt(1, question, []) for key in debaters}
            answers = await _stream_round(list(debaters.values()), prompts, 1, verbose)
            transcript.append(f"\n### Re-poll: {question}\n")
            for key, content in answers:
                transcript.append(f"\n#### {display.DISPLAY_NAMES.get(key, key)}\n\n{content}\n")
            if answers:
                winner, reason, best = await _judge(question, answers, arbiter, arbiter_name, verbose)
                display.print_winner(winner, reason, best, arbiter_name)
                transcript.append(
                    f"\n**Winner:** {display.DISPLAY_NAMES.get(winner, winner)}\n\n"
                    f"**Reason:** {reason}\n\n**Best answer:**\n\n{best}\n"
                )
        else:
            display.print_arbiter_reply(text, arbiter_name)
            transcript.append(f"\n**Arbiter:** {text}\n")
