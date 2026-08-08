"""Provider-reported errors must reach the user intact.

Found by running `arbiter verify` on this repo's own diff: codex failed and the
user saw "codex exited 1: Reading additional input from stdin... ERROR failed to
load models cache", while the actual cause — "requires a newer version of Codex"
— was in an event we parsed past.
"""

from __future__ import annotations

import pytest

from arbiter.models import _parse_event, _schema_args, _unwrap_provider_error, make_session


def _errors(cli: str, obj: dict) -> list[str]:
    return [payload for kind, payload in _parse_event(cli, obj) if kind == "error"]


def test_codex_error_event_is_surfaced():
    obj = {
        "type": "error",
        "message": '{"type":"error","status":400,"error":{"type":"invalid_request_error",'
        '"message":"The \'gpt-5.6-sol\' model requires a newer version of Codex."}}',
    }
    assert _errors("codex", obj) == ["The 'gpt-5.6-sol' model requires a newer version of Codex."]


def test_codex_turn_failed_is_surfaced():
    obj = {"type": "turn.failed", "error": {"message": "rate limited, try again"}}
    assert _errors("codex", obj) == ["rate limited, try again"]


def test_claude_error_result_is_surfaced_not_treated_as_an_answer():
    obj = {"type": "result", "is_error": True, "result": "Credit balance too low"}
    assert _errors("claude", obj) == ["Credit balance too low"]
    assert not [k for k, _ in _parse_event("claude", obj) if k == "done"]


def test_claude_normal_result_is_still_an_answer():
    obj = {"type": "result", "result": "42", "session_id": "s1"}
    kinds = dict(_parse_event("claude", obj))
    assert kinds["done"] == "42"


def test_gemini_error_event_is_surfaced():
    assert _errors("gemini", {"type": "error", "message": "quota exceeded"}) == ["quota exceeded"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain message", "plain message"),
        ('{"error": {"message": "nested once"}}', "nested once"),
        ('{"message": "{\\"error\\":{\\"message\\":\\"nested twice\\"}}"}', "nested twice"),
        ("", "provider reported an unspecified error"),
        (None, "provider reported an unspecified error"),
        ("{not valid json", "{not valid json"),
    ],
)
def test_unwrapping_nested_provider_errors(raw, expected):
    assert _unwrap_provider_error(raw) == expected


# ── Schema wiring ────────────────────────────────────────────────────────────

SCHEMA = {"type": "object"}


def test_missing_schema_writer_is_a_loud_caller_error():
    """Silently dropping the flag would run codex unstructured with no signal
    that the requested schema never applied."""
    sess = make_session("codex", "codex", output_schema=SCHEMA)
    with pytest.raises(ValueError, match="schema_path_writer"):
        _schema_args(sess, None)


def test_provider_without_structured_output_is_not_an_error():
    """Gemini genuinely cannot do this; the prose fallback handles it."""
    sess = make_session("gemini", "gemini", output_schema=SCHEMA)
    assert _schema_args(sess, None) == []


def test_no_schema_requested_is_not_an_error():
    assert _schema_args(make_session("codex", "codex"), None) == []
