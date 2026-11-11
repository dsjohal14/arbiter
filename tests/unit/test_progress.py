"""What a run shows while it is working.

Two renderers behind one interface: a board redrawn in place for a terminal,
and one line per finished step for a pipe or a CI log. The properties that
matter are that neither corrupts the other's output, that the terminal is
always handed back the way it was found, and that a log gets no escape codes.
"""

from __future__ import annotations

import io
import re
import threading

import pytest

from arbiter import display, progress
from arbiter.models import Usage
from arbiter.progress import LiveProgress, PlainProgress

HIDE_CURSOR, SHOW_CURSOR = "\033[?25l", "\033[?25h"


def _visible(text: str) -> str:
    """What a terminal would show: the text with its control sequences removed."""
    return re.sub(r"\033\[[0-9;?]*[A-Za-z]", "", text)


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def board():
    stream = Terminal()
    live = LiveProgress(stream, animate=False)
    yield live, stream
    live.close()


# ── Plain: one line per finished step ────────────────────────────────────────


def test_a_plain_run_prints_nothing_until_a_step_finishes(capsys):
    """A log has no use for a heartbeat, and a line per update would bury the
    lines that matter."""
    plain = PlainProgress()
    plain.add("claude", "claude (sonnet)")
    plain.start("claude", "reviewing")
    plain.update("claude", "reviewing · 1,204 characters back")

    assert capsys.readouterr().out == ""


def test_a_finished_step_is_one_line_with_its_cost(capsys):
    plain = PlainProgress()
    plain.add("claude", "claude (sonnet)")
    plain.start("claude", "reviewing")

    plain.done(
        "claude", "reviewed", 38.24, Usage(input_tokens=12840, output_tokens=612, cost_usd=0.0412)
    )

    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert "claude (sonnet) reviewed in 38.2s" in out
    assert "12.8k in · 612 out · $0.0412" in out


def test_a_skipped_step_says_why(capsys):
    plain = PlainProgress()
    plain.add("consolidation", "consolidation")
    plain.skip("consolidation", "one reviewer, so there is nothing to reconcile")

    assert "consolidation skipped: one reviewer" in capsys.readouterr().out


def test_a_failed_step_goes_to_stderr(capsys):
    plain = PlainProgress()
    plain.add("codex", "codex")
    plain.fail("codex", "codex exited 1: upstream overloaded")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "codex failed: codex exited 1: upstream overloaded" in captured.err


def test_a_plain_run_carries_no_escape_codes(capsys):
    plain = PlainProgress()
    plain.add("claude", "claude")
    plain.start("claude", "reviewing")
    plain.done("claude", "reviewed", 1.0)
    plain.skip("consolidation", "nothing to reconcile")
    plain.close()

    assert "\033" not in capsys.readouterr().out


# ── Live: a board redrawn in place ───────────────────────────────────────────


def test_the_cursor_is_hidden_while_the_board_is_up_and_then_restored():
    stream = Terminal()
    live = LiveProgress(stream, animate=False)
    assert stream.getvalue().startswith(HIDE_CURSOR)
    assert SHOW_CURSOR not in stream.getvalue()

    live.close()

    assert stream.getvalue().endswith(SHOW_CURSOR)


def test_closing_twice_is_harmless(board):
    live, stream = board
    live.close()
    written = stream.getvalue()
    live.close()
    assert stream.getvalue() == written


def test_each_frame_overwrites_the_one_before(board):
    """The board moves back up by exactly the lines it drew. Off by one and
    every frame scrolls the terminal."""
    live, stream = board
    live.add("claude", "claude (sonnet)")
    live.add("codex", "codex")
    live.start("claude", "reviewing")
    live.draw()
    stream.seek(0)
    stream.truncate()

    live.draw()

    assert stream.getvalue().startswith("\033[2A\r\033[J"), "two rows drawn, so two lines up"


def test_a_row_shows_who_how_long_and_what(board):
    live, stream = board
    live.add("claude", "claude (sonnet)")
    live.start("claude", "reviewing")
    live.done("claude", "reviewed", 83.0, Usage(input_tokens=900, cost_usd=0.01))
    live.draw()

    row = stream.getvalue().splitlines()[-1]
    assert "✓" in row
    assert "claude (sonnet)" in row
    assert "1:23" in row
    assert "reviewed · 900 in · $0.0100" in row


def test_a_step_that_has_not_started_is_shown_waiting(board):
    """The whole pipeline is on the board from the first frame, so it is clear
    what is still to come."""
    live, stream = board
    live.add("evidence", "evidence")
    live.draw()

    assert "○" in stream.getvalue() and "waiting" in stream.getvalue()


