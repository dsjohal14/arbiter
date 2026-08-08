"""`arbiter verify` — a falsification-first gate for AI-generated changes.

Reviewers do not vote on whether a change is good. They are asked to *falsify*
it: to name specific, evidence-backed reasons the diff fails the approved plan.
Each finding must cite a file, a line, a command, or a failing test. A finding
without evidence is not a finding.

The verdict is then derived mechanically from the surviving findings rather than
picked by a judge. That is the difference between a gate and an opinion — and it
is what makes the exit code safe to wire into pre-commit or CI.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import display
from .models import IsolationMode, ModelCallError, Session, call_session, make_session
from .prompts import Blinding, blind
from .streaming import stream_parallel

SEVERITIES = ("blocking", "major", "minor", "question")

# Exit codes. `unclear` gets its own code so a CI job can tell "reviewers were
# not confident" apart from "you invoked the tool wrong" or "the run crashed".
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_UNCLEAR = 3
EXIT_RUNTIME = 4


FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "claim": {"type": "string"},
                    "acceptance_criterion": {"type": ["string", "null"]},
                    "evidence": {"type": "string"},
                    "validation_command": {"type": ["string", "null"]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["severity", "claim", "evidence", "confidence"],
            },
        },
        "unevaluable_criteria": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["findings", "unevaluable_criteria"],
}

CONSOLIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "claim": {"type": "string"},
                    "acceptance_criterion": {"type": ["string", "null"]},
                    "evidence": {"type": "string"},
                    "validation_command": {"type": ["string", "null"]},
                    "status": {
                        "type": "string",
                        "enum": ["confirmed", "unresolved", "refuted"],
                    },
                    "raised_by": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["severity", "claim", "evidence", "status"],
            },
        },
        "unevaluable_criteria": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["findings", "unevaluable_criteria"],
}


@dataclass
class Finding:
    severity: str
    claim: str
    evidence: str
    acceptance_criterion: str | None = None
    validation_command: str | None = None
    confidence: str = "medium"
    status: str = "unresolved"
    raised_by: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class VerifyResult:
    status: str  # pass | fail | unclear
    findings: list[Finding]
    unevaluable: list[str]
    counts: dict[str, int]
    blinding: Blinding | None = None
    reviewers: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return {
            "pass": EXIT_PASS,
            "fail": EXIT_FAIL,
            "unclear": EXIT_UNCLEAR,
        }[self.status]

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "counts": self.counts,
            "findings": [f.as_dict() for f in self.findings],
            "unevaluable_criteria": self.unevaluable,
            "reviewers": self.reviewers,
            "blinding": self.blinding.as_dict() if self.blinding else None,
        }


# ── Inputs ────────────────────────────────────────────────────────────────────


class VerifyInputError(ValueError):
    """The plan or diff could not be read."""


def read_plan(spec: str) -> str:
    if spec == "-":
        text = sys.stdin.read()
    else:
        path = Path(spec)
        if not path.is_file():
            raise VerifyInputError(f"Plan file not found: {spec}")
        text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise VerifyInputError("The plan is empty.")
    return text


def read_diff(spec: str) -> str:
    """`staged` (default), `working`, `-` for stdin, or a path to a diff file."""
    if spec == "-":
        text = sys.stdin.read()
    elif spec in ("staged", "working"):
        args = ["git", "diff"] + (["--cached"] if spec == "staged" else [])
        try:
            proc = subprocess.run(args, capture_output=True, text=True, check=False)
        except FileNotFoundError as e:
            raise VerifyInputError("git is not installed or not on PATH.") from e
        if proc.returncode != 0:
            raise VerifyInputError(f"git diff failed: {proc.stderr.strip()[:200]}")
        text = proc.stdout
    else:
        path = Path(spec)
        if not path.is_file():
            raise VerifyInputError(f"Diff file not found: {spec}")
        text = path.read_text(encoding="utf-8")

    if not text.strip():
        hint = " (nothing staged — did you forget `git add`?)" if spec == "staged" else ""
        raise VerifyInputError(f"The diff is empty{hint}.")
    return text


_DIFF_PATH_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)


def build_manifest(diff: str, root: Path | None = None) -> list[str]:
    """The explicit read-only set of paths reviewers are told they may consult.

    Reviewers run with the provider's read-only/plan mode where one exists, but
    a reviewer that cannot read the repo cannot verify code. Rather than blanket
    tool-disabling, we name exactly what is in scope — which also keeps the three
    reviewers looking at the same material.
    """
    root = root or Path.cwd()
    paths: list[str] = []
    for a, b in _DIFF_PATH_RE.findall(diff):
        for p in (a, b):
            if p not in paths and (root / p).exists():
                paths.append(p)
    return paths


# ── Prompts ───────────────────────────────────────────────────────────────────


def build_review_prompt(plan: str, diff: str, manifest: list[str]) -> str:
    files = "\n".join(f"  - {p}" for p in manifest) or "  (none resolved on disk)"
    return f"""You are a reviewer on a verification gate. Your job is to FALSIFY this change,
