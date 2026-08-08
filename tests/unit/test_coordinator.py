"""The streaming coordinator as a pure state machine.

Every case is driven by a scripted event queue and asserted against a render
log, so a hang shows up as a fast timeout failure rather than a stuck suite.
"""

from __future__ import annotations

import asyncio

import pytest

from arbiter import display, streaming
from arbiter.models import ModelResponse

NAMES = {"claude": "claude", "codex": "codex", "gemini": "gemini"}


@pytest.fixture
def render_log(monkeypatch):
    log: list[str] = []
    monkeypatch.setattr(
        display, "begin_block", lambda role, name, rn=None: log.append(f"begin:{role}")
    )
    monkeypatch.setattr(display, "stream_chunk", lambda text: log.append(f"chunk:{text}"))
    monkeypatch.setattr(display, "end_block", lambda: log.append("end"))
    monkeypatch.setattr(display, "print_error", lambda msg: log.append(f"error:{msg}"))
    monkeypatch.setattr(display, "print_timing", lambda *a, **kw: None)
    monkeypatch.setattr(display, "print_verbose_cmd", lambda *a, **kw: None)
    return log


def _resp(key: str, content: str = "") -> ModelResponse:
    return ModelResponse(model=key, content=content, duration=1.0)


async def _run(events, expected):
    queue: asyncio.Queue = asyncio.Queue()
    for e in events:
        queue.put_nowait(e)
    await asyncio.wait_for(streaming._coordinator(queue, expected, None, False, NAMES), timeout=2)


async def test_single_model_streams_then_closes(render_log):
    await _run([("delta", "claude", "Hello"), ("done", "claude", _resp("claude", "Hello"))], 1)
    assert render_log == ["begin:claude", "chunk:Hello", "end"]


async def test_a_model_that_never_emits_a_delta_is_still_rendered(render_log):
    """The never-printed-model bug.

    `order` was only appended in the delta branch, so a model that completed with
    a single `done` event was never displayed — yet its answer still reached the
    transcript and the judge. Users could see a winner they never saw an answer
    from. Codex hits this every time: it emits its whole message at once.
    """
    await _run(
        [
            ("delta", "claude", "Hi"),
            ("done", "claude", _resp("claude", "Hi")),
            ("done", "codex", _resp("codex", "codex answer")),
        ],
        2,
    )
    assert "begin:codex" in render_log
    assert "chunk:codex answer" in render_log


async def test_blocks_never_interleave(render_log):
    """Two models streaming at once must not scramble each other's output."""
    await _run(
        [
            ("delta", "claude", "a1"),
            ("delta", "codex", "b1"),
            ("delta", "claude", "a2"),
            ("delta", "codex", "b2"),
            ("done", "claude", _resp("claude")),
            ("done", "codex", _resp("codex")),
        ],
        2,
    )
    begins = [i for i, e in enumerate(render_log) if e.startswith("begin:")]
    ends = [i for i, e in enumerate(render_log) if e == "end"]
    assert len(begins) == 2 and len(ends) == 2
    # The first block is fully closed before the second opens.
    assert begins[0] < ends[0] < begins[1] < ends[1]
    first = render_log[begins[0] : ends[0]]
    assert "chunk:b1" not in first and "chunk:b2" not in first


async def test_a_finished_model_is_shown_before_one_still_streaming(render_log):
    """When a block closes and several models are waiting, the coordinator shows
    a finished one first — a completed answer should not queue behind a slow
    stream. (The first block is whoever spoke first; there is nothing to prefer
    yet at that point.)"""
    await _run(
        [
            ("delta", "claude", "first"),
            ("delta", "codex", "slow"),  # waiting, still streaming
            ("delta", "gemini", "quick"),  # waiting, will finish first
            ("done", "gemini", _resp("gemini")),
            ("done", "claude", _resp("claude")),  # closes the open block
            ("done", "codex", _resp("codex")),
        ],
        3,
    )
    begins = [e for e in render_log if e.startswith("begin:")]
    assert begins == ["begin:claude", "begin:gemini", "begin:codex"]


async def test_a_failing_model_is_reported_and_does_not_stall_the_group(render_log):
    await _run(
        [
            ("fail", "gemini", "auth expired"),
            ("delta", "claude", "ok"),
            ("done", "claude", _resp("claude")),
        ],
        2,
    )
    assert any(e.startswith("error:") and "auth expired" in e for e in render_log)
    assert "begin:claude" in render_log


async def test_all_models_failing_terminates_cleanly(render_log):
    await _run([("fail", "claude", "x"), ("fail", "codex", "y")], 2)
    assert len([e for e in render_log if e.startswith("error:")]) == 2


async def test_failure_while_a_block_is_open_still_closes_it(render_log):
    await _run(
        [
            ("delta", "claude", "partial"),
            ("fail", "claude", "died mid-stream"),
            ("done", "codex", _resp("codex", "fine")),
        ],
        2,
    )
    assert "begin:codex" in render_log
