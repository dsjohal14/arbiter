"""Transport layer: driving Claude Code, Codex, and Gemini CLI as persistent sessions.

Each model runs as one *persistent* CLI session, streamed live. Session ids are
pre-assigned as UUIDs where the provider supports it, so `_build_cmd` is
deterministic in `(Session, prompt)` — which is what makes every flag decision
unit-testable, and what stops two sessions of the same provider from resuming
each other.

Providers differ in what they can be asked to do. Rather than pretend parity, the
`Provider` record states each one's capabilities and `_build_cmd` reads from it.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

# asyncio.StreamReader defaults to a 64 KiB line limit. Claude emits its entire
# final answer as ONE JSON line in the `result` event, so any answer longer than
# that raises "Separator is not found, and chunk exceed the limit" mid-stream.
STREAM_LIMIT = 8 * 1024 * 1024

DEFAULT_TIMEOUT = 300.0

# How long a child gets to exit after SIGTERM before we SIGKILL it.
_TERM_GRACE = 2.0

IsolationMode = Literal["readonly", "open"]


# ── Provider capabilities ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Provider:
    """What one CLI can be asked to do. Capabilities are not uniform across
    providers; encoding that here keeps the asymmetry honest and testable."""

    key: str
    cli: str
    default_model: str | None = None
    # Can the session id be chosen by us up front, rather than captured?
    preassigns_session: bool = False
    # Can the provider be asked to emit a response matching a JSON Schema?
    output_schema_flag: str | None = None
    # Does the schema arrive inline, or as a path to a file?
    output_schema_as_file: bool = False
    # Flags that put the agent into a read-only / no-write posture.
    readonly_flags: tuple[str, ...] = ()
    # Flags that stop the agent inheriting the user's ambient customizations
    # (CLAUDE.md, AGENTS.md, skills, hooks, MCP servers). Without these the
    # reviewers answer under different, undisclosed system prompts and the
    # comparison is not controlled.
    isolate_flags: tuple[str, ...] = ()


PROVIDERS: dict[str, Provider] = {
    "claude": Provider(
        key="claude",
        cli="claude",
        default_model="sonnet",
        preassigns_session=True,
        output_schema_flag="--json-schema",
        readonly_flags=("--permission-mode", "plan"),
        isolate_flags=("--strict-mcp-config",),
    ),
    "codex": Provider(
        key="codex",
        cli="codex",
        default_model=None,
        preassigns_session=False,  # codex mints its own thread_id
        output_schema_flag="--output-schema",
        output_schema_as_file=True,
        readonly_flags=("-s", "read-only"),
        isolate_flags=("--ignore-user-config", "--ephemeral"),
    ),
    "gemini": Provider(
        key="gemini",
        cli="gemini",
        default_model=None,  # let the CLI pick its current default; pinned ids rot
        preassigns_session=True,
        output_schema_flag=None,  # no structured-output support
        readonly_flags=("--approval-mode", "plan"),
        isolate_flags=(),  # no equivalent
    ),
}

VALID_MODELS = set(PROVIDERS)


# ── Session + response envelopes ─────────────────────────────────────────────


@dataclass
class Session:
    """One model's persistent CLI session across a whole run."""

    key: str  # logical role label: "claude" | "codex" | "gemini" | "arbiter"
    provider: Provider
    model: str | None = None
    session_id: str | None = None
    started: bool = False  # True once the first call has run
    isolation: IsolationMode = "readonly"
    output_schema: dict | None = None

    @property
    def cli(self) -> str:
        return self.provider.cli

    def label(self) -> str:
        """Human-facing name. Derived from configuration rather than hardcoded
        marketing names, which rot every time a provider ships a new default."""
        if self.model:
            return f"{self.provider.cli} ({self.model})"
        return self.provider.cli


@dataclass
class ModelResponse:
    model: str
    content: str
    session_id: str | None = None
    duration: float = 0.0
    cmd: list[str] = field(default_factory=list)


def make_session(
    role: str,
    provider_key: str,
    model: str | None = None,
    isolation: IsolationMode = "readonly",
    output_schema: dict | None = None,
) -> Session:
    provider = PROVIDERS[provider_key]
    sid = str(uuid.uuid4()) if provider.preassigns_session else None
    return Session(
        key=role,
        provider=provider,
        model=model if model is not None else provider.default_model,
        session_id=sid,
        isolation=isolation,
        output_schema=output_schema,
    )


def make_debater_sessions(
    keys: list[str], isolation: IsolationMode = "readonly"
) -> dict[str, Session]:
    return {k: make_session(k, k, isolation=isolation) for k in keys}


def make_arbiter_session(
    judge: str,
    isolation: IsolationMode = "readonly",
    output_schema: dict | None = None,
) -> Session:
    # The arbiter is the same provider in a different role, with its own session.
    model = "opus" if judge == "claude" else None
    return make_session(
        "arbiter", judge, model=model, isolation=isolation, output_schema=output_schema
    )


