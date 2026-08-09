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
import errno
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, fields
from typing import Literal

# asyncio.StreamReader defaults to a 64 KiB line limit. Claude emits its entire
# final answer as ONE JSON line in the `result` event, so any answer longer than
# that raises "Separator is not found, and chunk exceed the limit" mid-stream.
STREAM_LIMIT = 8 * 1024 * 1024

DEFAULT_TIMEOUT = 300.0

# Above this, the prompt travels on stdin instead of argv. A plan plus a real
# diff blows past OS limits fast: ARG_MAX is 1 MiB on macOS, and Linux caps any
# single argument at 128 KiB (MAX_ARG_STRLEN) regardless of ARG_MAX. Verified:
# execve fails with E2BIG at ~1.1 MB here.
ARGV_PROMPT_LIMIT = 32 * 1024

# How long a child gets to exit after SIGTERM before we SIGKILL it.
_TERM_GRACE = 2.0

IsolationMode = Literal["readonly", "open"]


class _Unset:
    """Sentinel: 'use the session default' as distinct from 'explicitly none'."""

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<unset>"


UNSET = _Unset()


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
    # What `isolate_flags` actually covers, when it is less than all of it.
    # Reported verbatim by `arbiter doctor`: a provider that only silences
    # skills must not read as one that controls the whole system prompt.
    isolate_scope: str = "user config"
    # Can the prompt be written to stdin instead of argv? A plan plus a real
    # diff exceeds what execve will carry, so a provider that insists on argv
    # has a size ceiling the others do not.
    accepts_stdin_prompt: bool = True
    # Does its stream carry token counts we know how to read?
    reports_usage: bool = False
    # Is this one of the providers a bare `arbiter verify` reaches for, and that
    # `arbiter doctor` reports on without being asked? False means supported but
    # opt-in: still selectable by name, never assumed.
    default_panel: bool = True


PROVIDERS: dict[str, Provider] = {
    "claude": Provider(
        key="claude",
        cli="claude",
        default_model="sonnet",
        preassigns_session=True,
        output_schema_flag="--json-schema",
        readonly_flags=("--permission-mode", "plan"),
        isolate_flags=("--safe-mode", "--strict-mcp-config"),
        reports_usage=True,
    ),
    "codex": Provider(
        key="codex",
        cli="codex",
        default_model=None,
        preassigns_session=False,  # codex mints its own thread_id
        output_schema_flag="--output-schema",
        output_schema_as_file=True,
        readonly_flags=("-s", "read-only"),
        # NOT --ephemeral: it stops codex persisting session files, which breaks
        # `codex exec resume` and therefore multi-round debate and follow-ups.
        isolate_flags=("--ignore-user-config",),
        reports_usage=True,
    ),
    "gemini": Provider(
        key="gemini",
        cli="gemini",
        default_model=None,  # let the CLI pick its current default; pinned ids rot
        preassigns_session=True,
        output_schema_flag=None,  # no structured-output support
        readonly_flags=("--approval-mode", "plan"),
        isolate_flags=(),  # no equivalent
        # Its stream-json result may well carry counts; nothing here parses them
        # because nothing here has seen one. Claiming otherwise would put a
        # guessed shape into run records.
        reports_usage=False,
        # Opt-in: it is the weakest panel member (no structured output, no config
        # isolation), and its CLI blocks on an interactive login that a headless
        # run cannot answer — which turns a default run into a timeout rather
        # than a verdict. Still fully supported via `--models gemini`.
        default_panel=False,
    ),
    "antigravity": Provider(
        key="antigravity",
        cli="agy",
        default_model=None,  # `agy models` moves; let the CLI pick its default
        preassigns_session=False,  # agy mints its own conversation_id
        output_schema_flag="--json-schema",
        output_schema_as_file=False,  # takes the schema inline as a JSON string
        readonly_flags=("--mode", "plan"),
        # Stops skills and slash commands expanding in print mode. It does NOT
        # stop project instruction files or settings being read, so the scope
        # is narrower than claude's and codex's and says so.
        isolate_flags=("--disable-slash-commands",),
        isolate_scope="slash commands and skills only",
        # `--print` requires its value in argv; a bare `--print` swallows the
        # next flag as the prompt. Verified against agy 1.1.11.
        accepts_stdin_prompt=False,
        reports_usage=True,
    ),
}

