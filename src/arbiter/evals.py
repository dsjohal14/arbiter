"""Evaluation cases: hand-labelled runs that say how often Arbiter is right.

An eval case is a directory holding the inputs of one run plus a human's answer
key for it. Scoring compares a run's result against that key, which turns "the
gate feels reliable" into numbers you can defend: how often a broken
implementation still passes, how often a real requirement gap is caught, how
often a plan finding is one a reviewer would act on.

The key is deliberately coarse. Per-criterion status is exact, because that is
the unit Arbiter promises. Findings are matched on their enumerated fields plus
required substrings, because a finding's prose is written by a model and no two
runs phrase it the same way — an exact-text answer key would measure wording.

    evals/<case-id>/
      plan.md        the plan, with its criteria pinned for a verify case
      change.diff    the implementation under review (verify cases only)
      expected.json  the answer key described below

Cases are scored against a run record, so labelling a case costs one paid run
and every later scoring of it is free. See evals/README.md for the format.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import criteria as crit
from . import plan_review, verify

SCHEMA_VERSION = 1

COMMANDS = ("verify", "plan review")

PLAN_FILE = "plan.md"
DIFF_FILE = "change.diff"
EXPECTED_FILE = "expected.json"
REPO_DIR = "repo"

VERDICTS: dict[str, tuple[str, ...]] = {
    "verify": ("pass", "fail", "unclear"),
    "plan review": ("ready", "blocked"),
}

# Enumerated finding fields an expectation may constrain exactly, and the free
# text it may require substrings in.
FINDING_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "verify": {"severity": verify.SEVERITIES},
    "plan review": {
        "kind": plan_review.KINDS,
        "action": plan_review.ACTIONS,
        "target": (),  # any criterion id
    },
}
FINDING_TEXT: dict[str, tuple[str, ...]] = {
    "verify": ("claim", "evidence"),
    "plan review": ("problem", "proposed_criterion", "proposed_test", "quote"),
}


class EvalCaseError(ValueError):
    """A case directory is missing something, or its answer key is malformed."""


# ── The answer key ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExpectedFinding:
    """One finding a run must (or must not) produce.

    Constraints are exact on enumerated fields and substring on prose, so the
    key stays stable across reruns without becoming so loose that any finding
    satisfies it.
    """

    label: str
    constraints: dict[str, str] = field(default_factory=dict)
    match: tuple[str, ...] = ()

    def matches(self, finding: dict, text_fields: tuple[str, ...]) -> bool:
        for name, expected in self.constraints.items():
            if str(finding.get(name) or "").strip().lower() != expected.strip().lower():
                return False
        blob = " ".join(str(finding.get(f) or "") for f in text_fields).lower()
        return all(needle.lower() in blob for needle in self.match)

    def as_dict(self) -> dict:
        return {"label": self.label, **self.constraints, "match": list(self.match)}


@dataclass(frozen=True)
class EvalCase:
    id: str
    path: Path
    command: str
    description: str
    plan: str
    diff: str | None = None
    # The tree the diff applies to, when the case ships one. A verify run has to
    # be recorded from inside it, or the evidence checks have nothing to read.
    repo: Path | None = None
    verdict: str | None = None
    criteria: dict[str, str] = field(default_factory=dict)
    must_find: tuple[ExpectedFinding, ...] = ()
    must_not_find: tuple[ExpectedFinding, ...] = ()

    @property
    def text_fields(self) -> tuple[str, ...]:
        return FINDING_TEXT[self.command]


def _expected_findings(raw, command: str, where: str) -> tuple[ExpectedFinding, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise EvalCaseError(f"{where} must be a list of finding expectations.")

    allowed = FINDING_FIELDS[command]
    out: list[ExpectedFinding] = []
    for index, item in enumerate(raw, 1):
        at = f"{where}[{index}]"
        if not isinstance(item, dict):
            raise EvalCaseError(f"{at} must be an object.")

        constraints: dict[str, str] = {}
        for name, value in item.items():
            if name in ("label", "match"):
                continue
            if name not in allowed:
                raise EvalCaseError(
                    f"{at} constrains unknown field {name!r}; "
                    f"a {command} finding allows: {', '.join(sorted(allowed)) or 'none'}."
                )
            text = str(value).strip()
            if allowed[name] and text.lower() not in allowed[name]:
                raise EvalCaseError(
                    f"{at} has {name}={text!r}; allowed: {', '.join(allowed[name])}."
                )
            constraints[name] = text

        needles = item.get("match") or []
        if not isinstance(needles, list) or not all(isinstance(n, str) for n in needles):
            raise EvalCaseError(f"{at} `match` must be a list of strings.")
        if not constraints and not needles:
            raise EvalCaseError(f"{at} constrains nothing, so every finding would satisfy it.")

        label = str(item.get("label") or "").strip()
        if not label:
            parts = [f"{k}={v}" for k, v in constraints.items()] + [f"~{n}" for n in needles]
            label = " ".join(parts)
        out.append(ExpectedFinding(label, constraints, tuple(needles)))
    return tuple(out)


def load_case(path: Path) -> EvalCase:
    """Read and validate one case directory. Raises rather than scoring a case
    whose answer key cannot mean what it says."""
    path = Path(path)
    if not path.is_dir():
        raise EvalCaseError(f"Not a case directory: {path}")

    expected_path = path / EXPECTED_FILE
    if not expected_path.is_file():
        raise EvalCaseError(f"{path} has no {EXPECTED_FILE}.")
    try:
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise EvalCaseError(f"Could not read {expected_path}: {e}") from e
    if not isinstance(expected, dict):
        raise EvalCaseError(f"{expected_path} must contain a JSON object.")

    version = expected.get("schema_version")
    if not isinstance(version, int):
        raise EvalCaseError(f"{expected_path} needs an integer schema_version.")
    if version > SCHEMA_VERSION:
        raise EvalCaseError(
            f"{expected_path} uses case schema v{version}; this arbiter understands "
            f"up to v{SCHEMA_VERSION}."
        )

    command = expected.get("command")
    if command not in COMMANDS:
        raise EvalCaseError(
            f"{expected_path} has command {command!r}; expected one of {', '.join(COMMANDS)}."
        )

    plan_path = path / PLAN_FILE
    if not plan_path.is_file():
        raise EvalCaseError(f"{path} has no {PLAN_FILE}.")
    plan = plan_path.read_text(encoding="utf-8")
    if not plan.strip():
        raise EvalCaseError(f"{plan_path} is empty.")

    diff: str | None = None
    if command == "verify":
        diff_path = path / DIFF_FILE
        if not diff_path.is_file():
            raise EvalCaseError(f"{path} has no {DIFF_FILE}; a verify case needs one.")
        diff = diff_path.read_text(encoding="utf-8")
        if not diff.strip():
            raise EvalCaseError(f"{diff_path} is empty.")

    verdict = expected.get("verdict")
    if verdict is not None:
        verdict = str(verdict).strip().lower()
        if verdict not in VERDICTS[command]:
            raise EvalCaseError(
                f"{expected_path} expects verdict {verdict!r}; a {command} case allows "
                f"{', '.join(VERDICTS[command])}."
            )

    criteria = _expected_criteria(expected, command, plan, expected_path)

    return EvalCase(
        id=str(expected.get("id") or path.name),
        path=path,
        command=command,
        description=str(expected.get("description") or "").strip(),
        plan=plan,
        diff=diff,
        repo=(path / REPO_DIR) if (path / REPO_DIR).is_dir() else None,
        verdict=verdict,
        criteria=criteria,
        must_find=_expected_findings(expected.get("must_find"), command, "must_find"),
        must_not_find=_expected_findings(expected.get("must_not_find"), command, "must_not_find"),
    )


def _expected_criteria(expected: dict, command: str, plan: str, where: Path) -> dict[str, str]:
    """Per-criterion answer key, checked against the plan's own pinned contract.

    An id the plan never promised cannot be scored, and silently ignoring it
    would inflate accuracy on a case that is actually mislabelled.
    """
    raw = expected.get("criteria")
    if raw is None:
        return {}
    if command != "verify":
        raise EvalCaseError(
            f"{where} sets `criteria`, but a plan review produces proposed edits, "
            "not per-criterion statuses. Express those as must_find entries."
        )
    if not isinstance(raw, dict):
        raise EvalCaseError(f"{where} `criteria` must map criterion ids to expected statuses.")

    try:
        pinned = {c.id.upper() for c in crit.read_criteria(plan)}
    except crit.CriteriaError as e:
        raise EvalCaseError(f"{where} labels criteria, but {PLAN_FILE} has none pinned: {e}") from e

    out: dict[str, str] = {}
    for cid, status in raw.items():
        key = str(cid).strip().upper()
        if key not in pinned:
            raise EvalCaseError(f"{where} labels {key}, which {PLAN_FILE} does not pin.")
        text = str(status).strip().lower()
        if text not in verify.STATUSES:
            raise EvalCaseError(
                f"{where} expects {key}={text!r}; allowed: {', '.join(verify.STATUSES)}."
            )
        out[key] = text
    return out


# ── Scoring ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CriterionScore:
    id: str
    expected: str
    actual: str | None

    @property
    def correct(self) -> bool:
        return self.actual == self.expected

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "expected": self.expected,
            "actual": self.actual,
            "correct": self.correct,
        }


@dataclass(frozen=True)
class FindingScore:
    label: str
    required: bool  # from must_find; otherwise it is a finding that must not appear
    found: bool

    @property
    def correct(self) -> bool:
        return self.found is self.required

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "required": self.required,
            "found": self.found,
            "correct": self.correct,
        }


@dataclass
class CaseScore:
    case_id: str
    command: str
    verdict_expected: str | None
    verdict_actual: str | None
    criteria: list[CriterionScore] = field(default_factory=list)
    findings: list[FindingScore] = field(default_factory=list)

    @property
    def verdict_correct(self) -> bool | None:
        if self.verdict_expected is None:
            return None
        return self.verdict_actual == self.verdict_expected

    @property
    def criteria_accuracy(self) -> float | None:
        if not self.criteria:
            return None
        return sum(c.correct for c in self.criteria) / len(self.criteria)

    @property
    def recall(self) -> float | None:
        """Share of the findings the case says must be there that were found."""
        required = [f for f in self.findings if f.required]
        if not required:
            return None
        return sum(f.found for f in required) / len(required)

    @property
    def false_positives(self) -> list[FindingScore]:
        return [f for f in self.findings if not f.required and f.found]

    @property
    def failures(self) -> list[str]:
        out: list[str] = []
        if self.verdict_correct is False:
            out.append(f"verdict: expected {self.verdict_expected}, got {self.verdict_actual}")
        for c in self.criteria:
            if not c.correct:
                out.append(f"{c.id}: expected {c.expected}, got {c.actual or 'no verdict'}")
        for f in self.findings:
            if f.correct:
                continue
            out.append(
                f"missed finding: {f.label}" if f.required else f"unwanted finding: {f.label}"
            )
        return out

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict:
        return {
            "case": self.case_id,
            "command": self.command,
            "passed": self.passed,
            "verdict": {
                "expected": self.verdict_expected,
                "actual": self.verdict_actual,
                "correct": self.verdict_correct,
            },
            "criteria_accuracy": self.criteria_accuracy,
            "recall": self.recall,
            "criteria": [c.as_dict() for c in self.criteria],
            "findings": [f.as_dict() for f in self.findings],
            "failures": self.failures,
        }


def _actual_verdict(command: str, result: dict) -> str | None:
    if command == "verify":
        status = result.get("status")
        return str(status) if isinstance(status, str) else None
    if "ready" not in result:
        return None
    return "ready" if result.get("ready") else "blocked"


def score(case: EvalCase, result: dict) -> CaseScore:
    """Compare one run's result dict against this case's answer key."""
    findings_key = "ungrounded_findings" if case.command == "verify" else "findings"
    actual_findings = [f for f in result.get(findings_key) or [] if isinstance(f, dict)]
    statuses = {
        str(c.get("id", "")).upper(): c.get("status")
        for c in result.get("criteria") or []
        if isinstance(c, dict)
    }

    def seen(expectation: ExpectedFinding) -> bool:
        return any(expectation.matches(f, case.text_fields) for f in actual_findings)

    return CaseScore(
        case_id=case.id,
        command=case.command,
        verdict_expected=case.verdict,
        verdict_actual=_actual_verdict(case.command, result),
        criteria=[
            CriterionScore(cid, expected, statuses.get(cid))
            for cid, expected in sorted(case.criteria.items())
        ],
        findings=[FindingScore(e.label, True, seen(e)) for e in case.must_find]
        + [FindingScore(e.label, False, seen(e)) for e in case.must_not_find],
    )