# ── Command construction ─────────────────────────────────────────────────────


def _schema_args(sess: Session, schema_path_writer: Callable[[dict], str] | None) -> list[str]:
    """Structured-output flags, when the provider supports them.

    A provider with no structured-output support (gemini) silently gets nothing —
    that asymmetry is expected and handled by the prose fallback. A provider that
    needs the schema on disk but was given no writer is a *caller* bug, and is
    raised rather than swallowed: dropping it would run the model unstructured
    with no signal that the requested schema never applied.
    """
    p = sess.provider
    if not sess.output_schema or not p.output_schema_flag:
        return []
    if p.output_schema_as_file:
        if schema_path_writer is None:
            raise ValueError(
                f"{p.cli} needs its schema written to a file, but no "
                f"schema_path_writer was supplied to call_session()."
            )
        return [p.output_schema_flag, schema_path_writer(sess.output_schema)]
    return [p.output_schema_flag, json.dumps(sess.output_schema)]


def _build_cmd(
    sess: Session,
    prompt: str,
    schema_path_writer: Callable[[dict], str] | None = None,
) -> list[str]:
    """Build the CLI invocation for `sess`.

    Deterministic in `(sess, prompt)`: the same session and prompt always produce
    the same command, which is what makes every flag decision unit-testable. The
    one impurity is deliberate and injected — `schema_path_writer` materializes a
    schema to disk for providers that take a path rather than inline JSON, and
    the caller owns that file's lifetime.

    Uses the resume form once the session has been started. Session ids are
    pre-assigned where the provider allows, so two sessions of the same provider
    can never resolve to each other.
    """
    p = sess.provider
    extra: list[str] = []
    if sess.isolation == "readonly":
        extra += list(p.readonly_flags) + list(p.isolate_flags)
    extra += _schema_args(sess, schema_path_writer)

    if p.cli == "claude":
        cmd = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--verbose",
        ]
        if sess.model:
            cmd += ["--model", sess.model]
        if sess.session_id:
            cmd += ["--resume" if sess.started else "--session-id", sess.session_id]
        return cmd + extra

    if p.cli == "codex":
        # codex mints its own thread id; we capture it and resume by it.
        if sess.started and sess.session_id:
            cmd = ["codex", "exec", "resume", sess.session_id, "--json", "--skip-git-repo-check"]
        else:
            cmd = ["codex", "exec", "--json", "--skip-git-repo-check"]
            if sess.model:
                cmd += ["-m", sess.model]
        return cmd + extra + ["--", prompt]

    if p.cli == "gemini":
        cmd = ["gemini", "-p", prompt, "--output-format", "stream-json"]
        if sess.model:
            cmd += ["-m", sess.model]
        if sess.session_id:
            cmd += ["--resume" if sess.started else "--session-id", sess.session_id]
        return cmd + extra

    raise ValueError(f"Unknown cli: {p.cli}")


# ── Streaming event parser ───────────────────────────────────────────────────


def _unwrap_provider_error(msg: object) -> str:
    """Pull the human-readable sentence out of a provider error.

    Providers nest the real cause inside a JSON-encoded string, sometimes twice.
    Left raw, a genuinely actionable message ("requires a newer version of
    Codex") is buried under transport noise the user can do nothing with.
    """
    text = str(msg or "").strip()
    for _ in range(3):
        if not text.startswith("{"):
            break
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            break
        if not isinstance(obj, dict):
            break
        nxt = obj.get("error") or obj.get("message")
        if isinstance(nxt, dict):
            nxt = nxt.get("message")
        if not nxt:
            break
        text = str(nxt).strip()
    return text or "provider reported an unspecified error"


