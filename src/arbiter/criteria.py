"""Acceptance criteria: extracted once, pinned into the plan, read thereafter.

Real plans are prose. If a model re-derived the criteria on every run, two runs
would enumerate different promises and the "mechanical" verdict would quietly
rest on a non-deterministic step — which is exactly the guarantee that separates
this from a review tool.

So extraction happens once, via an explicit `arbiter plan pin`, and the result is
written back into the plan between fences. From then on the criteria are read,
not inferred: enumeration is deterministic, and the list is diffable in git so a
change to what was promised shows up in review like any other change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

OPEN_FENCE = "<!-- arbiter:criteria -->"
CLOSE_FENCE = "<!-- /arbiter:criteria -->"

# Lenient on the separator so hand-edited plans keep parsing.
_CRITERION_RE = re.compile(
    r"^\s*[-*]\s*\**\s*(AC-\d+)\s*\**\s*[—–\-:]\s*(.+?)\s*$",  # noqa: RUF001 - en dash is a real separator here
    re.MULTILINE,
)
_BLOCK_RE = re.compile(
    re.escape(OPEN_FENCE) + r"(.*?)" + re.escape(CLOSE_FENCE),
    re.DOTALL,
)


# `[proof: tests/x.py::test_y]` at the end of a criterion line.
_PROOF_RE = re.compile(r"\s*[\[(]\s*proof:\s*`?(?P<node>[^`\]\)]+?)`?\s*[\]\)]\s*$", re.I)


@dataclass(frozen=True)
class Criterion:
    id: str
    text: str
    # A test the HUMAN says proves this criterion. When present it is used in
    # place of whatever the reviewer cites, which is the only way relevance
    # becomes a fact rather than a model's opinion.
    test_evidence: str | None = None

    def as_dict(self) -> dict:
        return {"id": self.id, "text": self.text, "test_evidence": self.test_evidence}


def split_proof(text: str) -> tuple[str, str | None]:
    """Separate a criterion's prose from any `[proof: ...]` annotation."""
    m = _PROOF_RE.search(text)
    if not m:
        return text.strip(), None
    return text[: m.start()].strip(), m.group("node").strip()


class CriteriaError(ValueError):
    """The plan has no usable acceptance criteria."""


def is_pinned(plan_text: str) -> bool:
    return _BLOCK_RE.search(plan_text) is not None


def read_criteria(plan_text: str) -> list[Criterion]:
    """Read the pinned criteria block. Raises if the plan has not been pinned."""
    block = _BLOCK_RE.search(plan_text)
    if not block:
        raise CriteriaError(
            "This plan has no pinned acceptance criteria.\n"
            "  Run:  arbiter plan pin <plan.md>\n"
            "  Then: arbiter verify --plan <plan.md> --diff"
        )
    criteria = []
    for m in _CRITERION_RE.finditer(block.group(1)):
        text, proof = split_proof(m.group(2))
        criteria.append(Criterion(id=m.group(1), text=text, test_evidence=proof))
    if not criteria:
        raise CriteriaError("The pinned criteria block is empty.")

    seen = set()
    for c in criteria:
        if c.id in seen:
            raise CriteriaError(f"Duplicate criterion id {c.id} in the pinned block.")
        seen.add(c.id)
    return criteria


def render_block(criteria: list[Criterion]) -> str:
    lines = [OPEN_FENCE, "", "## Acceptance criteria", ""]
    for c in criteria:
        proof = f"  [proof: {c.test_evidence}]" if c.test_evidence else ""
        lines.append(f"- **{c.id}** — {c.text}{proof}")
    lines += ["", CLOSE_FENCE]
    return "\n".join(lines)


def pin_into(plan_text: str, criteria: list[Criterion]) -> str:
    """Insert or replace the criteria block, leaving the rest of the plan alone."""
    block = render_block(criteria)
    if is_pinned(plan_text):
        return _BLOCK_RE.sub(lambda _: block, plan_text, count=1)
    sep = "" if plan_text.endswith("\n\n") else ("\n" if plan_text.endswith("\n") else "\n\n")
    return plan_text + sep + block + "\n"


def number(texts: list[str]) -> list[Criterion]:
    out: list[Criterion] = []
    for t in texts:
        if not t.strip():
            continue
        text, proof = split_proof(t)
        out.append(Criterion(id=f"AC-{len(out) + 1}", text=text, test_evidence=proof))
    return out


# ── Extraction ────────────────────────────────────────────────────────────────

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "criteria": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Each checkable promise the plan makes, one per entry.",
        }
    },
    "required": ["criteria"],
}


def build_extraction_prompt(plan_text: str) -> str:
    return f"""Extract the acceptance criteria from this implementation plan.

An acceptance criterion is a single, checkable promise the plan makes about the
finished state of the code — something a reviewer could later confirm or refute
by reading the diff, the source, or a test result.

RULES:
- One promise per criterion. Split compound statements.
- Write each as a testable assertion about the end state, not as a task.
  Good: "Unknown provider names exit with a usage error."
  Bad:  "Add validation to the CLI."
- Include implicit promises the plan clearly relies on, but do not invent
  requirements the plan never makes.
- Exclude background, rationale, and anything already true before this change.
- Keep the plan's own wording where you can, so the criteria stay recognisable.

=== PLAN ===
{plan_text}

Respond with ONLY a JSON object, no prose and no code fence:
{{"criteria": ["...", "..."]}}"""
