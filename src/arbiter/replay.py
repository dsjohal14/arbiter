"""`arbiter replay` — re-derive a verdict from a recorded run, calling nothing.

A run record holds the reviewers' and consolidator's final responses. Everything
downstream of those responses is deterministic: parsing, merging, the evidence
downgrade rules, and the roll-up. Replay re-runs exactly that half of the tool
from the record, so a change to how Arbiter reads a response can be evaluated
against runs that already happened, at zero cost and with no provider installed.

What replay does NOT do is re-check evidence. Reading a cited line or running a
cited test is a fact about a working tree; its observations are restored from a
record and the same downgrade rules re-applied. A workspace digest can bind an
eval score to the same tree, but cannot recreate that tree. Replay therefore
answers "does today's logic reach the recorded verdict from the recorded
responses?", not "is that verdict still true of the repository today".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import criteria as crit
from . import display, plan_review, verify
from .evidence import TEST_OUTCOMES, EvidenceCheck
from .prompts import Blinding
from .run_records import SCHEMA_VERSION

REPLAYABLE_COMMANDS = ("verify", "plan review")

# Every version this build can actually parse. v1/v2 keep observations inside
# `result`, so they can be replayed but cannot certify an exact comparison.
# v3 predates stored test outcomes; its checks are read through the prose rules
# they were written under, so it still replays to the verdict it recorded.
SUPPORTED_RECORD_VERSIONS = (1, 2, 3, 4)

# From here on a test check states its outcome instead of implying it.
_OUTCOME_VERSION = 4


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
    # `True` is an int in Python, and a bool here means the field is not a
    # version at all. An unknown *older* version is no more replayable than a
    # newer one: there is no parser for it and no migration to run.
    if not isinstance(version, int) or isinstance(version, bool):
        raise ReplayError(f"{path} has no integer schema_version; it is not a run record.")
    if version not in SUPPORTED_RECORD_VERSIONS:
        known = ", ".join(f"v{v}" for v in SUPPORTED_RECORD_VERSIONS)
        hint = " Upgrade arbiter to replay it." if version > SCHEMA_VERSION else ""
        raise ReplayError(
            f"{path} uses record schema v{version}; this arbiter reads {known}.{hint}"
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
    """Reviewer reports in the order the live run handled them.

    Live, reports come back in panel order whatever order the reviewers
    finished in. The record lists calls as they completed. Where no consolidated
    verdict exists, the first reviewer on the panel to give the worst status
    supplies the row — so replaying in completion order could quote a different
    reviewer than the run did.
    """
    reports = [
        (str(call.get("role") or ""), call["content"]) for call in _calls(record, "reviewer")
    ]
    panel = [str(key) for key in (record.get("configuration") or {}).get("models") or []]
    position = {key: index for index, key in enumerate(panel)}
    return sorted(reports, key=lambda report: position.get(report[0], len(panel)))


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
    exact_comparison: bool = True

    @property
    def comparable(self) -> bool:
        return self.recorded is not None

    @property
    def matches(self) -> bool:
        """True only when there was a recorded result AND it was reproduced."""
        return self.comparable and self.exact_comparison and not self.drift

    @property
    def exit_code(self) -> int:
        return self.result.exit_code

    def as_dict(self) -> dict:
        return {
            "command": self.command,
            "matches": self.matches,
            "drift": self.drift,
            "notes": self.notes,
            "exact_comparison": self.exact_comparison,
            "replayed": self.result.as_dict(),
            "recorded": self.recorded,
        }


def replay(record: dict) -> ReplayOutcome:
    """Re-derive this record's result from the responses it captured."""
    recorded = record.get("result")
    recorded = recorded if isinstance(recorded, dict) else None
    notes: list[str] = []
    exact_comparison = int(record["schema_version"]) >= 3
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
    if not exact_comparison:
        notes.append(
            "Legacy record: replay observations were stored inside the expected result, "
            "so this run can be replayed but cannot pass an exact --check."
        )

    if record["command"] == "verify":
        result = _replay_verify(record, criteria, reports, recorded, notes)
    else:
        result = _replay_plan_review(record, criteria, reports, recorded)

    drift = _drift(result.as_dict(), recorded) if recorded else []
    return ReplayOutcome(record["command"], result, recorded, drift, notes, exact_comparison)


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
        _restore_evidence(
            results,
            _observations(record, recorded),
            strict=int(record["schema_version"]) >= 3,
        )

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
        _blinding(_observations(record, recorded)) if recorded is not None else None,
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


