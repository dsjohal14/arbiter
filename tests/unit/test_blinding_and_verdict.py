from __future__ import annotations

from arbiter.prompts import blind, build_judge_prompt
from arbiter.verify import Finding, derive_verdict, parse_findings

ANSWERS = [("claude", "alpha"), ("codex", "beta"), ("gemini", "gamma")]


# ── Blinding ─────────────────────────────────────────────────────────────────


def test_labels_are_sequential_and_content_is_preserved():
    blinded, b = blind(ANSWERS, seed=7)
    assert [lbl for lbl, _ in blinded] == ["A", "B", "C"]
    assert {text for _, text in blinded} == {"alpha", "beta", "gamma"}
    assert set(b.label_to_model.values()) == {"claude", "codex", "gemini"}


def test_same_seed_reproduces_the_same_arrangement():
    """Blinding and reproducibility pull in opposite directions; the persisted
    seed is what reconciles them."""
    first, b1 = blind(ANSWERS, seed=1234)
    second, b2 = blind(ANSWERS, seed=1234)
    assert first == second
    assert b1.label_to_model == b2.label_to_model


def test_mapping_reverses_the_blinding():
    blinded, b = blind(ANSWERS, seed=99)
    for label, text in blinded:
        model = b.resolve(label)
        assert (model, text) in ANSWERS


def test_resolve_is_case_insensitive_and_handles_none():
    _, b = blind(ANSWERS, seed=3)
    assert b.resolve("a") == b.resolve("A")
    assert b.resolve(None) is None
    assert b.resolve("Z") is None


def test_judge_prompt_leaks_no_vendor_name():
    """The judge used to be shown 'Answer from claude [CLAUDE]', which let an
    Anthropic model recognise its own answer."""
    blinded, _ = blind(ANSWERS, seed=5)
    prompt = build_judge_prompt("Which sorting algorithm?", blinded)
    lowered = prompt.lower()
    for vendor in ("claude", "codex", "gemini", "anthropic", "openai", "google"):
        assert vendor not in lowered


def test_blinding_round_trips_through_its_serialised_form():
    _, b = blind(ANSWERS, seed=42)
    d = b.as_dict()
    assert d["seed"] == 42
    assert d["label_to_model"] == b.label_to_model


# ── Verdict derivation ───────────────────────────────────────────────────────


def _f(severity, status="confirmed"):
    return Finding(severity=severity, claim="c", evidence="f.py:1", status=status)


def test_no_findings_passes():
    status, counts = derive_verdict([], [])
    assert status == "pass"
    assert counts["blocking"] == 0


def test_confirmed_blocking_fails():
    status, _ = derive_verdict([_f("blocking")], [])
    assert status == "fail"


def test_unresolved_blocking_is_unclear_not_a_pass():
    """Reviewers raised something serious that nobody could settle. Failing
    would be unfair; passing would be a lie."""
    status, _ = derive_verdict([_f("blocking", "unresolved")], [])
    assert status == "unclear"


def test_refuted_blocking_does_not_fail_the_gate():
    status, counts = derive_verdict([_f("blocking", "refuted")], [])
    assert status == "pass"
    assert counts["blocking"] == 0


def test_unevaluable_criteria_make_the_result_unclear():
    status, _ = derive_verdict([], ["Criterion 3: end-to-end test"])
    assert status == "unclear"


def test_non_blocking_findings_still_pass():
    status, counts = derive_verdict([_f("major"), _f("minor")], [])
    assert status == "pass"
    assert counts["major"] == 1 and counts["minor"] == 1


def test_confirmed_blocking_outranks_unevaluable():
    status, _ = derive_verdict([_f("blocking")], ["something"])
    assert status == "fail"


# ── Finding parsing ──────────────────────────────────────────────────────────


def test_findings_without_evidence_are_dropped():
    """The whole discipline of the tool: an unevidenced claim is not a finding."""
    raw = """{"findings": [
        {"severity": "blocking", "claim": "It is broken", "evidence": "", "confidence": "high"},
        {"severity": "blocking", "claim": "Off by one", "evidence": "a.py:12", "confidence": "high"}
    ], "unevaluable_criteria": []}"""
    findings, _ = parse_findings(raw)
    assert len(findings) == 1
    assert findings[0].claim == "Off by one"


def test_findings_parse_from_a_fenced_block():
    raw = '```json\n{"findings": [], "unevaluable_criteria": ["c1"]}\n```'
    findings, unevaluable = parse_findings(raw)
    assert findings == []
    assert unevaluable == ["c1"]


def test_findings_parse_when_wrapped_in_prose():
    raw = 'Here is my review:\n{"findings": [], "unevaluable_criteria": []}\nHope that helps.'
    findings, unevaluable = parse_findings(raw)
    assert (findings, unevaluable) == ([], [])


def test_unparseable_report_yields_nothing_rather_than_raising():
    findings, unevaluable = parse_findings("I could not review this.")
    assert (findings, unevaluable) == ([], [])


def test_unknown_severity_degrades_to_minor_not_blocking():
    """A garbled severity must never be able to fail the gate by accident."""
    raw = """{"findings": [{"severity": "catastrophic", "claim": "x",
        "evidence": "a.py:1", "confidence": "high"}], "unevaluable_criteria": []}"""
    findings, _ = parse_findings(raw)
    assert findings[0].severity == "minor"


def test_source_is_recorded_when_given():
    raw = """{"findings": [{"severity": "major", "claim": "x",
        "evidence": "a.py:1", "confidence": "high"}], "unevaluable_criteria": []}"""
    findings, _ = parse_findings(raw, source="codex")
    assert findings[0].raised_by == ["codex"]
