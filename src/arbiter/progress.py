"""What a run is doing right now, for whoever is watching.

A verification is a few minutes of waiting on other programs. The reviewers'
own output is structured JSON meant for a parser, so streaming it at a person
tells them nothing they can use. What they want to know is which step the run
is on, whether it is still alive, and how long it has been.

Two renderers sit behind one small interface, and the orchestration never asks
which it has:

  LiveProgress   a status board redrawn in place, for a terminal
  PlainProgress  one line per finished step, for a pipe or a CI log

`for_terminal()` picks. Nothing here decides anything about a verdict.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import TextIO

from . import display
from .models import Usage

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_FRAME_SECONDS = 0.1

WAITING, RUNNING, DONE, FAILED, SKIPPED = "waiting", "running", "done", "failed", "skipped"

# Column widths on the board: who, and how long.
_LABEL = 24
_TOOK = 6


def clock(seconds: float) -> str:
    """`0:42`, or `12:07` — a duration a person can read at a glance."""
    whole = max(0, int(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


def took_for(seconds: float) -> str:
    """How long a finished step took: `1.3s` under a minute, `2:07` over."""
    return f"{seconds:.1f}s" if seconds < 60 else clock(seconds)


def brief(usage: Usage | None) -> str:
    """One call's cost in a few characters, from whatever the provider reported."""
    if usage is None:
        return ""
    parts = []
    if usage.input_tokens is not None:
        parts.append(f"{_thousands(usage.input_tokens)} in")
    if usage.output_tokens is not None:
        parts.append(f"{_thousands(usage.output_tokens)} out")
    if usage.cost_usd is not None:
        parts.append(f"${usage.cost_usd:.4f}")
    return " · ".join(parts)


def _thousands(count: int) -> str:
    return f"{count / 1000:.1f}k" if count >= 1000 else str(count)


@dataclass
class _Row:
    key: str
    label: str
    state: str = WAITING
    activity: str = "waiting"
    started: float | None = None
    seconds: float | None = None
    extra: str = ""


class Progress:
    """The interface. Every method is safe to call from a worker thread."""

    def add(self, key: str, label: str) -> None:
        """Announce a step that will happen, so the board shows what is coming."""

    def start(self, key: str, activity: str) -> None: ...

    def update(self, key: str, activity: str) -> None: ...

    def done(
        self, key: str, summary: str, seconds: float | None = None, usage: Usage | None = None
    ) -> None: ...

    def fail(self, key: str, error: str) -> None: ...

    def skip(self, key: str, why: str) -> None: ...

    def retry(self, key: str, attempt: int, error: str) -> None: ...

    def close(self) -> None: ...

    def __enter__(self) -> Progress:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class _Rows(Progress):
    """Shared bookkeeping: which steps exist and what state each is in."""

    def __init__(self) -> None:
        self._rows: dict[str, _Row] = {}
        self._lock = threading.Lock()

    def _row(self, key: str) -> _Row:
        return self._rows.setdefault(key, _Row(key, key))

    def add(self, key: str, label: str) -> None:
        with self._lock:
            self._row(key).label = label

    def start(self, key: str, activity: str) -> None:
        with self._lock:
            row = self._row(key)
            row.state, row.activity, row.started, row.seconds = (
                RUNNING,
                activity,
                time.monotonic(),
                None,
            )

    def update(self, key: str, activity: str) -> None:
        with self._lock:
            self._row(key).activity = activity

    def _finish(
        self, key: str, state: str, activity: str, seconds: float | None, extra: str
    ) -> _Row:
        with self._lock:
            row = self._row(key)
            if seconds is None and row.started is not None:
                seconds = time.monotonic() - row.started
            row.state, row.activity, row.seconds, row.extra = state, activity, seconds, extra
            return row


class PlainProgress(_Rows):
    """One line when a step ends. Nothing while it runs: a log has no use for
    a spinner, and a line per heartbeat would bury the lines that matter."""

    def done(
        self, key: str, summary: str, seconds: float | None = None, usage: Usage | None = None
    ) -> None:
        row = self._finish(key, DONE, summary, seconds, brief(usage))
        took = f" in {row.seconds:.1f}s" if row.seconds is not None else ""
        extra = f" · {row.extra}" if row.extra else ""
        display.print(
            f"  {display._c('✓', display.BOLD, display.GREEN)} {row.label} {summary}{took}"
            f"{display._c(extra, display.DIM)}"
        )

    def fail(self, key: str, error: str) -> None:
        row = self._finish(key, FAILED, error, None, "")
        display.print_error(f"{row.label} failed: {error}")

    def skip(self, key: str, why: str) -> None:
        row = self._finish(key, SKIPPED, why, None, "")
        display.print_note(f"· {row.label} skipped: {why}")

    def retry(self, key: str, attempt: int, error: str) -> None:
        display.print_retry(self._row(key).label, attempt, error)


@dataclass
class _Screen:
    """Where the board is drawn, and how much of it is currently on screen."""

    stream: TextIO
    drawn: int = 0
    pending: list[str] = field(default_factory=list)


