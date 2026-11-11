"""What a run's provider calls took, added up without inventing anything.

Every call records what its provider reported: some token counts, sometimes a
cost, always a duration. Rolling those up is where a tool starts to lie, so the
rules are fixed here:

  - Token counts are summed within one provider and model, never across them.
    Providers do not mean the same thing by "input tokens" — one counts cached
    reads inside it and another beside it — so a cross-provider total would be a
    number with no definition.
  - A cost is only ever the sum of what providers themselves reported, and it
    says how many calls that covers. "$0.04" from one of three calls is not
    the cost of the run, and must not be able to read as one.
  - A failed call counts. It took time and may have been billed; leaving it out
    understates a run by exactly the calls that went wrong.
  - Time is the sum of the calls' own durations. Calls overlap, so that is not
    how long anyone waited — the run's wall-clock duration is reported apart.
"""

from __future__ import annotations

from dataclasses import fields

from .models import Usage

_TOKEN_FIELDS = tuple(f.name for f in fields(Usage) if f.name != "cost_usd")


def _label(call: dict) -> str:
    cli, model = str(call.get("cli") or call.get("provider") or "unknown"), call.get("model")
    return f"{cli} ({model})" if model else cli


def summarize(calls: list[dict]) -> dict:
    """Roll one run's provider calls up by provider and model."""
    groups: dict[str, dict] = {}
    for call in calls:
        if not isinstance(call, dict):
            continue
        group = groups.setdefault(
            _label(call),
            {
                "provider": call.get("provider"),
                "model": call.get("model"),
                "label": _label(call),
                "calls": 0,
                "failed_calls": 0,
                "priced_calls": 0,
                "duration_ms": 0,
                "cost_usd": None,
                **dict.fromkeys(_TOKEN_FIELDS),
            },
        )
        group["calls"] += 1
        group["failed_calls"] += call.get("status") == "failed"
        group["duration_ms"] += call.get("duration_ms") or 0

        reported = call.get("usage") or {}
        for name in _TOKEN_FIELDS:
            if isinstance(reported.get(name), int):
                group[name] = (group[name] or 0) + reported[name]
        if isinstance(reported.get("cost_usd"), (int, float)):
            group["cost_usd"] = round((group["cost_usd"] or 0.0) + reported["cost_usd"], 6)
            group["priced_calls"] += 1

    providers = list(groups.values())
    priced = sum(g["priced_calls"] for g in providers)
    return {
        "calls": sum(g["calls"] for g in providers),
        "failed_calls": sum(g["failed_calls"] for g in providers),
        "priced_calls": priced,
        # None, not zero, when no provider said: an unknown cost is not a free run.
        "cost_usd": round(sum(g["cost_usd"] or 0.0 for g in providers), 6) if priced else None,
        "provider_time_ms": sum(g["duration_ms"] for g in providers),
        "providers": providers,
    }


def headline(summary: dict, seconds: float | None = None) -> str:
    """One line for the end of a run: calls, time, and cost with its coverage."""
    calls = summary.get("calls") or 0
    if not calls:
        return "No provider was called."
    parts = [f"{calls} provider call{'' if calls == 1 else 's'}"]
    if seconds is not None:
        parts[0] += f" in {seconds:.1f}s"
    if failed := summary.get("failed_calls"):
        parts.append(f"{failed} failed")

    priced, cost = summary.get("priced_calls") or 0, summary.get("cost_usd")
    if cost is None:
        parts.append("no provider reported a cost")
    elif priced == calls:
        parts.append(f"${cost:.4f}")
    else:
        parts.append(f"${cost:.4f} for the {priced} of {calls} calls that reported a cost")
    return " · ".join(parts)
