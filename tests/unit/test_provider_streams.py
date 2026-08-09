"""Stream shapes and token counts, pinned to what the real CLIs actually emit.

Every event below was captured verbatim from the installed CLI, then trimmed.
That is the point: a provider that renames a field has to show up here as a red
test, rather than as an empty answer or a run record full of nulls.

Captured from claude 2.x, codex 0.5x, agy 1.1.11.
"""

from __future__ import annotations

import pytest

from arbiter.models import PROVIDERS, Usage, _parse_event, parse_usage

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

CLAUDE_RESULT = {
    "type": "result",
    "result": "ok",
    "session_id": "s1",
    "total_cost_usd": 0.0369241,
    "usage": {
        "input_tokens": 2,
        "cache_creation_input_tokens": 4728,
        "cache_read_input_tokens": 26357,
        "output_tokens": 4,
        "service_tier": "standard",
    },
}

CODEX_TURN = {
    "type": "turn.completed",
    "usage": {
        "input_tokens": 12226,
        "cached_input_tokens": 2432,
        "output_tokens": 5,
        "reasoning_output_tokens": 0,
    },
}


def test_claude_usage_including_both_kinds_of_cache_token():
    usage = parse_usage("claude", CLAUDE_RESULT)
    assert usage == Usage(
        input_tokens=2,
        output_tokens=4,
        cached_input_tokens=26357,
        cache_write_tokens=4728,
        cost_usd=0.0369241,
    )


def test_codex_usage_maps_reasoning_output_separately():
    assert parse_usage("codex", CODEX_TURN) == Usage(
        input_tokens=12226,
        output_tokens=5,
        cached_input_tokens=2432,
        reasoning_tokens=0,
    )


def test_agy_usage_maps_thinking_tokens_and_keeps_its_own_total():
    assert parse_usage("agy", AGY_RESULT["result"]) == Usage(
        input_tokens=17437,
        output_tokens=33,
        cached_input_tokens=0,
        reasoning_tokens=28,
        total_tokens=17470,
    )


def test_a_total_is_never_invented_for_a_provider_that_does_not_report_one():
    """Summing the parts would produce a number whose billing meaning we do not
    know — claude bills cache reads and writes at different rates."""
    assert parse_usage("claude", CLAUDE_RESULT).total_tokens is None
    assert parse_usage("codex", CODEX_TURN).total_tokens is None


def test_usage_rides_along_with_the_events_that_carry_it():
    assert _kinds("claude", CLAUDE_RESULT)["usage"].output_tokens == 4
    assert _kinds("codex", CODEX_TURN)["usage"].input_tokens == 12226
    assert _kinds("agy", AGY_RESULT)["usage"].total_tokens == 17470


def test_an_event_with_no_usage_block_reports_none():
    assert parse_usage("claude", {"type": "result", "result": "ok"}) is None
    assert "usage" not in _kinds("claude", {"type": "result", "result": "ok", "session_id": "s"})


def test_a_provider_we_cannot_read_counts_for_reports_none():
    """Gemini's stream may well carry counts; nothing here has seen one, and a
    guessed field name would put fabricated numbers in a run record."""
    assert PROVIDERS["gemini"].reports_usage is False
    assert parse_usage("gemini", {"usage": {"input_tokens": 5}}) is None


def test_zero_is_recorded_as_zero_and_missing_stays_missing():
    usage = parse_usage("codex", {"usage": {"input_tokens": 0}})
    assert usage.input_tokens == 0
    assert usage.output_tokens is None


def test_a_usage_block_of_nothing_useful_is_not_a_usage_record():
    assert parse_usage("codex", {"usage": {"unrecognized": 7}}) is None


@pytest.mark.parametrize("junk", [True, "12", None, {"a": 1}, [1]])
def test_non_integer_counts_are_dropped_rather_than_coerced(junk):
    """A bool is an int in Python; letting `True` through would record 1 token."""
    assert parse_usage("codex", {"usage": {"input_tokens": junk}}) is None


def test_every_provider_that_claims_usage_can_actually_be_read():
    """`reports_usage` is a promise `arbiter doctor` repeats to the user."""
    samples = {"claude": CLAUDE_RESULT, "codex": CODEX_TURN, "agy": AGY_RESULT["result"]}
    for provider in PROVIDERS.values():
        if provider.reports_usage:
            assert parse_usage(provider.cli, samples[provider.cli]) is not None
