"""Parallel, block-streamed display coordinator.

Several models run concurrently, but a terminal has one cursor. The coordinator
buffers every model's output and renders exactly one block at a time, preferring
whichever finished first, so the transcript stays readable instead of interleaved.

Shared by `debate` (rounds) and `verify` (reviewers).
"""

from __future__ import annotations

import asyncio

from . import display
from .models import ModelCallError, Session, call_session
from .progress import Progress
from .run_records import RunTrace


async def stream_parallel(
    sessions: list[Session],
    prompts: dict[str, str],
    verbose: bool,
    names: dict[str, str],
    timeout: float,
    round_num: int | None = None,
    schema_path_writer=None,
    trace: RunTrace | None = None,
    phase: str = "reviewer",
) -> list[tuple[str, str]]:
    """Run all sessions concurrently, rendering one live block at a time in
    completion order. Returns [(key, content), ...] for those that succeeded.

    One model failing degrades to a reported error; it never aborts the group.
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def on_delta(key: str, text: str) -> None:
        await queue.put(("delta", key, text))

    def on_cmd(key: str, cmd: list[str]) -> None:
        if verbose:
            queue.put_nowait(("cmd", key, cmd))

    def on_retry(key: str, attempt: int, error: Exception) -> None:
        queue.put_nowait(("retry", key, (attempt, error)))

    async def run_one(sess: Session):
        try:
            resp = await call_session(
                sess,
                prompts[sess.key],
                on_delta=on_delta,
                on_cmd=on_cmd,
                timeout=timeout,
                schema_path_writer=schema_path_writer,
                on_retry=on_retry,
            )
            if trace is not None:
                trace.success(phase, sess, resp)
            await queue.put(("done", sess.key, resp))
            return resp
        except Exception as e:
            if trace is not None:
                trace.failure(phase, sess, e)
            await queue.put(("fail", sess.key, str(e)))
            return None

    coordinator = asyncio.create_task(_coordinator(queue, len(sessions), round_num, verbose, names))
    results = await asyncio.gather(*[run_one(s) for s in sessions])
    await coordinator
    return [(r.model, r.content) for r in results if r is not None]


async def _coordinator(
    queue: asyncio.Queue,
    expected: int,
    round_num: int | None,
    verbose: bool,
    names: dict[str, str],
) -> None:
    buffers: dict[str, list[str]] = {}
    cmds: dict[str, list[str]] = {}
    durations: dict[str, float] = {}
    order: list[str] = []  # arrival order of first output
    done: set[str] = set()
    displayed: set[str] = set()
    finished = 0
    focus: str | None = None

    def _register(key: str) -> None:
        if key not in buffers:
            buffers[key] = []
            order.append(key)

    def _begin(key: str) -> None:
        nonlocal focus
        focus = key
        displayed.add(key)
        display.begin_block(key, names.get(key, key), round_num)
        if verbose and key in cmds:
            display.print_verbose_cmd(cmds[key])
        if buffers.get(key):
            display.stream_chunk("".join(buffers[key]))
            buffers[key] = []

    def _end_focus() -> None:
        nonlocal focus
        if focus and buffers.get(focus):
            display.stream_chunk("".join(buffers[focus]))
            buffers[focus] = []
        display.end_block()
        if verbose and focus in durations:
            display.print_timing(names.get(focus, focus), durations[focus])
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
            _register(key)
            buffers[key].append(payload)
            if focus is None:
                advance()
            elif key == focus:
                display.stream_chunk(payload)
                buffers[key] = []
        elif kind == "done":
            # A model can complete without ever emitting a delta — codex sends
            # its whole message at once, and claude's `result` event carries the
            # full text. Register it here so its answer is actually shown; such
            # a model used to be judged and transcribed but never printed.
            if key not in buffers:
                _register(key)
                buffers[key] = [payload.content]
            done.add(key)
            durations[key] = payload.duration
            finished += 1
            if key == focus:
                _end_focus()
            advance()
        elif kind == "retry":
            # Whatever that attempt streamed is not the answer. Close its block
            # and forget it, so the next attempt opens one of its own.
            if key == focus:
                _end_focus()
            buffers.pop(key, None)
            displayed.discard(key)
            if key in order:
                order.remove(key)
            attempt, error = payload
            display.print_retry(names.get(key, key), attempt, error)
            advance()
        elif kind == "fail":
            finished += 1
            displayed.add(key)
            display.print_error(f"{names.get(key, key)} failed: {payload}")
            if key == focus:
                focus = None
            advance()


# ── Structured reviews: report progress, not output ──────────────────────────
#
# A debate streams prose a person reads. A verification's reviewers answer in
# JSON meant for a parser, so what is worth showing is which step the run is on
# and whether it is alive — which is what `Progress` renders.


async def review_parallel(
    sessions: list[Session],
    prompts: dict[str, str],
    names: dict[str, str],
    timeout: float,
    progress: Progress,
    schema_path_writer=None,
    trace: RunTrace | None = None,
    phase: str = "reviewer",
    activity: str = "reviewing",
    finished: str = "reviewed",
) -> list[tuple[str, str]]:
    """Run every session concurrently. Returns [(key, content), ...] for those
    that succeeded, in the order the sessions were given.

    One model failing degrades to a reported error; it never aborts the group.
    """
    for sess in sessions:
        progress.add(sess.key, names.get(sess.key, sess.key))

    async def run_one(sess: Session):
        progress.start(sess.key, activity)
        received = 0

        async def on_delta(key: str, text: str) -> None:
            nonlocal received
            received += len(text)
            progress.update(key, f"{activity} · {received:,} characters back")

        try:
            resp = await call_session(
                sess,
                prompts[sess.key],
                on_delta=on_delta,
                timeout=timeout,
                schema_path_writer=schema_path_writer,
                on_retry=lambda key, attempt, error: progress.retry(key, attempt, str(error)),
            )
        except Exception as e:
            if trace is not None:
                trace.failure(phase, sess, e)
            progress.fail(sess.key, str(e))
            return None
        if trace is not None:
            trace.success(phase, sess, resp)
        progress.done(sess.key, finished, resp.duration, resp.usage)
        return resp

    results = await asyncio.gather(*[run_one(s) for s in sessions])
    return [(r.model, r.content) for r in results if r is not None]


CONSOLIDATION = "consolidation"


async def consolidate(
    consolidator: Session,
    name: str,
    build_prompt,
    skip: str | None,
    progress: Progress,
    timeout: float,
    fallback: str,
    schema_path_writer=None,
    trace: RunTrace | None = None,
    verbose: bool = False,
):
    """The consolidator's reply, or None when the call was skipped or failed.

    Consolidation reconciles; it is not the source of truth. Losing it costs
    precision and must not cost the whole run, so a failure here is reported
    and the caller falls back to `fallback` — what the reviewers themselves said.

    `build_prompt` is called only if the call is made: with nothing to
    reconcile there is no reason to assemble a prompt nobody will read.
    """
    progress.add(CONSOLIDATION, CONSOLIDATION)
    if skip:
        progress.skip(CONSOLIDATION, skip)
        return None

    progress.start(CONSOLIDATION, f"{name} reconciling the reviews (blinded)")
    try:
        resp = await call_session(
            consolidator,
            build_prompt(),
            on_cmd=(lambda _key, cmd: display.print_verbose_cmd(cmd)) if verbose else None,
            timeout=timeout,
            schema_path_writer=schema_path_writer,
            on_retry=lambda _key, attempt, error: progress.retry(
                CONSOLIDATION, attempt, str(error)
            ),
        )
    except ModelCallError as e:
        if trace is not None:
            trace.failure("consolidator", consolidator, e)
        progress.fail(CONSOLIDATION, f"{e} Falling back to {fallback}.")
        return None
    if trace is not None:
        trace.success("consolidator", consolidator, resp)
    progress.done(CONSOLIDATION, f"reconciled by {name}", resp.duration, resp.usage)
    return resp
