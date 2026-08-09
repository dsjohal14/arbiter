from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

FAKE_CLI = Path(__file__).parent / "fake_cli.py"


PROVIDER_CLIS = ("claude", "codex", "gemini", "agy")


@pytest.fixture
def shim_path(tmp_path, monkeypatch):
    """Put fake provider executables on PATH, and nothing else that answers to
    those names.

    PATH is *replaced*, not prepended. Prepending leaves the developer's own
    `claude` and `gemini` reachable, so anything that asks "is this installed?"
    finds a real one — and the suite starts making paid calls, or blocks on an
    unauthenticated CLI's login prompt. Only the directories the rest of the
    suite genuinely needs (git, and the system utilities the shims run) survive.

    Returns a callable that configures the fake's behaviour for a test.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in PROVIDER_CLIS:
        shim = bindir / name
        shim.write_text(
            f'#!/bin/sh\nFAKE_CLI_NAME={name} exec "{sys.executable}" "{FAKE_CLI}" "$@"\n'
        )
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    kept = [str(bindir)]
    if git := shutil.which("git"):
        kept.append(os.path.dirname(git))
    kept += ["/usr/bin", "/bin"]
    monkeypatch.setenv("PATH", os.pathsep.join(dict.fromkeys(kept)))

    def _write(events, suffix):
        stream = tmp_path / f"stream{suffix}.jsonl"
        stream.write_text("\n".join(json.dumps(e) for e in events) + "\n")
        return str(stream)

    def configure(
        events=None,
        *,
        per_provider=None,
        exit_code=0,
        hang=None,
        garbage=False,
        stderr=None,
        prose=None,
        installed=None,
    ):
        if installed is not None:
            # Model a machine that simply does not have the other CLIs, which is
            # what decides the default panel.
            for shim in bindir.iterdir():
                if shim.name not in installed:
                    shim.unlink()
            for name in PROVIDER_CLIS:
                if name not in installed and shutil.which(name):
                    raise AssertionError(
                        f"{name} is still resolvable at {shutil.which(name)} after being "
                        "removed from the shim directory; the test would call it for real."
                    )
        if events is not None:
            monkeypatch.setenv("FAKE_CLI_STREAM", _write(events, ""))
        for name, evs in (per_provider or {}).items():
            monkeypatch.setenv(f"FAKE_CLI_STREAM_{name.upper()}", _write(evs, f"-{name}"))
        if exit_code:
            monkeypatch.setenv("FAKE_CLI_EXIT_CODE", str(exit_code))
        if hang:
            monkeypatch.setenv("FAKE_CLI_HANG", str(hang))
        if garbage:
            monkeypatch.setenv("FAKE_CLI_GARBAGE", "1")
        if stderr:
            monkeypatch.setenv("FAKE_CLI_STDERR", stderr)
        if prose:
            monkeypatch.setenv("FAKE_CLI_PROSE", prose)
        argv_log = tmp_path / "argv.jsonl"
        monkeypatch.setenv("FAKE_CLI_ARGV_OUT", str(argv_log))
        return argv_log

    return configure


def claude_events(text: str, session_id: str = "sess-1", stream_deltas: bool = True):
    """A realistic claude stream-json sequence."""
    events = [{"type": "system", "subtype": "init", "session_id": session_id}]
    if stream_deltas:
        events.append(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": text},
                },
            }
        )
    events.append({"type": "result", "result": text, "session_id": session_id})
    return events


def codex_events(text: str, thread_id: str = "thread-1"):
    return [
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
        {"type": "turn.completed"},
    ]


def gemini_events(text: str, session_id: str = "gsess-1"):
    return [
        {"type": "init", "session_id": session_id},
        {"type": "message", "role": "assistant", "content": text},
        {"type": "result"},
    ]


def agy_events(text: str, conversation_id: str = "conv-1", usage: dict | None = None):
    """Antigravity keys its stream on `event`, not `type`, and reports usage."""
    return [
        {"event": "init", "conversation_id": conversation_id, "init": {"cwd": "."}},
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": conversation_id,
                "step_index": 0,
                "state": "ACTIVE",
                "step_type": "agent_response",
                "text_delta": text,
            },
        },
        {
            "event": "result",
            "result": {
                "conversation_id": conversation_id,
                "status": "SUCCESS",
                "response": text,
                "num_turns": 1,
                "usage": usage
                if usage is not None
                else {
                    "input_tokens": 1200,
                    "output_tokens": 42,
                    "thinking_tokens": 7,
                    "cache_read_tokens": 0,
                    "total_tokens": 1242,
                },
            },
        },
    ]