def _parse_event(cli: str, obj: dict) -> list[tuple[str, str | None]]:
    """Map one JSON event to ('session' | 'delta' | 'done', payload) pairs.

    Shapes confirmed by probing each CLI, and pinned by fixture tests so a
    provider schema change shows up as a red test rather than an empty answer.

      claude: system/init -> session_id; stream_event content_block_delta -> text;
              result -> final text + session_id
      codex : thread.started -> thread_id; item.completed/agent_message -> text;
              turn.completed -> done
      gemini: init -> session_id; message(role=assistant) -> incremental content;
              result -> done
    """
    out: list[tuple[str, str | None]] = []
    t = obj.get("type")

    if cli == "claude":
        if t == "system" and obj.get("subtype") == "init":
            if obj.get("session_id"):
                out.append(("session", obj["session_id"]))
        elif t == "stream_event":
            ev = obj.get("event", {})
            if ev.get("type") == "content_block_delta":
                d = ev.get("delta", {})
                if d.get("type") == "text_delta":
                    out.append(("delta", d.get("text", "")))
        elif t == "result":
            if obj.get("is_error"):
                out.append(("error", _unwrap_provider_error(obj.get("result"))))
            else:
                out.append(("done", obj.get("result")))
            if obj.get("session_id"):
                out.append(("session", obj["session_id"]))

    elif cli == "codex":
        if t == "thread.started":
            if obj.get("thread_id"):
                out.append(("session", obj["thread_id"]))
        elif t == "item.completed":
            item = obj.get("item", {})
            # NOTE: codex emits the whole message at once here. It does not
            # stream incrementally, unlike claude and gemini.
            if item.get("type") == "agent_message":
                out.append(("delta", item.get("text", "")))
        elif t == "turn.completed":
            out.append(("done", None))
        elif t in ("error", "turn.failed"):
            msg = obj.get("message") or (obj.get("error") or {}).get("message")
            out.append(("error", _unwrap_provider_error(msg)))

    elif cli == "gemini":
        if t == "init":
            if obj.get("session_id"):
                out.append(("session", obj["session_id"]))
        elif t == "message" and obj.get("role") == "assistant":
            out.append(("delta", obj.get("content", "")))
        elif t == "result":
            out.append(("done", None))
        elif t == "error":
            out.append(("error", _unwrap_provider_error(obj.get("message") or obj.get("error"))))

    return out


# ── Streaming subprocess runner ──────────────────────────────────────────────

OnDelta = Callable[[str, str], Awaitable[None]] | None
OnCmd = Callable[[str, list[str]], None] | None


class ModelCallError(RuntimeError):
    """A provider CLI failed, produced nothing, or exceeded its timeout."""


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """Terminate a child that is still running, escalating to SIGKILL.

    Lives in a `finally`, not an `except`, so it also runs when the caller is
    cancelled — which is what stops Ctrl-C from leaving orphaned CLI processes.
    """
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=_TERM_GRACE)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        await proc.wait()


async def _consume(
    sess: Session,
    proc: asyncio.subprocess.Process,
    stderr_task: asyncio.Task[bytes],
    on_delta: OnDelta,
    start: float,
    cmd: list[str],
) -> ModelResponse:
    assert proc.stdout is not None
    buf: list[str] = []
    final: str | None = None
    captured_sid: str | None = None
    provider_error: str | None = None

    async for raw in proc.stdout:
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # non-JSON noise on stdout is expected from some CLIs
        for kind, payload in _parse_event(sess.cli, obj):
            if kind == "session" and payload:
                captured_sid = payload
            elif kind == "delta" and payload:
                buf.append(payload)
                if on_delta:
                    await on_delta(sess.key, payload)
            elif kind == "done" and payload:
                final = payload
            elif kind == "error" and payload:
                provider_error = payload

    await proc.wait()
    stderr = (await stderr_task).decode(errors="replace").strip()

    # A provider that told us *why* it failed beats anything we can infer from an
    # exit code or from stderr, which is usually startup noise.
    if provider_error:
        raise ModelCallError(f"{sess.cli}: {provider_error}")

    if proc.returncode not in (0, None):
        raise ModelCallError(
            f"{sess.cli} exited {proc.returncode}: {stderr[:300] or '(no stderr)'}"
        )

    content = (final if final is not None else "".join(buf)).strip()
    if not content:
        raise ModelCallError(f"{sess.cli} returned no content. {stderr[:200]}")

    if captured_sid and not sess.session_id:
        sess.session_id = captured_sid
    sess.started = True

    return ModelResponse(
        model=sess.key,
        content=content,
        session_id=sess.session_id,
        duration=time.monotonic() - start,
        cmd=cmd,
    )


async def call_session(
    sess: Session,
    prompt: str,
    on_delta: OnDelta = None,
    on_cmd: OnCmd = None,
    timeout: float = DEFAULT_TIMEOUT,
    schema_path_writer: Callable[[dict], str] | None = None,
) -> ModelResponse:
    """Run (or resume) `sess` with `prompt`, streaming deltas via `on_delta`.

    None of the three CLIs offers a timeout flag, so the deadline is enforced
    here. Marks the session started on success so later calls use the resume form.
    """
    cmd = _build_cmd(sess, prompt, schema_path_writer)
    if on_cmd:
        on_cmd(sess.key, cmd)

    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,  # codex reads stdin otherwise
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT,
        )
    except FileNotFoundError as e:
        raise ModelCallError(
            f"{sess.cli} is not installed or not on PATH. Run 'arbiter doctor'."
        ) from e

    assert proc.stderr is not None
    # Drain stderr concurrently so a full stderr pipe can't deadlock stdout.
    stderr_task = asyncio.create_task(proc.stderr.read())

    try:
        return await asyncio.wait_for(
            _consume(sess, proc, stderr_task, on_delta, start, cmd), timeout
        )
    except TimeoutError as e:
        raise ModelCallError(f"{sess.cli} exceeded the {timeout:.0f}s timeout") from e
    finally:
        stderr_task.cancel()
        await _reap(proc)
