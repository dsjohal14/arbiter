"""Adding up what a run cost, without inventing a number.

Every rule here is a way a cost summary can lie: a total that mixes two
providers' definitions of a token, a cost that covers one call and reads as the
cost of the run, a failed call left out, an unknown reported as zero.
"""

from __future__ import annotations

from arbiter import usage


def _call(provider, status="completed", model=None, duration_ms=1000, **reported):
    return {
        "phase": "reviewer",
        "role": provider,
        "provider": provider,
        "cli": provider,
        "model": model,
        "status": status,
        "duration_ms": duration_ms,
        "usage": reported or None,
    }


def test_token_counts_are_summed_within_a_provider_never_across_them():
    """Providers do not mean the same thing by "input tokens": one counts cached
    reads inside it, another beside it. A cross-provider total has no definition."""
    summary = usage.summarize(
        [
            _call("claude", model="sonnet", input_tokens=100, output_tokens=10),
            _call("claude", model="sonnet", input_tokens=50, output_tokens=5),
            _call("codex", input_tokens=1000, output_tokens=100),
        ]
    )

    by_label = {group["label"]: group for group in summary["providers"]}
    assert by_label["claude (sonnet)"]["input_tokens"] == 150
    assert by_label["codex"]["input_tokens"] == 1000
    assert "input_tokens" not in summary, "no token total across providers"


def test_the_same_provider_under_two_models_is_two_groups():
    """A reviewer on one model and a consolidator on another are priced apart."""
    summary = usage.summarize(
        [
            _call("claude", model="sonnet", cost_usd=0.01),
            _call("claude", model="opus", cost_usd=0.2),
        ]
    )
    assert [g["label"] for g in summary["providers"]] == ["claude (sonnet)", "claude (opus)"]


def test_a_cost_says_how_many_calls_it_covers():
    summary = usage.summarize([_call("claude", cost_usd=0.04), _call("codex", input_tokens=9)])

    assert summary["cost_usd"] == 0.04
    assert summary["priced_calls"] == 1 and summary["calls"] == 2
    assert usage.headline(summary) == (
        "2 provider calls · $0.0400 for the 1 of 2 calls that reported a cost"
    )


def test_an_unknown_cost_is_not_a_free_run():
    summary = usage.summarize([_call("codex", input_tokens=9)])

    assert summary["cost_usd"] is None
    assert "no provider reported a cost" in usage.headline(summary)


def test_a_fully_priced_run_states_its_cost_plainly():
    summary = usage.summarize([_call("claude", cost_usd=0.04), _call("claude", cost_usd=0.01)])
    assert usage.headline(summary, 38.24) == "2 provider calls in 38.2s · $0.0500"


def test_a_failed_call_still_counts():
    """It took time and may have been billed. Leaving it out understates a run
    by exactly the calls that went wrong."""
    summary = usage.summarize(
        [
            _call("claude", status="failed", duration_ms=4000, cost_usd=0.02),
            _call("claude", duration_ms=1000, cost_usd=0.03),
        ]
    )

    assert summary["calls"] == 2 and summary["failed_calls"] == 1
    assert summary["cost_usd"] == 0.05
    assert summary["provider_time_ms"] == 5000
    assert "1 failed" in usage.headline(summary)


def test_a_count_a_provider_never_reported_stays_unknown():
    summary = usage.summarize([_call("codex", input_tokens=9)])
    assert summary["providers"][0]["cache_write_tokens"] is None


def test_a_run_that_called_nothing_says_so():
    assert usage.headline(usage.summarize([])) == "No provider was called."
