from __future__ import annotations

import json
import stat

import pytest

from arbiter import run_records
from arbiter.models import ModelCallError, ModelResponse, Usage, make_session
from arbiter.run_records import (
    RunRecorder,
    RunTrace,
    recorded_input,
    write_record,
)


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
    assert payload["schema_version"] == run_records.SCHEMA_VERSION
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


def test_a_failed_call_keeps_what_it_spent():
    """Providers commonly report usage in the same event that reports the error,
    and a timeout still burned wall-clock. Recording null for both biases every
    later cost and latency summary downward by exactly the runs that failed."""
    session = make_session("reviewer-a", "claude")
    error = ModelCallError(
        "claude: credit balance too low",
        duration=1.5,
        usage=Usage(input_tokens=900, output_tokens=3, cost_usd=0.01),
    )
    trace = RunTrace()

    trace.failure("reviewer", session, error)

    call = trace.calls[0]
    assert call["status"] == "failed"
    assert call["duration_ms"] == 1500
    assert call["usage"]["input_tokens"] == 900
    assert call["usage"]["cost_usd"] == 0.01


def test_a_failure_carrying_nothing_still_records_cleanly():
    """Not every exception reaching the recorder is a ModelCallError."""
    trace = RunTrace()

    trace.failure("reviewer", make_session("reviewer-a", "claude"), RuntimeError("boom"))

    assert trace.calls[0]["duration_ms"] is None
    assert trace.calls[0]["usage"] is None


# ── Workspace identity ───────────────────────────────────────────────────────


def test_workspace_identity_digests_only_the_files_in_scope(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "untouched.py").write_text("y = 2\n")

    identity = run_records.workspace_identity(["app.py"], tmp_path)

    assert list(identity["files"]) == ["app.py"]
    assert identity["files"]["app.py"] == run_records.digest("x = 1\n")


def test_two_trees_holding_different_content_get_different_identities(tmp_path):
    """This is the whole point: the plan and diff can be identical while the
    tree the evidence checks read is not."""
    for name, body in (("a", "x = 1\n"), ("b", "x = 999\n")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "app.py").write_text(body)

    first = run_records.workspace_identity(["app.py"], tmp_path / "a")
    second = run_records.workspace_identity(["app.py"], tmp_path / "b")

    assert first["files"] != second["files"]


def test_a_file_absent_from_one_tree_shows_up_as_a_differing_key(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")

    identity = run_records.workspace_identity(["app.py", "gone.py"], tmp_path)

    assert "gone.py" not in identity["files"]
