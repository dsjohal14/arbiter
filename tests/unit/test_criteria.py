from __future__ import annotations

import pytest

from arbiter.criteria import (
    CriteriaError,
    Criterion,
    is_pinned,
    number,
    pin_into,
    read_criteria,
)

PLAN = "# Plan\n\nMake the totals correct.\n"


def test_unpinned_plan_refuses_with_instructions():
    """Silently inferring criteria would make the verdict non-reproducible, so an
    unpinned plan is an error that tells you the next command to run."""
    assert not is_pinned(PLAN)
    with pytest.raises(CriteriaError, match="arbiter plan pin"):
        read_criteria(PLAN)


def test_pin_then_read_round_trips():
    criteria = number(["total() sums every item", "empty list still returns 0"])
    pinned = pin_into(PLAN, criteria)
    assert is_pinned(pinned)
    assert read_criteria(pinned) == criteria


def test_pinning_preserves_the_rest_of_the_plan():
    pinned = pin_into(PLAN, number(["a"]))
    assert "Make the totals correct." in pinned
    assert pinned.startswith("# Plan")


def test_repinning_replaces_rather_than_appends():
    once = pin_into(PLAN, number(["first"]))
    twice = pin_into(once, number(["second", "third"]))
    assert twice.count("<!-- arbiter:criteria -->") == 1
    assert [c.text for c in read_criteria(twice)] == ["second", "third"]


def test_ids_are_stable_and_sequential():
    assert [c.id for c in number(["a", "b", "c"])] == ["AC-1", "AC-2", "AC-3"]


def test_blank_criteria_are_dropped():
    assert number(["a", "   ", ""]) == [Criterion("AC-1", "a")]


@pytest.mark.parametrize(
    "line",
    [
        "- **AC-1** — em dash",
        "- AC-1 - hyphen",
        "* **AC-1**: colon",
        "-   AC-1 \u2013 en dash",  # models really do emit these
    ],
)
def test_hand_edited_separators_still_parse(line):
    """People will edit these by hand; don't be brittle about the separator."""
    plan = f"<!-- arbiter:criteria -->\n{line}\n<!-- /arbiter:criteria -->"
    assert read_criteria(plan)[0].id == "AC-1"


def test_empty_block_is_an_error_not_an_empty_pass():
    plan = "<!-- arbiter:criteria -->\n\n<!-- /arbiter:criteria -->"
    with pytest.raises(CriteriaError, match="empty"):
        read_criteria(plan)


def test_duplicate_ids_are_rejected():
    plan = "<!-- arbiter:criteria -->\n- AC-1 - a\n- AC-1 - b\n<!-- /arbiter:criteria -->"
    with pytest.raises(CriteriaError, match="Duplicate"):
        read_criteria(plan)


def test_criteria_outside_the_block_are_ignored():
    """Only the pinned block counts, so prose that happens to look like a
    criterion cannot quietly enter the contract."""
    plan = "- AC-9 - not pinned\n\n<!-- arbiter:criteria -->\n- AC-1 - real\n<!-- /arbiter:criteria -->"
    assert [c.id for c in read_criteria(plan)] == ["AC-1"]
