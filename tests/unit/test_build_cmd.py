"""`_build_cmd` is a pure function of (Session, prompt). These tests lock every
flag decision — including the one that used to let two gemini sessions resume
each other."""

from __future__ import annotations

import itertools

import pytest

from arbiter.models import PROVIDERS, _build_cmd, make_session


def _pairs(cmd: list[str]) -> list[tuple[str, str]]:
    return list(itertools.pairwise(cmd))


@pytest.mark.parametrize("key", sorted(PROVIDERS))
def test_prompt_is_present_and_never_shell_interpolated(key):
    sess = make_session(key, key)
    cmd = _build_cmd(sess, "hello; rm -rf /")
    assert "hello; rm -rf /" in cmd  # passed as one argv element, never a shell string


@pytest.mark.parametrize("key", sorted(PROVIDERS))
def test_readonly_isolation_applies_provider_flags(key):
    provider = PROVIDERS[key]
    cmd = _build_cmd(make_session(key, key, isolation="readonly"), "q")
    for flag in provider.readonly_flags + provider.isolate_flags:
        assert flag in cmd


@pytest.mark.parametrize("key", sorted(PROVIDERS))
def test_open_isolation_omits_restriction_flags(key):
    provider = PROVIDERS[key]
    cmd = _build_cmd(make_session(key, key, isolation="open"), "q")
    for flag in provider.readonly_flags + provider.isolate_flags:
        assert flag not in cmd


# ── Session pinning ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["claude", "gemini"])
def test_first_call_pins_a_preassigned_session_id(key):
    sess = make_session(key, key)
    assert sess.session_id is not None
    cmd = _build_cmd(sess, "q")
    assert ("--session-id", sess.session_id) in _pairs(cmd)
    assert "--resume" not in cmd


@pytest.mark.parametrize("key", ["claude", "gemini"])
def test_second_call_resumes_that_exact_session_id(key):
    sess = make_session(key, key)
    sess.started = True
    cmd = _build_cmd(sess, "q")
    assert ("--resume", sess.session_id) in _pairs(cmd)
    assert "--session-id" not in cmd


def test_gemini_never_resumes_the_ambiguous_latest_handle():
    """`--resume latest` resolved to the project-latest gemini session, so a
    gemini debater and a gemini judge in the same run resumed each other."""
    sess = make_session("gemini", "gemini")
    sess.started = True
    cmd = _build_cmd(sess, "q")
    assert "latest" not in cmd


def test_two_sessions_of_one_provider_get_distinct_ids():
    a = make_session("gemini", "gemini")
    b = make_session("arbiter", "gemini")
    assert a.session_id != b.session_id


def test_codex_resumes_by_its_own_thread_id():
    sess = make_session("codex", "codex")
    assert sess.session_id is None  # codex mints its own
    sess.session_id = "thread-abc"
    sess.started = True
    cmd = _build_cmd(sess, "q")
    assert cmd[:4] == ["codex", "exec", "resume", "thread-abc"]


def test_codex_passes_the_prompt_after_a_double_dash():
    """A prompt starting with '-' must not be read as a flag by the child."""
    cmd = _build_cmd(make_session("codex", "codex"), "--help me")
    assert cmd[-2:] == ["--", "--help me"]


# ── Structured output ────────────────────────────────────────────────────────

SCHEMA = {"type": "object", "properties": {"winner": {"type": "string"}}}


def test_claude_receives_the_schema_inline():
    sess = make_session("claude", "claude", output_schema=SCHEMA)
    cmd = _build_cmd(sess, "q")
    i = cmd.index("--json-schema")
    assert '"winner"' in cmd[i + 1]


def test_codex_receives_the_schema_as_a_file_path(tmp_path):
    written = {}

    def writer(schema):
        p = tmp_path / "schema.json"
        p.write_text("{}")
        written["schema"] = schema
        return str(p)

    sess = make_session("codex", "codex", output_schema=SCHEMA)
    cmd = _build_cmd(sess, "q", schema_path_writer=writer)
    i = cmd.index("--output-schema")
    assert cmd[i + 1].endswith("schema.json")
    assert written["schema"] == SCHEMA


def test_gemini_silently_skips_structured_output_it_cannot_do():
    sess = make_session("gemini", "gemini", output_schema=SCHEMA)
    cmd = _build_cmd(sess, "q")
    assert "--json-schema" not in cmd
    assert "--output-schema" not in cmd


def test_model_flag_only_appears_when_a_model_is_configured():
    with_model = _build_cmd(make_session("claude", "claude", model="opus"), "q")
    assert ("--model", "opus") in _pairs(with_model)

    without = _build_cmd(make_session("gemini", "gemini"), "q")
    assert "-m" not in without