not to praise it. Assume it is wrong and try to prove it.

You are reviewing a diff against an approved plan. Answer only these questions:
  1. Does the implementation satisfy the approved plan?
  2. Which acceptance criteria in the plan are unmet?
  3. Which assumptions in the plan or the diff can be disproved?
  4. What concrete blocking issues remain?

RULES:
- Every finding MUST cite evidence: a `file:line`, a command, or a failing test.
  A finding you cannot evidence is not a finding — leave it out.
- Do not report style preferences, naming opinions, or hypothetical refactors.
- If a criterion cannot be evaluated from the plan and diff alone, do not guess.
  List it under `unevaluable_criteria` instead.
- Severity: `blocking` means this must not merge. Use it only when you can
  evidence real breakage or an unmet plan requirement.

You may read these files for context (read-only):
{files}

=== APPROVED PLAN ===
{plan}

=== DIFF UNDER REVIEW ===
{diff}

Respond with ONLY a JSON object, no prose and no code fence:
{{"findings": [{{"severity": "blocking|major|minor|question", "claim": "...",
  "acceptance_criterion": "which plan item this violates, or null",
  "evidence": "file:line, a command, or a failing test",
  "validation_command": "how to confirm or refute this, or null",
  "confidence": "high|medium|low"}}],
 "unevaluable_criteria": ["plan items you could not evaluate"]}}"""


def build_consolidation_prompt(plan: str, diff: str, blinded: list[tuple[str, str]]) -> str:
    reports = "\n\n---\n\n".join(f"REVIEWER {lbl}:\n{text}" for lbl, text in blinded)
    return f"""You are consolidating findings from several independent reviewers of the same
change. You do not know which model produced which report; judge only content.

Your job is NOT to decide whether the change is good. It is to:
  1. Merge findings that are the same issue stated differently.
  2. Mark each finding `confirmed` if its evidence holds up against the diff,
     `refuted` if the diff shows it is wrong, or `unresolved` if you cannot tell
     from the plan and diff alone.
  3. Drop findings with no checkable evidence.
  4. Collect acceptance criteria no reviewer could evaluate.

Be strict. A finding is only `confirmed` when its cited evidence is visible in
the diff or plan. When reviewers disagree and you cannot settle it, that is
`unresolved` — say so rather than picking a side.

=== APPROVED PLAN ===
{plan}

=== DIFF UNDER REVIEW ===
{diff}

=== REVIEWER REPORTS ===
{reports}

Respond with ONLY a JSON object, no prose and no code fence:
{{"findings": [{{"severity": "blocking|major|minor|question", "claim": "...",
  "acceptance_criterion": "... or null", "evidence": "...",
  "validation_command": "... or null",
  "status": "confirmed|unresolved|refuted",
  "raised_by": ["A", "B"]}}],
 "unevaluable_criteria": ["..."]}}"""


# ── Parsing ───────────────────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def extract_json(raw: str) -> dict | None:
    """Pull a JSON object out of a model response, fenced or not."""
    text = raw.strip()
    candidates = [text]
    fence = _FENCE_RE.search(text)
    if fence:
        candidates.insert(0, fence.group(1))
    # Fall back to the outermost braces.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for c in candidates:
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _clean(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def parse_findings(raw: str, source: str = "") -> tuple[list[Finding], list[str]]:
    """Parse one reviewer's report. Findings without evidence are dropped."""
    obj = extract_json(raw)
    if not obj:
        return [], []

    findings: list[Finding] = []
    for item in obj.get("findings") or []:
        if not isinstance(item, dict):
            continue
        claim = _clean(item.get("claim"))
        evidence = _clean(item.get("evidence"))
        # The whole discipline of this tool: no evidence, no finding.
        if not claim or not evidence:
            continue
        severity = str(item.get("severity", "minor")).lower().strip()
        if severity not in SEVERITIES:
            severity = "minor"
        status = str(item.get("status", "unresolved")).lower().strip()
        if status not in ("confirmed", "unresolved", "refuted"):
            status = "unresolved"
        raised = item.get("raised_by") or ([source] if source else [])
        findings.append(
            Finding(
                severity=severity,
                claim=claim,
                evidence=evidence,
                acceptance_criterion=_clean(item.get("acceptance_criterion")),
                validation_command=_clean(item.get("validation_command")),
                confidence=str(item.get("confidence", "medium")).lower().strip(),
                status=status,
                raised_by=[str(r) for r in raised],
            )
        )

    unevaluable = [s for s in (_clean(x) for x in obj.get("unevaluable_criteria") or []) if s]
    return findings, unevaluable


# ── Verdict ───────────────────────────────────────────────────────────────────