VALID_MODELS = set(PROVIDERS)

# What `arbiter verify` and `arbiter doctor` reach for when not told otherwise.
# The rest stay selectable by name; nothing here removes a provider.
DEFAULT_PANEL = tuple(k for k, p in PROVIDERS.items() if p.default_panel)
OPT_IN_PROVIDERS = tuple(k for k in PROVIDERS if k not in DEFAULT_PANEL)


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
class Usage:
    """What one call consumed, normalized across providers.

    Every field is optional because no two CLIs report the same set, and a
    missing count must stay distinguishable from a zero one. `total_tokens` and
    `cost_usd` are only ever the provider's own figures — summing the parts
    ourselves would invent a number whose billing meaning we do not know.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None  # served from cache
    cache_write_tokens: int | None = None  # written to cache, billed separately
    reasoning_tokens: int | None = None  # thinking/reasoning, where reported
    total_tokens: int | None = None
    cost_usd: float | None = None

    @property
    def empty(self) -> bool:
        return all(getattr(self, f.name) is None for f in fields(self))

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def parse_usage(cli: str, obj: dict) -> Usage | None:
    """Read one provider's token counts off the event that carries them.

    Shapes confirmed by probing each CLI, and pinned by fixture tests for the
    same reason `_parse_event` is: a provider that renames a field should show
    up as a red test, not as a run record full of nulls.

      claude: result.usage{input_tokens, output_tokens,
              cache_creation_input_tokens, cache_read_input_tokens}
              plus result.total_cost_usd
      codex : turn.completed.usage{input_tokens, cached_input_tokens,
              output_tokens, reasoning_output_tokens}
      agy   : result.result.usage{input_tokens, output_tokens, thinking_tokens,
              cache_read_tokens, total_tokens}
    """
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return None

    if cli == "claude":
        out = Usage(
            input_tokens=_int(usage.get("input_tokens")),
            output_tokens=_int(usage.get("output_tokens")),
            cached_input_tokens=_int(usage.get("cache_read_input_tokens")),
            cache_write_tokens=_int(usage.get("cache_creation_input_tokens")),
            cost_usd=_float(obj.get("total_cost_usd")),
        )
    elif cli == "codex":
        out = Usage(
            input_tokens=_int(usage.get("input_tokens")),
            output_tokens=_int(usage.get("output_tokens")),
            cached_input_tokens=_int(usage.get("cached_input_tokens")),
            reasoning_tokens=_int(usage.get("reasoning_output_tokens")),
        )
    elif cli == "agy":
        out = Usage(
            input_tokens=_int(usage.get("input_tokens")),
            output_tokens=_int(usage.get("output_tokens")),
            cached_input_tokens=_int(usage.get("cache_read_tokens")),
            reasoning_tokens=_int(usage.get("thinking_tokens")),
            total_tokens=_int(usage.get("total_tokens")),
        )
    else:
        return None

    return None if out.empty else out


@dataclass
class ModelResponse:
    model: str
    content: str
    session_id: str | None = None
    duration: float = 0.0
    cmd: list[str] = field(default_factory=list)
    usage: Usage | None = None


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


def _schema_args(
    sess: Session,
    schema_path_writer: Callable[[dict], str] | None,
    schema: dict | _Unset | None = UNSET,
) -> list[str]:
    """Structured-output flags, when the provider supports them.

    A provider with no structured-output support (gemini) silently gets nothing —
    that asymmetry is expected and handled by the prose fallback. A provider that
    needs the schema on disk but was given no writer is a *caller* bug, and is
    raised rather than swallowed: dropping it would run the model unstructured
    with no signal that the requested schema never applied.
    """
    p = sess.provider
    schema = sess.output_schema if isinstance(schema, _Unset) else schema
    if not schema or not p.output_schema_flag:
        return []
    if p.output_schema_as_file:
        if schema_path_writer is None:
            raise ValueError(
                f"{p.cli} needs its schema written to a file, but no "
                f"schema_path_writer was supplied to call_session()."
            )
        return [p.output_schema_flag, schema_path_writer(schema)]
    return [p.output_schema_flag, json.dumps(schema)]


def prompt_goes_on_stdin(prompt: str) -> bool:
    """Whether this prompt is too big to survive execve as an argument."""
    return len(prompt.encode("utf-8", errors="replace")) > ARGV_PROMPT_LIMIT


def uses_stdin_prompt(provider: Provider, prompt: str) -> bool:
    """Where this prompt has to travel for this provider.

    A provider that will not read stdin gets the prompt in argv whatever its
    size: writing to a pipe it never drains would hang rather than fail, and
    hanging is the worse failure. The size ceiling that creates is reported when
    the OS refuses the exec, not guessed at here.
    """
    return prompt_goes_on_stdin(prompt) and provider.accepts_stdin_prompt


def _build_cmd(
    sess: Session,
    prompt: str,
    schema_path_writer: Callable[[dict], str] | None = None,
    via_stdin: bool = False,
    schema: dict | _Unset | None = UNSET,
) -> list[str]:
    """Build the CLI invocation for `sess`.

    Deterministic in `(sess, prompt, via_stdin)`: the same inputs always produce
    the same command, which is what makes every flag decision unit-testable. The
    one impurity is deliberate and injected — `schema_path_writer` materializes a
    schema to disk for providers that take a path rather than inline JSON, and
    the caller owns that file's lifetime.

    With `via_stdin`, the prompt is omitted from argv entirely; all three CLIs
    read it from stdin when no prompt argument is present. A plan plus a real
    diff exceeds OS argument limits well before the provider even launches.

    Uses the resume form once the session has been started. Session ids are
    pre-assigned where the provider allows, so two sessions of the same provider
    can never resolve to each other.
    """
    p = sess.provider
    extra: list[str] = []
    if sess.isolation == "readonly":
        extra += list(p.readonly_flags) + list(p.isolate_flags)
    extra += _schema_args(sess, schema_path_writer, schema)

    if p.cli == "claude":
        cmd = ["claude", "-p"]
        if not via_stdin:
            cmd.append(prompt)
        cmd += ["--output-format", "stream-json", "--include-partial-messages", "--verbose"]
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
        cmd += extra
        # `--` guards a prompt that starts with a dash from being read as a flag.
        return cmd if via_stdin else [*cmd, "--", prompt]

    if p.cli == "gemini":
        cmd = ["gemini"] if via_stdin else ["gemini", "-p", prompt]
        cmd += ["--output-format", "stream-json"]
        if sess.model:
            cmd += ["-m", sess.model]
        if sess.session_id:
            cmd += ["--resume" if sess.started else "--session-id", sess.session_id]
        return cmd + extra

    if p.cli == "agy":
        # The prompt is always in argv: `--print` takes its value there, and a
        # bare `--print` swallows the following flag as the prompt. `via_stdin`
        # can never be true here — call_session consults accepts_stdin_prompt —
        # but building the command this way keeps it correct either way.
        cmd = ["agy", "--print", prompt, "--output-format", "stream-json"]
        if sess.model:
            cmd += ["--model", sess.model]
        # agy mints its own conversation id; we capture it and resume by it.
        if sess.started and sess.session_id:
            cmd += ["--conversation", sess.session_id]
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


def _parse_event(cli: str, obj: dict) -> list[tuple[str, object]]:
    """Map one JSON event to ('session' | 'delta' | 'done' | 'error' | 'usage')
    pairs.

    Shapes confirmed by probing each CLI, and pinned by fixture tests so a
    provider schema change shows up as a red test rather than an empty answer.

      claude: system/init -> session_id; stream_event content_block_delta -> text;
              result -> final text + session_id + usage
      codex : thread.started -> thread_id; item.completed/agent_message -> text;
              turn.completed -> done + usage
      gemini: init -> session_id; message(role=assistant) -> incremental content;
              result -> done
      agy   : keyed on `event`, not `type`. init -> conversation_id;
              step_update/agent_response -> text_delta;
              result -> final response + conversation_id + usage
    """
    out: list[tuple[str, object]] = []
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
            if (usage := parse_usage(cli, obj)) is not None:
                out.append(("usage", usage))

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
            if (usage := parse_usage(cli, obj)) is not None:
                out.append(("usage", usage))
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

    elif cli == "agy":
        event = obj.get("event")
        if event == "init":
            if obj.get("conversation_id"):
                out.append(("session", obj["conversation_id"]))
        elif event == "step_update":
            step = obj.get("step_update") or {}
            # Only the agent's own prose is answer text; tool and checkpoint
            # steps carry deltas of their own that must not land in the reply.
            if step.get("step_type") == "agent_response" and step.get("text_delta"):
                out.append(("delta", step["text_delta"]))
        elif event == "result":
            result = obj.get("result") or {}
            if str(result.get("status", "")).upper() == "SUCCESS":
                out.append(("done", result.get("response")))
            else:
                out.append(
                    (
                        "error",
                        _unwrap_provider_error(
                            result.get("error") or f"agy reported {result.get('status')}"
                        ),
                    )
                )
            if result.get("conversation_id"):
                out.append(("session", result["conversation_id"]))
            if (usage := parse_usage(cli, result)) is not None:
                out.append(("usage", usage))

    return out


# ── Streaming subprocess runner ──────────────────────────────────────────────

OnDelta = Callable[[str, str], Awaitable[None]] | None
OnCmd = Callable[[str, list[str]], None] | None


class ModelCallError(RuntimeError):
    """A provider CLI failed, produced nothing, or exceeded its timeout."""


# How many lines of non-JSON stdout to keep for diagnosis.
_PROSE_KEPT = 3
# How much of a still-buffered, unterminated line to pull back on a timeout.
_PARTIAL_READ = 4096
_PARTIAL_WAIT = 0.25

# A CLI that needs to log in asks on stdout and then waits on a stdin that is
# never going to answer. "exceeded the timeout" is true and useless; quoting the
# question it asked is the actual diagnosis.
_INTERACTIVE_RE = re.compile(
    r"\[y/n\]|\(y/n\)|\[yes/no\]|press enter|continue\?|sign in|log ?in|authenticat",
    re.I,
)


async def _collect_partial_line(proc: asyncio.subprocess.Process, prose: list[str]) -> None:
    """Pull back whatever is buffered on stdout but never terminated.

    A CLI waiting for a human ends its question with ": ", not a newline, so the
    line iterator in `_consume` never yields it and the timeout has nothing to
    report but its own duration. `read(n)` returns as soon as any byte is
    available, so this costs nothing when there is genuinely nothing there.
    """
    if proc.stdout is None or len(prose) >= _PROSE_KEPT:
        return
    try:
        chunk = await asyncio.wait_for(proc.stdout.read(_PARTIAL_READ), _PARTIAL_WAIT)
    except (TimeoutError, OSError, ValueError):
        return
    if text := chunk.decode(errors="replace").strip():
        prose.append(text[:200])


def _blocked_hint(prose: list[str] | None) -> str:
    """Turn captured stdout prose into a diagnosis, when it looks like a prompt."""
    if not prose:
        return ""
    text = " ".join(prose).strip()
    if not _INTERACTIVE_RE.search(text):
        return f" It wrote: {text[:200]!r}."
    return (
        f" It wrote: {text[:200]!r} — it is waiting for an answer that headless "
        "mode cannot give. Run it once interactively to authenticate, then retry."
    )


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
    prose: list[str] | None = None,
) -> ModelResponse:
    assert proc.stdout is not None
    buf: list[str] = []
    final: str | None = None
    captured_sid: str | None = None
    provider_error: str | None = None
    usage: Usage | None = None

    async for raw in proc.stdout:
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            obj = None
        # Noise on stdout is expected from some CLIs, but it is also how an
        # unauthenticated one asks a question it will then block on forever.
        # Keep a little of it so the caller can say so. A bare JSON string or
        # list is noise too — `_parse_event` would raise on it.
        if not isinstance(obj, dict):
            if prose is not None and len(prose) < _PROSE_KEPT:
                prose.append(line[:200])
            continue
        for kind, payload in _parse_event(sess.cli, obj):
            if kind == "session" and payload:
                captured_sid = str(payload)
            elif kind == "delta" and payload:
                buf.append(str(payload))
                if on_delta:
                    await on_delta(sess.key, str(payload))
            elif kind == "done" and payload:
                final = str(payload)
            elif kind == "error" and payload:
                provider_error = str(payload)
            elif kind == "usage" and isinstance(payload, Usage):
                usage = payload

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
        raise ModelCallError(
            f"{sess.cli} returned no content.{_blocked_hint(prose)} {stderr[:200]}".rstrip()
        )

    if captured_sid and not sess.session_id:
        sess.session_id = captured_sid
    sess.started = True

    return ModelResponse(
        model=sess.key,
        content=content,
        session_id=sess.session_id,
        duration=time.monotonic() - start,
        cmd=cmd,
        usage=usage,
    )


async def call_session(
    sess: Session,
    prompt: str,
    on_delta: OnDelta = None,
    on_cmd: OnCmd = None,
    timeout: float = DEFAULT_TIMEOUT,
    schema_path_writer: Callable[[dict], str] | None = None,
    output_schema: dict | _Unset | None = UNSET,
) -> ModelResponse:
    """Run (or resume) `sess` with `prompt`, streaming deltas via `on_delta`.

    `output_schema` overrides the session's schema for this turn only. Pass None
    to force a free-form reply: a session created for structured judging would
    otherwise keep emitting that JSON during follow-ups that expect prose.

    None of the three CLIs offers a timeout flag, so the deadline is enforced
    here. Marks the session started on success so later calls use the resume form.
    """
    via_stdin = uses_stdin_prompt(sess.provider, prompt)
    cmd = _build_cmd(sess, prompt, schema_path_writer, via_stdin=via_stdin, schema=output_schema)
    if on_cmd:
        on_cmd(sess.key, cmd)

    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            # DEVNULL when the prompt is in argv — codex otherwise blocks reading
            # stdin. A PIPE only when we actually have a prompt to write.
            stdin=asyncio.subprocess.PIPE if via_stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT,
        )
    except FileNotFoundError as e:
        raise ModelCallError(
            f"{sess.cli} is not installed or not on PATH. Run 'arbiter doctor'."
        ) from e
    except OSError as e:
        if e.errno == errno.E2BIG and not sess.provider.accepts_stdin_prompt:
            raise ModelCallError(
                f"{sess.cli} cannot accept a prompt this large "
                f"({len(prompt.encode('utf-8', errors='replace')):,} bytes): it only takes "
                "the prompt as a command-line argument, and this exceeds what the OS "
                f"will pass. Drop {sess.provider.key} from --models for this change."
            ) from e
        raise ModelCallError(f"could not launch {sess.cli}: {e}") from e

    assert proc.stderr is not None
    # Start draining stderr BEFORE anything else can block. A child that fills
    # its stderr pipe stops reading stdin, so writing the prompt first can
    # deadlock — and it used to do so outside the timeout, where neither the
    # deadline nor the cleanup could reach it.
    stderr_task = asyncio.create_task(proc.stderr.read())

    async def _write_prompt() -> None:
        if not (via_stdin and proc.stdin is not None):
            return
        try:
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the child died early; _consume reports the real reason
        finally:
            proc.stdin.close()

    # Filled by _consume as it reads; survives a timeout, which is exactly the
    # case where what the CLI printed instead of JSON is the whole diagnosis.
    prose: list[str] = []

    async def _run() -> ModelResponse:
        await _write_prompt()
        return await _consume(sess, proc, stderr_task, on_delta, start, cmd, prose)

    try:
        # One scope covers writing the prompt AND reading the response, so a
        # child that stops consuming stdin cannot hang past the deadline.
        return await asyncio.wait_for(_run(), timeout)
    except TimeoutError as e:
        await _collect_partial_line(proc, prose)
        raise ModelCallError(
            f"{sess.cli} exceeded the {timeout:.0f}s timeout.{_blocked_hint(prose)}"
        ) from e
    finally:
        # `finally`, so cancellation during the stdin write still reaps the child.
        stderr_task.cancel()
        await _reap(proc)
