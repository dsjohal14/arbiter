"""A scoped writer for providers that want their JSON Schema as a file path.

Codex takes `--output-schema <FILE>` rather than inline JSON, so a schema has to
be materialized before the call and removed after. Every code path that may talk
to codex needs one; `arbiter debate --judge codex` crashed outright because the
judge call site did not have one.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def schema_writer() -> Iterator:
    """Yield a `write(schema) -> path` callable; clean up every file on exit."""
    written: list[Path] = []

    def write(schema: dict) -> str:
        fd, name = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(schema, f)
        written.append(Path(name))
        return name

    try:
        yield write
    finally:
        for path in written:
            path.unlink(missing_ok=True)
