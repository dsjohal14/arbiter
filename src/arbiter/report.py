"""A run's result as markdown, for a pull request's job summary or a comment.

Most of what goes into this report was written by a model reading a diff, and
some of it is quoted straight from the code and test output under review. It is
then rendered somewhere people trust — next to a merge button. So every piece
of it is made inert on the way in:

  - prose is escaped, so it cannot open a heading, a link, an image, or a
    tag, and cannot notify anyone by naming them
  - citations and output go in code, delimited by more backticks than they
    contain, so they cannot end their own block and continue as markup
  - nothing is rendered as raw HTML except the few tags written here

Like `display`, this decides nothing. It renders a result dict.
"""

from __future__ import annotations

import re

from . import __version__, usage

_GLYPH = {"met": "✓", "untested": "~", "unmet": "✗", "unverifiable": "?"}
_VERDICT_GLYPH = {"pass": "✓", "fail": "✗", "unclear": "?"}
_MARK = {True: "✓", False: "✗", None: "?"}
_SOURCE = {
    "plan_selected": "selected in plan; relevance human-assessed",
    "plan_selected_changed": "plan-selected test changed in this diff",
    "model_assessed": "relevance model-assessed",
}

_SPECIAL = re.compile(r"([\\`*_\[\]|#~])")


def _prose(value: object) -> str:
    """Text a model wrote, made safe to sit in a paragraph or a table cell."""
    text = " ".join(str(value or "").split())
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = _SPECIAL.sub(r"\\\1", text)
    # An @name in a comment notifies that person. The empty comment keeps it
    # readable and stops it being one.
    return text.replace("@", "@<!-- -->")


def _fence(text: str, least: int) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(least, longest + 1)


