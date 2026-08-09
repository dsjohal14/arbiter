"""`arbiter replay` — re-derive a verdict from a recorded run, calling nothing.

A run record holds the reviewers' and consolidator's final responses. Everything
downstream of those responses is deterministic: parsing, merging, the evidence
downgrade rules, and the roll-up. Replay re-runs exactly that half of the tool
from the record, so a change to how Arbiter reads a response can be evaluated
against runs that already happened, at zero cost and with no provider installed.

What replay does NOT do is re-check evidence. Reading a cited line or running a
cited test is a fact about a working tree, and a record does not pin one — the
checks are restored from the record and the same downgrade rules re-applied to
them. So replay answers "does today's logic reach the recorded verdict from the
recorded responses?", not "is that verdict still true of the repository today".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import criteria as crit
from . import display, plan_review, verify
from .evidence import EvidenceCheck
from .prompts import Blinding
from .run_records import SCHEMA_VERSION

REPLAYABLE_COMMANDS = ("verify", "plan review")


class ReplayError(RuntimeError):
    """The record is unreadable, unsupported, or too incomplete to replay."""


# ── Reading the record ────────────────────────────────────────────────────────


def load_record(path: Path) -> dict:
    """Read one run record, refusing anything this build cannot faithfully replay."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ReplayError(f"Could not read {path}: {e}") from e
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ReplayError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(record, dict):
        raise ReplayError(f"{path} is not a run record.")

    version = record.get("schema_version")
    if not isinstance(version, int):
        raise ReplayError(f"{path} has no schema_version; it is not a run record.")
    if version > SCHEMA_VERSION:
        raise ReplayError(
            f"{path} uses record schema v{version}; this arbiter understands "
            f"up to v{SCHEMA_VERSION}. Upgrade arbiter to replay it."
        )

    command = record.get("command")
    if command not in REPLAYABLE_COMMANDS:
        raise ReplayError(
            f"Cannot replay a {command!r} record. Replayable commands: "
            f"{', '.join(REPLAYABLE_COMMANDS)}."
        )
    return record


def _input(record: dict, name: str) -> str:
    item = (record.get("inputs") or {}).get(name)
    if not isinstance(item, dict) or not isinstance(item.get("content"), str):
        raise ReplayError(f"The record has no recorded {name}; it cannot be replayed.")
    return item["content"]


def _calls(record: dict, phase: str) -> list[dict]:
    """Completed provider responses for one phase, in the order they arrived."""
    return [
        call
        for call in record.get("provider_calls") or []
        if isinstance(call, dict)
        and call.get("phase") == phase
        and call.get("status") == "completed"
        and isinstance(call.get("content"), str)
    ]


def _reports(record: dict) -> list[tuple[str, str]]:
    return [(str(call.get("role") or ""), call["content"]) for call in _calls(record, "reviewer")]


def _consolidated(record: dict) -> str | None:
    """The consolidator's response, or None if it failed and the run fell back."""
    calls = _calls(record, "consolidator")
    return calls[-1]["content"] if calls else None


def replay_criteria(record: dict) -> list[crit.Criterion]:
    """Rebuild the contract the recorded run worked from.

    `verify` always reads pinned criteria. `plan review` reads them when the plan
    was pinned and otherwise extracts them, in which case the extractor's own
    recorded response is what has to be re-parsed — re-extracting would call a
    model and could enumerate a different contract.
    """
    plan = _input(record, "plan")
    source = (record.get("configuration") or {}).get("criteria_source")
    extractor = _calls(record, "extractor")

    if record["command"] == "verify" or source == "pinned" or (not extractor and source is None):
        try:
            return crit.read_criteria(plan)
        except crit.CriteriaError as e:
            raise ReplayError(f"The recorded plan has no usable criteria: {e}") from e

    if not extractor:
        raise ReplayError(
            "The record's criteria were extracted, but no extractor response was "
            "recorded; there is no contract to replay against."
        )
    criteria = crit.parse_extraction(extractor[-1]["content"])
    if not criteria:
        raise ReplayError("The recorded extractor response yields no acceptance criteria.")
    return criteria