class LiveProgress(_Rows):
    """A status board redrawn in place.

    The board owns the terminal while it is up: anything else printed in that
    time is lifted above it rather than through it, which is what
    `display.capture` is for. The cursor is hidden for the duration and always
    restored, including when the run is interrupted.
    """

    def __init__(self, stream: TextIO | None = None, *, animate: bool = True) -> None:
        super().__init__()
        self._screen = _Screen(stream or display.out())
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._frame = 0
        self._write("\033[?25l")  # hide the cursor
        # A run that dies without unwinding must still give the cursor back.
        atexit.register(self._restore_cursor)
        display.capture(self._above)
        if animate:
            with contextlib.suppress(RuntimeError):  # no running loop: draw on demand only
                self._task = asyncio.get_running_loop().create_task(self._animate())

    # ── events ───────────────────────────────────────────────────────────────

    def done(
        self, key: str, summary: str, seconds: float | None = None, usage: Usage | None = None
    ) -> None:
        self._finish(key, DONE, summary, seconds, brief(usage))

    def fail(self, key: str, error: str) -> None:
        self._finish(key, FAILED, error, None, "")

    def skip(self, key: str, why: str) -> None:
        self._finish(key, SKIPPED, f"skipped — {why}", None, "")

    def retry(self, key: str, attempt: int, error: str) -> None:
        with self._lock:
            row = self._row(key)
            row.activity = f"retrying after attempt {attempt}: {error}"
            row.started = time.monotonic()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task is not None:
            self._task.cancel()
        display.capture(None)
        self.draw()
        self._restore_cursor()

    def _restore_cursor(self) -> None:
        self._write("\033[?25h")
        atexit.unregister(self._restore_cursor)

    # ── drawing ──────────────────────────────────────────────────────────────

    async def _animate(self) -> None:
        while True:
            self.draw()
            await asyncio.sleep(_FRAME_SECONDS)

    def _write(self, text: str) -> None:
        with contextlib.suppress(OSError, ValueError):
            self._screen.stream.write(text)
            self._screen.stream.flush()

    def _above(self, text: str) -> None:
        """Queue a line to appear above the board on the next frame."""
        with self._lock:
            self._screen.pending.extend(text.split("\n"))
        if self._task is None:
            self.draw()

    def draw(self) -> None:
        with self._lock:
            lines = [self._line(row) for row in self._rows.values()]
            notes, self._screen.pending = self._screen.pending, []
            out = []
            if self._screen.drawn:
                out.append(f"\033[{self._screen.drawn}A")  # back to the top of the board
            out.append("\r\033[J")  # and clear from there down
            out.extend(f"{note}\n" for note in notes)
            out.extend(f"{line}\n" for line in lines)
            self._screen.drawn = len(lines)
            self._frame += 1
        self._write("".join(out))

    def _line(self, row: _Row) -> str:
        """One row: who, how long, and what — in that order, so the first two
        columns line up and the description gets whatever width is left.

        Cut to the terminal's width before any colour goes on: a wrapped line
        would throw off the count of lines to redraw.
        """
        width = shutil.get_terminal_size((100, 24)).columns - 1
        if row.state == RUNNING:
            glyph, colour = _SPINNER[self._frame % len(_SPINNER)], display.CYAN
            took = clock(time.monotonic() - (row.started or time.monotonic()))
        else:
            glyph, colour = {
                DONE: ("✓", display.GREEN),
                FAILED: ("✗", display.RED),
                SKIPPED: ("·", display.DIM),
            }.get(row.state, ("○", display.DIM))
            took = "" if row.seconds is None else took_for(row.seconds)

        # An error message can span lines. A row that does would be redrawn
        # as if it were one, leaving the rest of it on screen for good.
        activity, label = " ".join(row.activity.split()), " ".join(row.label.split())
        what = f"{activity} · {row.extra}" if row.extra else activity
        text = f"{label[:_LABEL]:<{_LABEL}} {took:>{_TOOK}}  {what}"
        room = max(0, width - 4)
        if len(text) > room:
            text = text[: max(0, room - 1)] + "…"

        if row.state == FAILED:
            text = display._c(text, display.RED)
        elif row.state in (WAITING, SKIPPED):
            text = display._c(text, display.DIM)
        return f"  {display._c(glyph, display.BOLD, colour)} {text}"


def for_terminal() -> Progress:
    """A live board where one can be drawn, plain lines everywhere else.

    A pipe, a CI log, and a terminal that says it cannot move the cursor all
    get lines. So does an environment that sets `CI`, whatever it claims to be.
    """
    stream = display.out()
    live = (
        hasattr(stream, "isatty")
        and stream.isatty()
        and os.environ.get("TERM", "") != "dumb"
        and not os.environ.get("CI")
        and (os.name != "nt" or bool(os.environ.get("WT_SESSION")))
    )
    return LiveProgress(stream) if live else PlainProgress()
