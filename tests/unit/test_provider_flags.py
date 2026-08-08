"""Provider flag and argv-shape regressions.

Each test here corresponds to a defect found in review of the previous commit.
"""

from __future__ import annotations

import pytest

from arbiter.models import (
    ARGV_PROMPT_LIMIT,
    PROVIDERS,
    _build_cmd,
    make_arbiter_session,
    make_session,
    prompt_goes_on_stdin,
)
from arbiter.prompts import JUDGE_SCHEMA
from arbiter.schema_files import schema_writer


def test_codex_is_not_run_ephemeral():
    """`--ephemeral` means "do not persist session files", which makes the very
    next `codex exec resume <id>` fail — breaking multi-round debate and the
    follow-up loop under default settings."""
    assert "--ephemeral" not in PROVIDERS["codex"].isolate_flags
    assert "--ephemeral" not in _build_cmd(make_session("codex", "codex"), "q")


def test_codex_resume_still_works_after_isolation_flags():
    sess = make_session("codex", "codex")
    sess.session_id, sess.started = "thread-1", True
    cmd = _build_cmd(sess, "q")
    assert cmd[:4] == ["codex", "exec", "resume", "thread-1"]


def test_claude_isolation_covers_more_than_mcp():
    """`--strict-mcp-config` alone drops ambient MCP servers but leaves
    CLAUDE.md, skills, hooks, and plugins in place — so the reviewers were still
    answering under different, undisclosed system prompts."""
    cmd = _build_cmd(make_session("claude", "claude"), "q")
    assert "--safe-mode" in cmd
    assert "--strict-mcp-config" in cmd


def test_a_codex_judge_can_be_built(tmp_path):
    """`arbiter debate --judge codex` crashed before reaching a provider: the
    judge call site never passed a schema writer, and codex needs its schema on
    disk."""
    sess = make_arbiter_session("codex", output_schema=JUDGE_SCHEMA)
    with schema_writer() as write:
        cmd = _build_cmd(sess, "q", write)
    assert "--output-schema" in cmd


# ── argv size ────────────────────────────────────────────────────────────────


def test_small_prompts_stay_in_argv():
    assert not prompt_goes_on_stdin("a short question")


def test_large_prompts_move_to_stdin():
    """A plan plus a real diff exceeds OS limits: ARG_MAX is 1 MiB on macOS and
    Linux caps any single argument at 128 KiB regardless. execve fails with
    E2BIG long before the provider launches."""
    assert prompt_goes_on_stdin("x" * (ARGV_PROMPT_LIMIT + 1))


def test_the_limit_is_measured_in_bytes_not_characters():
    # Multi-byte characters must not sneak past a character-based check.
    assert prompt_goes_on_stdin("é" * (ARGV_PROMPT_LIMIT // 2 + 1))


@pytest.mark.parametrize("key", sorted(PROVIDERS))
def test_stdin_mode_keeps_the_prompt_out_of_argv(key):
    big = "x" * (ARGV_PROMPT_LIMIT + 1)
    cmd = _build_cmd(make_session(key, key), big, via_stdin=True)
    assert all(big not in arg for arg in cmd)


@pytest.mark.parametrize("key", sorted(PROVIDERS))
def test_stdin_mode_keeps_every_other_flag(key):
    provider = PROVIDERS[key]
    sess = make_session(key, key)  # one session: ids are minted per call
    argv_cmd = _build_cmd(sess, "q")
    stdin_cmd = _build_cmd(sess, "q", via_stdin=True)
    for flag in provider.readonly_flags + provider.isolate_flags:
        assert flag in stdin_cmd
    # The only difference should be the prompt (and codex's `--` guard).
    assert set(stdin_cmd) <= set(argv_cmd)


def test_argv_mode_still_guards_a_dash_leading_prompt():
    cmd = _build_cmd(make_session("codex", "codex"), "--help me")
    assert cmd[-2:] == ["--", "--help me"]


# ── Per-turn schema override ─────────────────────────────────────────────────


def test_a_judge_session_can_be_called_free_form_for_follow_ups():
    """The arbiter session carries JUDGE_SCHEMA. Reusing it for a follow-up used
    to force {winner, reason, best_answer} JSON, which makes the `REPOLL:`
    directive undetectable."""
    sess = make_arbiter_session("claude", output_schema=JUDGE_SCHEMA)
    assert "--json-schema" in _build_cmd(sess, "judge this")
    assert "--json-schema" not in _build_cmd(sess, "follow up", schema=None)


def test_omitting_the_override_keeps_the_session_schema():
    sess = make_arbiter_session("claude", output_schema=JUDGE_SCHEMA)
    assert "--json-schema" in _build_cmd(sess, "q")


def test_a_per_turn_schema_can_replace_the_session_one():
    sess = make_arbiter_session("claude", output_schema=JUDGE_SCHEMA)
    cmd = _build_cmd(sess, "q", schema={"type": "object", "properties": {"zzz": {}}})
    assert "zzz" in cmd[cmd.index("--json-schema") + 1]


def test_free_form_override_needs_no_schema_writer_for_codex():
    """Otherwise a codex follow-up would raise instead of simply going free-form."""
    sess = make_arbiter_session("codex", output_schema=JUDGE_SCHEMA)
    assert "--output-schema" not in _build_cmd(sess, "follow up", schema=None)
