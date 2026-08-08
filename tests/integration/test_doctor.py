from __future__ import annotations

import json

import pytest

from arbiter.cli import main
from arbiter.doctor import check_provider, describe_capabilities, run_doctor
from arbiter.models import PROVIDERS
from conftest import claude_events, codex_events, gemini_events


async def test_missing_binary_is_reported_with_an_install_hint(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    health = await check_provider("codex")
    assert health.installed is False
    assert health.ok is False
    assert "npm install" in health.error


async def test_installed_and_answering_is_healthy(shim_path):
    shim_path(claude_events("ok"))
    health = await check_provider("claude")
    assert health.installed and health.authenticated is True
    assert health.ok


async def test_installed_but_unauthenticated_is_caught(shim_path):
    """A CLI can sit on PATH and still refuse to serve you — an expired login or
    an ineligible account tier only shows up on a real call."""
    shim_path(claude_events("x"), exit_code=1, stderr="IneligibleTierError: not supported")
    health = await check_provider("claude")
    assert health.installed is True
    assert health.authenticated is False
    assert health.ok is False
    assert "IneligibleTier" in health.error


async def test_no_probe_skips_the_live_call(shim_path, tmp_path):
    argv_log = shim_path(claude_events("ok"))
    health = await check_provider("claude", probe=False)
    assert health.installed is True
    assert health.authenticated is None  # unknown, not asserted
    assert not argv_log.exists(), "probe=False must not invoke the CLI"


@pytest.mark.parametrize("key", sorted(PROVIDERS))
def test_capabilities_are_reported_honestly_per_provider(key):
    caps = " ".join(describe_capabilities(PROVIDERS[key]))
    # Gemini genuinely cannot do structured output; the report must say so
    # rather than implying parity across providers.
    if key == "gemini":
        assert "no structured output" in caps
        assert "inherits user config" in caps
    else:
        assert "structured output" in caps


ALL_OK = {"claude": claude_events("ok"), "codex": codex_events("ok"), "gemini": gemini_events("ok")}


async def test_run_doctor_covers_every_provider(shim_path):
    shim_path(per_provider=ALL_OK)
    results = await run_doctor()
    assert {h.key for h in results} == set(PROVIDERS)


def test_doctor_json_output_and_exit_code(shim_path, capsys):
    shim_path(per_provider=ALL_OK)
    code = main(["doctor", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert {p["provider"] for p in payload} == set(PROVIDERS)
    assert all(p["authenticated"] for p in payload)


def test_doctor_exits_nonzero_when_a_provider_is_unusable(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert main(["doctor", "--no-probe", "--json"]) != 0
