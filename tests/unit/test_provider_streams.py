"""Stream shapes and token counts, pinned to what the real CLIs actually emit.

Every event below was captured verbatim from the installed CLI, then trimmed.
That is the point: a provider that renames a field has to show up here as a red
test, rather than as an empty answer or a run record full of nulls.

Captured from claude 2.x, codex 0.5x, agy 1.1.11.
"""

from __future__ import annotations

from arbiter.models import _parse_event

# ── agy / antigravity, keyed on `event` rather than `type` ───────────────────

AGY_INIT = {
    "event": "init",
    "conversation_id": "e6f527cd-7322-47de-9d3b-1bcad741f0c8",
    "init": {"cwd": "/repo", "tools": ["view_file"], "permission_mode": "request-review"},
}

AGY_DELTA = {
    "event": "step_update",
    "step_update": {
        "conversation_id": "e6f527cd-7322-47de-9d3b-1bcad741f0c8",
        "step_index": 2,
        "state": "ACTIVE",
        "step_type": "agent_response",
        "text_delta": "ok",
    },
}

AGY_CHECKPOINT = {
    "event": "step_update",
    "step_update": {
        "conversation_id": "e6f527cd-7322-47de-9d3b-1bcad741f0c8",
        "step_index": 3,
        "state": "DONE",
        "step_type": "checkpoint",
        "duration_seconds": 1.157388,
        "usage": {"input_tokens": 99, "output_tokens": 4, "total_tokens": 103},
    },
}

AGY_RESULT = {
    "event": "result",
    "result": {
        "conversation_id": "e6f527cd-7322-47de-9d3b-1bcad741f0c8",
        "status": "SUCCESS",
        "response": "ok\n",
        "duration_seconds": 4.003625,
        "num_turns": 1,
        "usage": {
            "input_tokens": 17437,
            "output_tokens": 33,
            "thinking_tokens": 28,
            "cache_read_tokens": 0,
            "total_tokens": 17470,
        },
    },
}

AGY_ERROR = {
    "event": "result",
    "result": {
        "conversation_id": "",
        "status": "ERROR",
        "response": "",
        "error": 'Error: empty prompt. Usage: agy --print "your prompt here"',
        "num_turns": 0,
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    },
}


def _kinds(cli: str, obj: dict) -> dict:
    return dict(_parse_event(cli, obj))


def test_agy_init_yields_the_conversation_id():
    assert _kinds("agy", AGY_INIT)["session"] == "e6f527cd-7322-47de-9d3b-1bcad741f0c8"


def test_agy_agent_response_is_the_answer_text():
    assert _kinds("agy", AGY_DELTA)["delta"] == "ok"


def test_agy_non_answer_steps_contribute_no_text():
    """Tool and checkpoint steps carry deltas of their own. Treating them as
    answer text would splice tool chatter into the JSON a reviewer returns."""
    assert _parse_event("agy", AGY_CHECKPOINT) == []


def test_agy_result_carries_the_final_answer_and_the_session():
    parsed = _kinds("agy", AGY_RESULT)
    assert parsed["done"] == "ok\n"
    assert parsed["session"] == "e6f527cd-7322-47de-9d3b-1bcad741f0c8"


def test_agy_a_non_success_status_is_an_error_not_an_empty_answer():
    parsed = _parse_event("agy", AGY_ERROR)
    assert dict(parsed)["error"] == 'Error: empty prompt. Usage: agy --print "your prompt here"'
    assert not [k for k, _ in parsed if k == "done"]


def test_agy_a_failure_without_an_error_message_still_names_the_status():
    obj = {"event": "result", "result": {"status": "TIMEOUT", "response": ""}}
    assert "TIMEOUT" in dict(_parse_event("agy", obj))["error"]


# ── Token usage ──────────────────────────────────────────────────────────────
