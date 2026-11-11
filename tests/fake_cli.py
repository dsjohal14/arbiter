#!/usr/bin/env python3
"""A stand-in provider CLI that replays a scripted JSONL stream.

Why a real executable on PATH rather than mocking `create_subprocess_exec`:
the 64 KiB line-length limit that used to crash long answers lives inside
asyncio's real `StreamReader`. A mocked seam bypasses it entirely, so a test
against a mock would pass while production failed. Same for exit codes, SIGTERM
handling, and stderr backpressure — none of it exists in a mock.

Driven by environment variables so the shim scripts stay trivial:
    FAKE_CLI_NAME        which provider this shim impersonates
    FAKE_CLI_STREAM      path to a JSONL file to replay on stdout
    FAKE_CLI_STREAM_<N>  per-provider override (e.g. FAKE_CLI_STREAM_CODEX)
    FAKE_CLI_EXIT_CODE   exit with this code after replaying (default 0)
    FAKE_CLI_HANG        sleep this many seconds instead of exiting
    FAKE_CLI_GARBAGE     emit non-JSON noise between events
    FAKE_CLI_PROSE       write this to stdout before anything else, unterminated
                         (how a CLI needing a login asks, right before it blocks)
    FAKE_CLI_STDERR      write this to stderr
    FAKE_CLI_ARGV_OUT    append the received argv to this file, as JSON
    FAKE_CLI_PID_OUT     write this process's pid to this file
    FAKE_CLI_FAIL_FIRST  fail this many invocations (per provider) before behaving,
                         counted in FAKE_CLI_FAIL_STATE — a provider having a bad minute
"""

from __future__ import annotations

import json
import os
import sys
import time


def main() -> int:
    argv_out = os.environ.get("FAKE_CLI_ARGV_OUT")
    if argv_out:
        with open(argv_out, "a", encoding="utf-8") as f:
            f.write(json.dumps(sys.argv) + "\n")

    pid_out = os.environ.get("FAKE_CLI_PID_OUT")
    if pid_out:
        with open(pid_out, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))

    fail_first = int(os.environ.get("FAKE_CLI_FAIL_FIRST", "0"))
    if fail_first:
        state = f"{os.environ['FAKE_CLI_FAIL_STATE']}.{os.environ.get('FAKE_CLI_NAME', 'cli')}"
        seen = 0
        if os.path.exists(state):
            with open(state, encoding="utf-8") as f:
                seen = int(f.read())
        with open(state, "w", encoding="utf-8") as f:
            f.write(str(seen + 1))
        if seen < fail_first:
            sys.stderr.write("upstream overloaded, try again")
            return 1

    stderr = os.environ.get("FAKE_CLI_STDERR")
    if stderr:
        sys.stderr.write(stderr)
        sys.stderr.flush()

    prose = os.environ.get("FAKE_CLI_PROSE")
    if prose:
        # Deliberately unterminated: a CLI asking a question ends with ": ", not
        # a newline, which is exactly why a line-based reader never sees it.
        sys.stdout.write(prose)
        sys.stdout.flush()

    hang = os.environ.get("FAKE_CLI_HANG")
    if hang:
        # Outlive any sane test timeout so the runner must terminate us.
        time.sleep(float(hang))
        return 0

    # Each shim exports FAKE_CLI_NAME so one harness can give claude, codex, and
    # gemini different (correctly-shaped) replies in the same test.
    name = os.environ.get("FAKE_CLI_NAME", "")
    stream = os.environ.get(f"FAKE_CLI_STREAM_{name.upper()}") or os.environ.get("FAKE_CLI_STREAM")
    if stream and os.path.exists(stream):
        garbage = bool(os.environ.get("FAKE_CLI_GARBAGE"))
        with open(stream, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                if garbage:
                    sys.stdout.write("this line is not JSON\n")
                sys.stdout.write(line if line.endswith("\n") else line + "\n")
                sys.stdout.flush()

    return int(os.environ.get("FAKE_CLI_EXIT_CODE", "0"))


if __name__ == "__main__":
    sys.exit(main())