def _blinding(recorded: dict | None) -> Blinding | None:
    data = (recorded or {}).get("blinding")
    if not isinstance(data, dict) or not isinstance(data.get("label_to_model"), dict):
        return None
    seed = data.get("seed")
    return Blinding(
        seed=seed if isinstance(seed, int) else 0,
        label_to_model={str(k): str(v) for k, v in data["label_to_model"].items()},
    )


# ── Replaying ─────────────────────────────────────────────────────────────────


@dataclass
class ReplayOutcome:
    command: str
    result: verify.VerifyResult | plan_review.PlanReviewResult
    recorded: dict | None
    drift: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def comparable(self) -> bool:
        return self.recorded is not None

    @property
    def matches(self) -> bool:
        """True only when there was a recorded result AND it was reproduced."""
        return self.comparable and not self.drift

    @property
    def exit_code(self) -> int:
        return self.result.exit_code

    def as_dict(self) -> dict:
        return {
            "command": self.command,
            "matches": self.matches,
            "drift": self.drift,
            "notes": self.notes,
            "replayed": self.result.as_dict(),
            "recorded": self.recorded,
        }


def replay(record: dict) -> ReplayOutcome:
    """Re-derive this record's result from the responses it captured."""
    recorded = record.get("result")
    recorded = recorded if isinstance(recorded, dict) else None
    notes: list[str] = []
    if recorded is None:
        notes.append(
            "The record has no result — the run failed before one existed — so there "
            "is nothing to compare the replay against."
        )

    criteria = replay_criteria(record)
    reports = _reports(record)
    if not reports:
        raise ReplayError("The record contains no completed reviewer responses to replay.")
    if _consolidated(record) is None:
        notes.append("No consolidator response was recorded; replaying the per-reviewer fallback.")

    if record["command"] == "verify":
        result = _replay_verify(record, criteria, reports, recorded, notes)
        drift = _drift_verify(result, recorded) if recorded else []
    else:
        result = _replay_plan_review(record, criteria, reports, recorded)
        drift = _drift_plan_review(result, recorded) if recorded else []

    return ReplayOutcome(record["command"], result, recorded, drift, notes)


def _replay_verify(
    record: dict,
    criteria: list[crit.Criterion],
    reports: list[tuple[str, str]],
    recorded: dict | None,
    notes: list[str],
) -> verify.VerifyResult:
    config = record.get("configuration") or {}
    diff = _input(record, "diff")

    results, findings = verify.assemble_criteria_results(
        criteria,
        reports,
        _consolidated(record),
        verify.changed_paths(diff),
        verify.added_paths(diff),
    )

    if recorded is None:
        notes.append(
            "No evidence checks were recorded, so every citation replays as unchecked; "
            "statuses that the original run downgraded on evidence will differ."
        )
    else:
        _restore_evidence(results, recorded)

    strict_tests = bool(config.get("require_discriminating_tests"))
    pinned = bool(config.get("require_pinned_evidence"))
    for result in results:
        verify.apply_downgrades(result, pinned, strict_tests)

    completed = [role for role, _ in reports]
    failed = [key for key in config.get("models") or [] if key not in completed]
    return verify.finalize_verify(
        results,
        findings,
        completed,
        failed,
        _blinding(recorded),
        require_tests=bool(config.get("require_tests")),
        require_pinned_evidence=pinned or strict_tests,
    )


def _recorded_rows(recorded: dict) -> dict[str, dict]:
    """The recorded per-criterion rows, keyed by id. A row without one is not
    addressable and is dropped rather than crashing the replay."""
    return {
        str(row["id"]): row
        for row in recorded.get("criteria") or []
        if isinstance(row, dict) and row.get("id")
    }


