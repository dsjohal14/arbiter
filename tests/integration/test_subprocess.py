"""Integration tests against a fake CLI executed for real.

These deliberately avoid mocking `create_subprocess_exec`: the 64 KiB limit,
exit-code handling, and SIGTERM escalation all live in machinery a mock would
replace, so a mocked test would pass while production failed.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from arbiter.models import ARGV_PROMPT_LIMIT, ModelCallError, call_session, make_session
from conftest import agy_events, claude_events, codex_events, gemini_events


async def test_reads_a_complete_claude_stream(shim_path):
    shim_path(claude_events("hello world"))
    resp = await call_session(make_session("claude", "claude"), "q")
    assert resp.content == "hello world"


async def test_captures_a_codex_thread_id_and_resumes_it(shim_path):
    argv_log = shim_path(codex_events("first"))
    sess = make_session("codex", "codex")
    await call_session(sess, "q1")
    assert sess.session_id == "thread-1"
    assert sess.started is True

    await call_session(sess, "q2")
    calls = [json.loads(line) for line in argv_log.read_text().splitlines()]
    assert calls[1][1:4] == ["exec", "resume", "thread-1"]


async def test_gemini_stream_is_parsed(shim_path):
    shim_path(gemini_events("gemini says hi"))
    resp = await call_session(make_session("gemini", "gemini"), "q")
    assert resp.content == "gemini says hi"


async def test_agy_stream_is_parsed_and_its_conversation_id_captured(shim_path):
    shim_path(agy_events("antigravity says hi"))
    sess = make_session("antigravity", "antigravity")
    assert sess.session_id is None  # agy mints its own

    resp = await call_session(sess, "q")

    assert resp.content == "antigravity says hi"
    assert sess.session_id == "conv-1"
    assert sess.started is True


async def test_agy_resumes_by_conversation_id(shim_path):
    argv_log = shim_path(agy_events("hi"))
    sess = make_session("antigravity", "antigravity")
    await call_session(sess, "q1")
    await call_session(sess, "q2")

    calls = [json.loads(line) for line in argv_log.read_text().splitlines()]
    assert "--conversation" not in calls[0]
    assert calls[1][calls[1].index("--conversation") + 1] == "conv-1"


async def test_an_oversized_prompt_still_goes_in_agy_argv(shim_path):
    """`--print` takes its value in argv, and a bare `--print` swallows the next
    flag as the prompt — so the run would review the string '--output-format'
    and report on nothing. Size cannot be allowed to change the shape."""
    argv_log = shim_path(agy_events("ok"))
    big = "PROMPT-MARKER " + "x" * (ARGV_PROMPT_LIMIT + 1)

    await call_session(make_session("antigravity", "antigravity"), big)

    cmd = json.loads(argv_log.read_text().splitlines()[0])
    assert cmd[1] == "--print"
    assert cmd[2] == big


async def test_usage_is_read_off_the_stream(shim_path):
    shim_path(agy_events("ok", usage={"input_tokens": 10, "output_tokens": 2}))
    resp = await call_session(make_session("antigravity", "antigravity"), "q")
    assert resp.usage.input_tokens == 10
    assert resp.usage.output_tokens == 2


async def test_a_provider_reporting_nothing_leaves_usage_unset(shim_path):
    shim_path(gemini_events("hi"))
    resp = await call_session(make_session("gemini", "gemini"), "q")
    assert resp.usage is None


async def test_answer_larger_than_the_64kib_line_limit(shim_path):
    """asyncio.StreamReader defaults to a 64 KiB line limit, and claude emits
    its whole final answer as ONE JSON line. Any long answer used to raise
    'Separator is not found, and chunk exceed the limit' mid-stream."""
    big = "x" * 300_000
    shim_path(claude_events(big, stream_deltas=False))
    resp = await call_session(make_session("claude", "claude"), "q")
    assert len(resp.content) == 300_000


async def test_nonzero_exit_raises_with_stderr_attached(shim_path):
    shim_path(claude_events("partial"), exit_code=2, stderr="auth expired")
    with pytest.raises(ModelCallError, match="exited 2"):
        await call_session(make_session("claude", "claude"), "q")


async def test_empty_output_raises_rather_than_returning_nothing(shim_path):
    shim_path([])
    with pytest.raises(ModelCallError, match="no content"):
        await call_session(make_session("claude", "claude"), "q")


async def test_non_json_noise_on_stdout_is_ignored(shim_path):
    shim_path(claude_events("still fine"), garbage=True)
    resp = await call_session(make_session("claude", "claude"), "q")
    assert resp.content == "still fine"


async def test_missing_binary_gives_an_actionable_error(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))  # nothing on PATH
    with pytest.raises(ModelCallError, match="arbiter doctor"):
        await call_session(make_session("claude", "claude"), "q")


# ── Timeout and cleanup ──────────────────────────────────────────────────────


async def test_a_hung_cli_times_out_instead_of_hanging_forever(shim_path):
    """No provider CLI offers a timeout flag, so the deadline is ours to enforce."""
    shim_path(hang=60)
    start = time.monotonic()
    with pytest.raises(ModelCallError, match="timeout"):
        await call_session(make_session("claude", "claude"), "q", timeout=1.0)
    assert time.monotonic() - start < 10


async def test_a_hung_cli_is_terminated_not_left_running(shim_path):
    shim_path(hang=60)
    procs: list = []
    real = asyncio.create_subprocess_exec

    async def spy(*a, **kw):
        p = await real(*a, **kw)
        procs.append(p)
        return p

    asyncio.create_subprocess_exec = spy
    try:
        with pytest.raises(ModelCallError):
            await call_session(make_session("claude", "claude"), "q", timeout=1.0)
    finally:
        asyncio.create_subprocess_exec = real

    assert procs, "expected a child process to have been spawned"
    # -15 is SIGTERM: the child was reaped rather than orphaned.
    assert procs[0].returncode is not None
    assert procs[0].returncode < 0


async def test_cancellation_still_reaps_the_child(shim_path):
    """Cleanup lives in a `finally`, not an `except`, so Ctrl-C mid-round does
    not leave orphaned CLI processes behind."""
    shim_path(hang=60)
    procs: list = []
    real = asyncio.create_subprocess_exec

    async def spy(*a, **kw):
        p = await real(*a, **kw)
        procs.append(p)
        return p

    asyncio.create_subprocess_exec = spy
    try:
        task = asyncio.create_task(call_session(make_session("claude", "claude"), "q", timeout=30))
        while not procs:  # noqa: ASYNC110 - polling for a real spawn
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        asyncio.create_subprocess_exec = real

    for _ in range(40):
        if procs[0].returncode is not None:
            break
        await asyncio.sleep(0.05)
    assert procs[0].returncode is not None, "child was orphaned after cancellation"


# ── Large prompts ────────────────────────────────────────────────────────────


async def test_a_prompt_too_large_for_argv_is_delivered_on_stdin(shim_path):
    """execve fails with E2BIG well before the provider launches: ARG_MAX is
    1 MiB on macOS and Linux caps a single argument at 128 KiB regardless. A plan
    plus a real diff clears that easily, so the prompt moves to stdin."""
    from arbiter.models import ARGV_PROMPT_LIMIT

    big = "d" * (ARGV_PROMPT_LIMIT + 5_000)
    argv_log = shim_path(claude_events("reviewed"))
    resp = await call_session(make_session("claude", "claude"), big)

    assert resp.content == "reviewed"
    argv = json.loads(argv_log.read_text().splitlines()[0])
    assert all(big not in arg for arg in argv), "prompt must not be in argv"


async def test_a_one_megabyte_prompt_does_not_raise_e2big(shim_path):
    shim_path(claude_events("ok"))
    resp = await call_session(make_session("claude", "claude"), "x" * 1_100_000)
    assert resp.content == "ok"


async def test_small_prompts_still_travel_in_argv(shim_path):
    argv_log = shim_path(claude_events("ok"))
    await call_session(make_session("claude", "claude"), "tiny question")
    argv = json.loads(argv_log.read_text().splitlines()[0])
    assert "tiny question" in argv


# ── Timeout coverage of the stdin write ──────────────────────────────────────


async def test_timeout_covers_writing_the_prompt_not_just_reading(shim_path):
    """The prompt write used to happen BEFORE the timeout scope, so a child that
    stopped consuming stdin could hang indefinitely with neither the deadline nor
    the cleanup active. A live run exceeded --timeout 180 until interrupted."""
    from arbiter.models import ARGV_PROMPT_LIMIT

    shim_path(hang=60)  # never reads stdin, never exits
    # Far beyond common 64 KiB pipe/transport buffers so drain must block when
    # the child never reads stdin. A ~37 KiB payload can fit and test the wrong path.
    big = "x" * 1_100_000
    assert len(big) > ARGV_PROMPT_LIMIT

    start = time.monotonic()
    with pytest.raises(ModelCallError, match="timeout"):
        await call_session(make_session("claude", "claude"), big, timeout=2.0)
    assert time.monotonic() - start < 15


async def test_a_child_that_never_reads_stdin_is_still_reaped(shim_path):
    from arbiter.models import ARGV_PROMPT_LIMIT

    shim_path(hang=60)
    procs: list = []
    real = asyncio.create_subprocess_exec

    async def spy(*a, **kw):
        p = await real(*a, **kw)
        procs.append(p)
        return p

    asyncio.create_subprocess_exec = spy
    try:
        with pytest.raises(ModelCallError):
            big = "x" * 1_100_000
            assert len(big) > ARGV_PROMPT_LIMIT
            await call_session(make_session("claude", "claude"), big, timeout=2.0)
    finally:
        asyncio.create_subprocess_exec = real

    assert procs and procs[0].returncode is not None, "child orphaned during stdin write"


async def test_a_flooded_stderr_does_not_deadlock_a_large_prompt(shim_path):
    """stderr is drained from the moment the child starts: a full stderr pipe
    makes the child stop reading stdin, which would otherwise deadlock the
    write."""
    from arbiter.models import ARGV_PROMPT_LIMIT

    shim_path(claude_events("survived"), stderr="noise\n" * 20_000)
    big = "y" * 1_100_000
    assert len(big) > ARGV_PROMPT_LIMIT
    resp = await call_session(make_session("claude", "claude"), big, timeout=30)
    assert resp.content == "survived"