def _observations(record: dict, recorded: dict) -> dict:
    """Independent replay inputs in v3; legacy result-as-input before that."""
    if int(record["schema_version"]) < 3:
        return recorded
    observations = record.get("observations")
    if not isinstance(observations, dict):
        raise ReplayError("The v3 record has a result but no replay observations.")
    if "blinding" not in observations:
        raise ReplayError("The v3 record's replay observations have no blinding value.")
    blinding = observations["blinding"]
    if blinding is not None:
        if not isinstance(blinding, dict) or not isinstance(blinding.get("seed"), int):
            raise ReplayError("The v3 record has malformed blinding observations.")
        if isinstance(blinding.get("seed"), bool) or not isinstance(
            blinding.get("label_to_model"), dict
        ):
            raise ReplayError("The v3 record has malformed blinding observations.")
    if record["command"] == "verify":
        rows = observations.get("criteria_evidence")
        if not isinstance(rows, list):
            raise ReplayError("The v3 verify record has no criterion evidence observations.")
        required = {"id", "impl_check", "test_check", "baseline_check", "evidence_source"}
        if any(not isinstance(row, dict) or not required.issubset(row) for row in rows):
            raise ReplayError("The v3 verify record has malformed criterion evidence observations.")
        ids = [row["id"] for row in rows]
        if any(not isinstance(cid, str) or not cid for cid in ids) or len(ids) != len(set(ids)):
            raise ReplayError("The v3 verify record has malformed criterion evidence observations.")
        if int(record["schema_version"]) >= _OUTCOME_VERSION:
            # Without this a hand-edited check would quietly fall back to the
            # legacy prose rules and replay to a status no v4 run could reach.
            for row in rows:
                check = row["test_check"]
                if isinstance(check, dict) and check.get("outcome") not in TEST_OUTCOMES:
                    raise ReplayError(
                        f"The record's test evidence for {row['id']} does not state a known "
                        "outcome, so the verdict rules have nothing to apply."
                    )
    return observations


def _restore_evidence(
    results: list[verify.CriterionResult], observations: dict, *, strict: bool
) -> None:
    """Reattach the checks the original run performed.

    `evidence_source` is an observation too: whether a plan-selected test
    survived comparison against the base revision was settled by a worktree
    that may no longer exist. In v3 it is independent of the expected result,
    so changing that result field is still visible as drift.
    """
    rows = observations.get("criteria_evidence")
    source = {"criteria": rows} if isinstance(rows, list) else observations
    by_id = _recorded_rows(source)
    for result in results:
        row = by_id.get(result.id)
        if row is None:
            if strict:
                raise ReplayError(f"The record has no evidence observations for {result.id}.")
            continue
        for attr in ("impl_check", "test_check", "baseline_check"):
            data = row.get(attr)
            if isinstance(data, dict):
                setattr(result, attr, EvidenceCheck.from_dict(data))
            elif strict and data is not None:
                raise ReplayError(f"The record has malformed {attr} observations for {result.id}.")
        source = row.get("evidence_source")
        if strict and (not isinstance(source, str) or not source):
            raise ReplayError(f"The record has no evidence_source observation for {result.id}.")
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

    blinding = _blinding(_observations(record, recorded)) if recorded is not None else None
    return plan_review.finalize_plan_review(criteria, intake, consolidated, blinding)


# ── Drift ─────────────────────────────────────────────────────────────────────


def _keyed(value: object) -> object:
    """Make lists of identified objects comparable by identity, not position.

    A criterion that moves in the sort order because its status changed would
    otherwise report as a difference in every field of two rows.
    """
    if isinstance(value, dict):
        return {k: _keyed(v) for k, v in value.items()}
    if isinstance(value, list):
        if value and all(isinstance(item, dict) and item.get("id") for item in value):
            return {str(item["id"]): _keyed(item) for item in value}
        return [_keyed(item) for item in value]
    return value


def _show(value: object) -> str:
    text = "absent" if value is _MISSING else json.dumps(value, sort_keys=True, default=str)
    return text if len(text) <= 120 else text[:117] + "..."


class _Missing:
    pass


_MISSING = _Missing()


def _walk(was: object, now: object, path: str = "") -> list[str]:
    """Every field where the recorded and replayed results disagree.

    Exhaustive on purpose. Comparing only verdicts and counts let a changed
    finding severity, a rewritten proposed criterion, or a dropped citation
    replay as an exact match — which is the one thing `--check` must never say.
    """
    if isinstance(was, dict) and isinstance(now, dict):
        drift: list[str] = []
        for key in sorted(set(was) | set(now)):
            drift += _walk(
                was.get(key, _MISSING), now.get(key, _MISSING), f"{path}.{key}" if path else key
            )
        return drift
    if isinstance(was, list) and isinstance(now, list):
        drift = []
        for i in range(max(len(was), len(now))):
            a = was[i] if i < len(was) else _MISSING
            b = now[i] if i < len(now) else _MISSING
            drift += _walk(a, b, f"{path}[{i}]")
        return drift
    if was == now and type(was) is type(now):
        return []
    return [f"{path or 'result'}: recorded {_show(was)}, replayed {_show(now)}"]


# Rendered first, in this order, because they are the answer the run exists to
# give. Everything else follows in path order.
_HEADLINE = ("status", "ready", "exit_code")


def _drift(result_dict: dict, recorded: dict) -> list[str]:
    lines = _walk(_keyed(recorded), _keyed(result_dict))
    head = [ln for ln in lines if ln.split(":", 1)[0] in _HEADLINE]
    return head + [ln for ln in lines if ln not in head]


# ── Output ────────────────────────────────────────────────────────────────────

# Terminal output stays readable; --json always carries the full list.
_DRIFT_SHOWN = 20


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
    if not outcome.exact_comparison and not outcome.drift:
        display.print_error(
            "This legacy record replays without visible drift, but its observations "
            "were not stored independently, so an exact match cannot be certified."
        )
        return
    display.print_error(f"{len(outcome.drift)} difference(s) from the recorded result:")
    for line in outcome.drift[:_DRIFT_SHOWN]:
        display.print(f"    {line}")
    if len(outcome.drift) > _DRIFT_SHOWN:
        display.print_note(
            f"    ... and {len(outcome.drift) - _DRIFT_SHOWN} more; --json lists every one."
        )
    display.print()
