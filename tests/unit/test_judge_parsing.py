"""Regression tests for the judge parser.

Every case below except the happy paths was a real, reproduced failure of the
original `str.find` + substring-scan implementation.
"""

from __future__ import annotations

import pytest

from arbiter.debate import parse_judge_response

LABELS = ["A", "B", "C"]


def test_plain_format():
    raw = "WINNER: B\n\nREASON: Tighter reasoning.\n\nBEST ANSWER:\nUse a heap."
    winner, reason, best = parse_judge_response(raw, LABELS)
    assert winner == "B"
    assert reason == "Tighter reasoning."
    assert best == "Use a heap."


def test_markdown_bold_markers():
    raw = "**WINNER:** C\n\n**REASON:** Clearest.\n\n**BEST ANSWER:**\nUse a heap."
    winner, _, best = parse_judge_response(raw, LABELS)
    assert winner == "C"
    assert best == "Use a heap."


def test_title_case_markers_still_parse():
    """`raw.find("WINNER:")` was case-sensitive, so this used to yield an empty
    winner and dump the whole raw response into `best_answer`."""
    raw = "Winner: A\n\nReason: Most complete.\n\nBest Answer:\nUse a heap."
    winner, reason, best = parse_judge_response(raw, LABELS)
    assert winner == "A"
    assert reason == "Most complete."
    assert best == "Use a heap."


def test_echoed_template_does_not_become_the_verdict():
    """THE wrong-winner bug.

    The old `next(k for k in valid_keys if k in winner_raw)` substring-scanned
    the label list, so an arbiter restating the format template silently
    produced the first label in list order regardless of its actual verdict.
    """
    raw = (
        "I'll use the requested format.\n\n"
        "WINNER: <one of: A | B | C>\n\n"
        "Now my actual verdict.\n\n"
        "WINNER: C\n\n"
        "REASON: It handles the edge case.\n\n"
        "BEST ANSWER:\nThe real answer."
    )
    winner, _, best = parse_judge_response(raw, LABELS)
    assert winner == "C"
    assert best == "The real answer."


def test_marker_inside_prose_does_not_truncate_the_answer():
    """Slicing to the *next* marker used to cut the primary output to a fragment
    whenever the arbiter wrote the marker text inside its own reasoning."""
    raw = (
        "WINNER: B\n\n"
        "REASON: A skipped the base case, so the BEST ANSWER: label belongs to B.\n\n"
        "BEST ANSWER:\nStep 1. Do the thing.\nStep 2. Do the other thing."
    )
    winner, _, best = parse_judge_response(raw, LABELS)
    assert winner == "B"
    assert "Step 1." in best and "Step 2." in best


def test_answer_prefix_is_stripped():
    raw = "WINNER: Answer B\n\nREASON: x\n\nBEST ANSWER:\ny"
    winner, _, _ = parse_judge_response(raw, LABELS)
    assert winner == "B"


def test_prose_mentioning_other_labels_does_not_confuse_the_winner():
    raw = (
        "WINNER: A\n\n"
        "REASON: B and C were close, and C nearly won, but A was strongest.\n\n"
        "BEST ANSWER:\nfinal"
    )
    winner, _, _ = parse_judge_response(raw, LABELS)
    assert winner == "A"


def test_tie_reports_no_winner_rather_than_guessing():
    raw = "WINNER: none - it is a tie\n\nREASON: Equally good.\n\nBEST ANSWER:\nEither."
    winner, _, best = parse_judge_response(raw, LABELS)
    assert winner is None
    assert best == "Either."


def test_unknown_label_reports_no_winner():
    raw = "WINNER: Z\n\nREASON: x\n\nBEST ANSWER:\ny"
    winner, _, _ = parse_judge_response(raw, LABELS)
    assert winner is None


def test_missing_markers_falls_back_to_whole_response():
    raw = "I think the second one is better because it is shorter."
    winner, reason, best = parse_judge_response(raw, LABELS)
    assert winner is None
    assert reason == "No reason provided."
    assert best == raw


@pytest.mark.parametrize(
    "raw",
    [
        '{"winner": "B", "reason": "Tighter.", "best_answer": "Use a heap."}',
        '```json\n{"winner": "B", "reason": "Tighter.", "best_answer": "Use a heap."}\n```',
    ],
)
def test_structured_output(raw):
    winner, reason, best = parse_judge_response(raw, LABELS)
    assert winner == "B"
    assert reason == "Tighter."
    assert best == "Use a heap."


def test_structured_output_with_invalid_winner():
    raw = '{"winner": "Q", "reason": "r", "best_answer": "b"}'
    winner, _, best = parse_judge_response(raw, LABELS)
    assert winner is None
    assert best == "b"


def test_multiline_best_answer_is_preserved_verbatim():
    body = "line one\n\n  indented\n\n```py\nprint(1)\n```"
    raw = f"WINNER: A\n\nREASON: r\n\nBEST ANSWER:\n{body}"
    _, _, best = parse_judge_response(raw, LABELS)
    assert best == body
