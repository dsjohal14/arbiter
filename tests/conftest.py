from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

FAKE_CLI = Path(__file__).parent / "fake_cli.py"


@pytest.fixture
def shim_path(tmp_path, monkeypatch):
    """Put fake `claude`/`codex`/`gemini` executables at the front of PATH.

    Returns a callable that configures the fake's behaviour for a test.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("claude", "codex", "gemini"):
        shim = bindir / name
        shim.write_text(
            f'#!/bin/sh\nFAKE_CLI_NAME={name} exec "{sys.executable}" "{FAKE_CLI}" "$@"\n'
        )
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def _write(events, suffix):
        stream = tmp_path / f"stream{suffix}.jsonl"
        stream.write_text("\n".join(json.dumps(e) for e in events) + "\n")
        return str(stream)

    def configure(
        events=None, *, per_provider=None, exit_code=0, hang=None, garbage=False, stderr=None
    ):
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