def _restore_evidence(results: list[verify.CriterionResult], recorded: dict) -> None:
    """Reattach the checks the original run performed.

    `evidence_source` comes back from the record too: whether a plan-selected
    test survived comparison against the base revision was settled by a worktree
    that no longer exists, and re-deriving it from the diff alone would be a
    strictly worse answer than the one the run already reached.
    """
    by_id = _recorded_rows(recorded)
    for result in results:
        row = by_id.get(result.id)
        if row is None:
            continue
        for attr in ("impl_check", "test_check", "baseline_check"):
            data = row.get(attr)
            if isinstance(data, dict):
                setattr(result, attr, EvidenceCheck.from_dict(data))
        source = row.get("evidence_source")
        if isinstance(source, str) and source:
            result.evidence_source = source


def _replay_plan_review(
    record: dict,
    criteria: list[crit.Criterion],
    reports: list[tuple[str, str]],
    recorded: dict | None,
) -> plan_review.PlanReviewResult:
    config = record.get("configuration") or {}
    reviewer_keys = [str(k) for k in config.get("models") or []] or [k for k, _ in reports]
    intake = plan_review.intake_reports(reports, reviewer_keys, criteria)
    if not intake.usable_reports:
        raise ReplayError("Every recorded reviewer response is unusable; nothing to replay.")

    consolidated_raw = _consolidated(record)
    consolidated = None
    if consolidated_raw is not None:
        parsed = plan_review.parse_findings_report(consolidated_raw, criteria)
        consolidated = parsed.findings if parsed.valid else None

    return plan_review.finalize_plan_review(criteria, intake, consolidated, _blinding(recorded))


# ── Drift ─────────────────────────────────────────────────────────────────────


def _drift_verify(result: verify.VerifyResult, recorded: dict) -> list[str]:
    drift: list[str] = []
    if recorded.get("status") != result.status:
        drift.append(f"verdict: recorded {recorded.get('status')}, replayed {result.status}")

    was = _recorded_rows(recorded)
    now = {c.id: c for c in result.criteria}
    for cid in sorted(set(was) | set(now)):
        if cid not in was:
            drift.append(f"{cid}: absent from the record, replayed {now[cid].status}")
        elif cid not in now:
            drift.append(f"{cid}: recorded {was[cid].get('status')}, absent from the replay")
        elif was[cid].get("status") != now[cid].status:
            drift.append(f"{cid}: recorded {was[cid].get('status')}, replayed {now[cid].status}")

    before, after = len(recorded.get("ungrounded_findings") or []), len(result.findings)
    if before != after:
        drift.append(f"ungrounded findings: recorded {before}, replayed {after}")
    return drift


def _drift_plan_review(result: plan_review.PlanReviewResult, recorded: dict) -> list[str]:
    drift: list[str] = []
    if bool(recorded.get("ready")) != result.ready:
        drift.append(
            f"readiness: recorded {'ready' if recorded.get('ready') else 'blocked'}, "
            f"replayed {'ready' if result.ready else 'blocked'}"
        )

    was_counts = recorded.get("counts") or {}
    for kind, count in result.counts.items():
        if int(was_counts.get(kind, 0)) != count:
            drift.append(f"{kind} findings: recorded {was_counts.get(kind, 0)}, replayed {count}")

    before, after = len(recorded.get("criteria") or []), len(result.criteria)
    if before != after:
        drift.append(f"proposed criteria: recorded {before}, replayed {after}")
    return drift


# ── Output ────────────────────────────────────────────────────────────────────


def render(outcome: ReplayOutcome, path: Path) -> None:
    if outcome.command == "verify":
        verify.render(outcome.result)
    else:
        plan_review.render(outcome.result)

    display.print_section("replay")
    display.print_note(f"Replayed {path} — no providers were called.")
    display.print_note(
        "Evidence checks were restored from the record, not re-run against this tree."
    )
    for note in outcome.notes:
        display.print_note(note)

    if not outcome.comparable:
        return
    if outcome.matches:
        display.print_note("✓  The replay reproduces the recorded result exactly.")
        return
    display.print_error(f"{len(outcome.drift)} difference(s) from the recorded result:")
    for line in outcome.drift:
        display.print(f"    {line}")
    display.print()