def test_a_row_longer_than_the_terminal_is_cut_not_wrapped(board, monkeypatch):
    """A wrapped row occupies two lines, so the next frame would move up too
    few and leave half a row behind."""
    monkeypatch.setattr(
        "arbiter.progress.shutil.get_terminal_size", lambda *_: progress.os.terminal_size((60, 24))
    )
    live, stream = board
    live.add("evidence", "evidence")
    live.start("evidence", "AC-12 (12/40) · " + "tests/a/very/deep/path/" * 6 + "test_x.py::test_y")
    live.draw()

    row = _visible(stream.getvalue().splitlines()[-1])
    assert len(row) <= 59
    assert row.endswith("…")


def test_output_printed_while_the_board_is_up_lands_above_it(board):
    """Anything written through the board would land in the middle of a frame.
    The board takes the line and places it above itself instead."""
    live, stream = board
    live.add("claude", "claude")
    live.start("claude", "reviewing")
    live.draw()

    display.print_note("1 of 2 reviewers failed.")
    live.draw()

    text = stream.getvalue()
    assert text.index("1 of 2 reviewers failed.") < text.rindex("reviewing")


def test_printing_goes_back_to_normal_once_the_board_is_closed(capsys):
    live = LiveProgress(Terminal(), animate=False)
    live.close()

    display.print("after the board")

    assert "after the board" in capsys.readouterr().out


def test_a_worker_thread_can_report_progress(board):
    """Evidence checks run on a thread, and say what they are doing from there."""
    live, stream = board
    live.add("evidence", "evidence")
    live.start("evidence", "reading cited lines")

    worker = threading.Thread(target=lambda: live.update("evidence", "AC-2 (2/5)"))
    worker.start()
    worker.join()
    live.draw()

    assert "AC-2 (2/5)" in stream.getvalue()


# ── Choosing between them ────────────────────────────────────────────────────


@pytest.fixture
def stdout_is(monkeypatch):
    def set_stream(stream):
        monkeypatch.setattr("arbiter.progress.display.out", lambda: stream)
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setenv("TERM", "xterm-256color")

    return set_stream


def test_a_terminal_gets_the_live_board(stdout_is):
    stdout_is(Terminal())
    chosen = progress.for_terminal()
    try:
        assert isinstance(chosen, LiveProgress)
    finally:
        chosen.close()


def test_a_pipe_gets_plain_lines(stdout_is):
    stdout_is(io.StringIO())
    assert isinstance(progress.for_terminal(), PlainProgress)


@pytest.mark.parametrize(("variable", "value"), [("CI", "true"), ("TERM", "dumb")])
def test_ci_and_a_dumb_terminal_get_plain_lines_whatever_they_claim(
    stdout_is, monkeypatch, variable, value
):
    stdout_is(Terminal())
    monkeypatch.setenv(variable, value)
    assert isinstance(progress.for_terminal(), PlainProgress)


# ── Small formats ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("seconds", "shown"), [(0, "0:00"), (42.9, "0:42"), (727, "12:07")])
def test_a_running_clock_is_minutes_and_seconds(seconds, shown):
    assert progress.clock(seconds) == shown


@pytest.mark.parametrize(("seconds", "shown"), [(1.34, "1.3s"), (59.94, "59.9s"), (127, "2:07")])
def test_a_finished_step_under_a_minute_is_shown_in_seconds(seconds, shown):
    assert progress.took_for(seconds) == shown


def test_usage_is_summarised_from_whatever_the_provider_reported():
    assert progress.brief(None) == ""
    assert progress.brief(Usage(input_tokens=15002, output_tokens=801)) == "15.0k in · 801 out"
    assert progress.brief(Usage(cost_usd=0.5)) == "$0.5000"


def test_an_error_that_spans_lines_still_occupies_one_row(board):
    """A provider's stderr is quoted in its failure. If a row took two lines the
    next frame would move up one too few, and half of it would stay on screen."""
    live, stream = board
    live.add("codex", "codex")
    live.fail("codex", "codex exited 1: Traceback (most recent call last):\n  File x\nBoom")
    live.draw()
    stream.seek(0)
    stream.truncate()

    live.draw()

    frame = stream.getvalue()
    assert frame.startswith("\033[1A\r\033[J"), "one row drawn, so one line up"
    assert frame.count("\n") == 1
    assert "Traceback (most recent call last): File x Boom" in _visible(frame)