def _code(value: object) -> str:
    """A citation, inline, in a span it cannot close early."""
    text = " ".join(str(value or "").split())
    fence = _fence(text, 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _block(value: object, indent: str = "  ") -> list[str]:
    """Quoted source or test output, in a block it cannot close early."""
    text = str(value or "").strip("\n")
    fence = _fence(text, 3)
    return [
        f"{indent}{fence}",
        *(f"{indent}{line}" for line in text.splitlines()),
        f"{indent}{fence}",
    ]


def _footer(spent: dict | None, seconds: float | None, reviewers: list[str]) -> str:
    parts = []
    if reviewers:
        parts.append("reviewers: " + ", ".join(_prose(r) for r in reviewers))
    if spent is not None:
        parts.append(usage.headline(spent, seconds))
    parts.append(f"arbiter {__version__}")
    return f"<sub>{' · '.join(parts)}</sub>"


def _evidence(row: dict) -> list[str]:
    lines: list[str] = []
    for label, citation_key, check_key in (
        ("code", "implementation_evidence", "impl_check"),
        ("test", "test_evidence", "test_check"),
    ):
        citation, check = row.get(citation_key), row.get(check_key) or {}
        if not citation:
            continue
        mark = _MARK.get(check.get("verified"), "·") if check else "·"
        note = ""
        if label == "test":
            note = f" — {_SOURCE.get(str(row.get('evidence_source')), _SOURCE['model_assessed'])}"
        lines.append(f"- **{label}** {_code(citation)} {mark}{note}")
        if check.get("detail"):
            lines += _block("\n".join(str(check["detail"]).splitlines()[:6]))

    baseline = row.get("baseline_check")
    if baseline:
        mark = _MARK.get(baseline.get("verified"), "?")
        lines.append(f"- **base** {mark} {_prose(baseline.get('detail'))}")
    if row.get("downgrade_reason"):
        lines.append(
            f"- downgraded from {_code(row.get('downgraded_from'))}: "
            f"{_prose(row['downgrade_reason'])}"
        )
    return lines


def verify_markdown(result: dict, spent: dict | None = None, seconds: float | None = None) -> str:
    """The verdict, a row per criterion, and the evidence behind each one."""
    status = str(result.get("status", "unclear"))
    counts = result.get("counts") or {}
    rows = [row for row in result.get("criteria") or [] if isinstance(row, dict)]

    out = [f"## {_VERDICT_GLYPH.get(status, '·')} Arbiter verdict: {status.upper()}", ""]
    out.append(
        " · ".join(
            f"**{counts.get(name, 0)}** {name}"
            for name in ("met", "untested", "unmet", "unverifiable")
        )
    )
    if failed := result.get("failed_reviewers"):
        out += [
            "",
            f"> ⚠ Reviewers that did not complete: {', '.join(_prose(r) for r in failed)}. "
            "The verdict cannot be a clean pass.",
        ]

    out += ["", "| | Criterion | Status | Why |", "|:-:|---|---|---|"]
    for row in rows:
        state = str(row.get("status", "unverifiable"))
        why = _prose(row.get("downgrade_reason") or "")
        out.append(
            f"| {_GLYPH.get(state, '·')} | **{_prose(row.get('id'))}** {_prose(row.get('text'))} "
            f"| {_code(state)} | {why} |"
        )

    for row in rows:
        state = str(row.get("status", "unverifiable"))
        out += ["", "<details>", f"<summary><b>{_prose(row.get('id'))}</b> — {state}</summary>", ""]
        if row.get("assessment"):
            out += [_prose(row["assessment"]), ""]
        out += _evidence(row) or ["- no evidence was cited"]
        out += ["", "</details>"]

    findings = [f for f in result.get("ungrounded_findings") or [] if isinstance(f, dict)]
    if findings:
        out += ["", "### Other findings", ""]
        for number, finding in enumerate(findings, 1):
            severity = _prose(str(finding.get("severity", "minor")).upper())
            out.append(
                f"{number}. **{severity}** {_prose(finding.get('claim'))} — "
                f"{_prose(finding.get('evidence'))}"
            )

    out += ["", _footer(spent, seconds, result.get("reviewers") or [])]
    return "\n".join(out) + "\n"


def plan_review_markdown(
    result: dict, spent: dict | None = None, seconds: float | None = None
) -> str:
    """Whether a plan is ready, and every proposed edit to its criteria."""
    findings = [f for f in result.get("findings") or [] if isinstance(f, dict)]
    blocking = sum(1 for f in findings if f.get("blocking"))
    ready = bool(result.get("ready"))
    title = "ready to pin" if ready else f"{blocking} blocking"

    out = [f"## {'✓' if ready else '!'} Arbiter plan review: {title}", ""]
    live = [(kind, n) for kind, n in (result.get("counts") or {}).items() if n]
    if live:
        out += [" · ".join(f"**{n}** {_prose(kind)}" for kind, n in live), ""]
    if failed := result.get("failed_reviewers"):
        out += [
            f"> ⚠ Reviewers without a complete review: {', '.join(_prose(r) for r in failed)}. "
            "The plan cannot be reported as ready.",
            "",
        ]

    if findings:
        out += ["### Proposed changes", ""]
        for number, finding in enumerate(findings, 1):
            flag = " — **blocking**" if finding.get("blocking") else ""
            target = f" {_code(finding['target'])}" if finding.get("target") else ""
            out.append(
                f"{number}. **{_prose(str(finding.get('action', '')).upper())}**{target} "
                f"({_prose(finding.get('kind'))}){flag}: {_prose(finding.get('problem'))}"
            )
            if finding.get("quote"):
                out.append(f"   - plan says: “{_prose(finding['quote'])}”")
            if finding.get("proposed_criterion"):
                out.append(f"   - criterion: {_prose(finding['proposed_criterion'])}")
            if finding.get("proposed_test"):
                out.append(f"   - test: {_code(finding['proposed_test'])}")
        out.append("")

    criteria = [c for c in result.get("criteria") or [] if isinstance(c, dict)]
    if criteria:
        out += ["### Criteria after these changes", ""]
        for criterion in criteria:
            proof = (
                f" — proof {_code(criterion['test_evidence'])}"
                if criterion.get("test_evidence")
                else ""
            )
            out.append(
                f"- **{_prose(criterion.get('id'))}** {_prose(criterion.get('text'))}{proof}"
            )
        out.append("")

    out.append(_footer(spent, seconds, result.get("reviewers") or []))
    return "\n".join(out) + "\n"
