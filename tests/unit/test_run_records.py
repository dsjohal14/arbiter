from __future__ import annotations

import json
import stat

import pytest

from arbiter.models import ModelResponse, make_session
from arbiter.run_records import RunRecorder, RunTrace, recorded_input, write_record


def test_recorded_input_embeds_content_and_a_stable_digest():
    item = recorded_input("hello\n", "plan.md")

    assert item == {
        "source": "plan.md",
        "sha256": "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03",
        "content": "hello\n",
    }


def test_trace_keeps_provider_output_but_not_invocation_or_session_ids():
    session = make_session("reviewer-a", "claude")
    response = ModelResponse(
        model="reviewer-a",
        content='{"findings": []}',
        session_id="private-session-id",
        duration=0.125,
        cmd=["claude", "--resume", "private-session-id"],
    )
    trace = RunTrace()

    trace.success("reviewer", session, response)

    assert trace.calls == [
        {
            "phase": "reviewer",
            "role": "reviewer-a",
            "provider": "claude",
            "cli": "claude",
            "model": "sonnet",
            "status": "completed",
            "duration_ms": 125,
            "content": '{"findings": []}',
            "error": None,
            "usage": None,
        }
    ]
    encoded = json.dumps(trace.calls)
    assert "private-session-id" not in encoded
    assert '"cmd"' not in encoded


def test_finished_record_is_versioned_owner_only_json(tmp_path):
    path = tmp_path / "nested" / "run.json"
    recorder = RunRecorder(
        path=path,
        command="verify",
        inputs={"plan": recorded_input("# Plan")},
        configuration={"models": ["claude"]},
    )

    payload = recorder.finish(exit_code=1, result={"status": "fail"})

    assert json.loads(path.read_text()) == payload
    assert payload["schema_version"] == 1
    assert payload["sensitive"] is True
    assert payload["status"] == "completed"
    assert payload["exit_code"] == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_failed_atomic_write_preserves_the_previous_record(tmp_path, monkeypatch):
    path = tmp_path / "run.json"
    path.write_text("old\n")

    def fail_dump(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("arbiter.run_records.json.dump", fail_dump)

    with pytest.raises(OSError, match="disk full"):
        write_record(path, {"new": True})

    assert path.read_text() == "old\n"
    assert list(tmp_path.glob(".run.json.*.tmp")) == []
