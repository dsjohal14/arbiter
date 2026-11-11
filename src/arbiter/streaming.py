"""Parallel, block-streamed display coordinator.

Several models run concurrently, but a terminal has one cursor. The coordinator
buffers every model's output and renders exactly one block at a time, preferring
whichever finished first, so the transcript stays readable instead of interleaved.

Shared by `debate` (rounds) and `verify` (reviewers).
"""

from __future__ import annotations

import asyncio

from . import display
from .models import Session, call_session
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