def derive_verdict(findings: list[Finding], unevaluable: list[str]) -> tuple[str, dict[str, int]]:
    """Mechanically derive pass/fail/unclear. No judge gets a vote here.

    - a confirmed blocking finding  -> fail
    - a blocking finding nobody could settle, or an unevaluable criterion -> unclear
    - otherwise -> pass
    """
    live = [f for f in findings if f.status != "refuted"]
    counts = {sev: sum(1 for f in live if f.severity == sev) for sev in SEVERITIES}

    if any(f.severity == "blocking" and f.status == "confirmed" for f in live):
        return "fail", counts
    if any(f.severity == "blocking" and f.status == "unresolved" for f in live):
        return "unclear", counts
    if unevaluable:
        return "unclear", counts
    return "pass", counts


# ── Orchestration ─────────────────────────────────────────────────────────────


def make_verify_sessions(
    reviewer_keys: list[str], consolidator_key: str, isolation: IsolationMode = "readonly"
) -> tuple[dict[str, Session], Session]:
    """Reviewers plus a consolidator, each with structured output requested where
    the provider supports it. The consolidator gets its own session even when it
    shares a provider with a reviewer, so the two can never resume each other."""
    reviewers = {
        k: make_session(k, k, isolation=isolation, output_schema=FINDINGS_SCHEMA)
        for k in reviewer_keys
    }
    consolidator = make_session(
        "consolidator",
        consolidator_key,
        model="opus" if consolidator_key == "claude" else None,
        isolation=isolation,
        output_schema=CONSOLIDATION_SCHEMA,
    )
    return reviewers, consolidator


def _schema_writer(stack: list[Path]):
    """Materialize a schema to a temp file for providers that want a path."""

    def write(schema: dict) -> str:
        fd, name = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(schema, f)
        stack.append(Path(name))
        return name

    return write


async def run_verify(
    plan: str,
    diff: str,
    reviewers: dict[str, Session],
    consolidator: Session,
    consolidator_name: str,
    names: dict[str, str],
    verbose: bool,
    timeout: float,
    seed: int | None = None,
    manifest: list[str] | None = None,
) -> VerifyResult:
    manifest = manifest if manifest is not None else build_manifest(diff)
    temp_files: list[Path] = []
    writer = _schema_writer(temp_files)

    try:
        display.print_section("independent review")
        prompt = build_review_prompt(plan, diff, manifest)
        reports = await stream_parallel(
            list(reviewers.values()),
            dict.fromkeys(reviewers, prompt),
            verbose,
            names,
            timeout,
            schema_path_writer=writer,
        )
        if not reports:
            raise ModelCallError("Every reviewer failed; cannot produce a verdict.")

        # Blind reviewer identity before consolidation, for the same reason the
        # debate judge is blinded: a model should not know it is grading itself.
        blinded, blinding = blind(reports, seed=seed)

        display.print_section("consolidation")
        display.print_note(f"{consolidator_name} is merging and checking findings (blinded)...")

        def on_cmd(key, cmd):
            if verbose:
                display.print_verbose_cmd(cmd)

        resp = await call_session(
            consolidator,
            build_consolidation_prompt(plan, diff, blinded),
            on_cmd=on_cmd,
            timeout=timeout,
            schema_path_writer=writer,
        )
        if verbose:
            display.print_timing(consolidator_name, resp.duration)

        findings, unevaluable = parse_findings(resp.content)

        if not findings and not unevaluable:
            # Consolidation produced nothing usable. Fall back to the raw reviewer
            # reports rather than silently declaring a pass.
            for key, text in reports:
                f, u = parse_findings(text, source=key)
                findings.extend(f)
                unevaluable.extend(u)

        status, counts = derive_verdict(findings, unevaluable)
        order = {sev: i for i, sev in enumerate(SEVERITIES)}
        findings.sort(key=lambda f: (order.get(f.severity, 99), f.status != "confirmed"))

        return VerifyResult(
            status=status,
            findings=findings,
            unevaluable=sorted(set(unevaluable)),
            counts=counts,
            blinding=blinding,
            reviewers=[k for k, _ in reports],
        )
    finally:
        for p in temp_files:
            p.unlink(missing_ok=True)


def render(result: VerifyResult, names: dict[str, str]) -> None:
    display.print_verdict_banner(result.status, result.counts)

    live = [f for f in result.findings if f.status != "refuted"]
    if live:
        display.print_section("findings")
        for i, f in enumerate(live, 1):
            display.print_finding(i, f.as_dict())

    if result.unevaluable:
        display.print_section("could not evaluate")
        for c in result.unevaluable:
            display.print(f"  · {c}")
        display.print()

    if not live and not result.unevaluable:
        display.print_note("No evidence-backed findings. The change satisfies the plan.")
        display.print()


async def _amain() -> int:  # pragma: no cover - convenience for manual runs
    raise SystemExit("Use `arbiter verify`.")


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_amain())
