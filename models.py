"""CLI callers for Claude Code, Codex, and Gemini CLI.

Each model runs as a *persistent* CLI session, streamed live:
  claude  →  claude -p "..." --output-format stream-json --include-partial-messages --verbose
  codex   →  codex exec --json --skip-git-repo-check "..."
  gemini  →  gemini -p "..." --output-format stream-json

The first call to a session captures the CLI's session id; later calls resume
it so context carries across rounds and the interactive follow-up loop.
"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional


# ── Model configuration (edit here) ──────────────────────────────────────────

# Debaters: Claude Sonnet vs Codex vs Gemini Pro.
DEBATERS = {
    "claude": {"cli": "claude", "model": "sonnet"},
    "codex":  {"cli": "codex",  "model": None},                 # codex default model
    "gemini": {"cli": "gemini", "model": "gemini-3.1-pro-preview"},
}

# Arbiter (judge) per --judge choice. Default 'claude' → Opus 4.8.
ARBITER_MODELS = {"claude": "opus", "codex": None, "gemini": "gemini-3.1-pro-preview"}
ARBITER_NAMES  = {"claude": "Opus 4.8", "codex": "Codex", "gemini": "Gemini Pro"}

VALID_MODELS = set(DEBATERS)


# ── Session + response envelopes ─────────────────────────────────────────────

@dataclass
class Session:
    """One model's persistent CLI session across the whole run."""
    key: str                          # logical label: "claude" | "codex" | "gemini" | "arbiter"
    cli: str                          # "claude" | "codex" | "gemini"
    model: Optional[str] = None       # --model value, or None for CLI default
    session_id: Optional[str] = None  # captured after the first call


@dataclass
class ModelResponse:
    model: str
    content: str
    session_id: Optional[str] = None
    duration: float = 0.0
    cmd: list[str] = field(default_factory=list)


def make_debater_sessions(keys: list[str]) -> dict[str, Session]:
    return {
        k: Session(key=k, cli=DEBATERS[k]["cli"], model=DEBATERS[k]["model"])
        for k in keys
    }


def make_arbiter_session(judge: str) -> Session:
    return Session(key="arbiter", cli=judge, model=ARBITER_MODELS[judge])


# ── Per-provider command builders ────────────────────────────────────────────

def _build_cmd(sess: Session, prompt: str) -> list[str]:
    """Build the CLI command for `sess`. Uses the resume form once a session id
    has been captured (gemini resumes 'latest' since it has only one session)."""
    if sess.cli == "claude":
        cmd = ["claude", "-p", prompt,
               "--output-format", "stream-json",
               "--include-partial-messages", "--verbose"]
        if sess.model:
            cmd += ["--model", sess.model]
        if sess.session_id:
            cmd += ["--resume", sess.session_id]
        return cmd

    if sess.cli == "codex":
        if sess.session_id:
            cmd = ["codex", "exec", "resume", sess.session_id,
                   "--json", "--skip-git-repo-check", prompt]
        else:
            cmd = ["codex", "exec", "--json", "--skip-git-repo-check"]
            if sess.model:
                cmd += ["-m", sess.model]
            cmd += [prompt]
        return cmd

    if sess.cli == "gemini":
        cmd = ["gemini", "-p", prompt, "--output-format", "stream-json"]
        if sess.model:
            cmd += ["-m", sess.model]
        if sess.session_id:
            cmd += ["--resume", "latest"]
        return cmd

    raise ValueError(f"Unknown cli: {sess.cli}")


# ── Per-provider streaming event parser ──────────────────────────────────────

def _parse_event(cli: str, obj: dict) -> list[tuple[str, Optional[str]]]:
    """Map one JSON event to a list of ('session'|'delta'|'done', payload).

    Shapes confirmed by probing each CLI:
      claude: system/init → session_id; stream_event content_block_delta → text;
              result → final text + session_id
      codex : thread.started → thread_id; item.completed/agent_message → text;
              turn.completed → done
      gemini: init → session_id; message(role=assistant) → incremental content;
              result → done
    """
    out: list[tuple[str, Optional[str]]] = []
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
            out.append(("done", obj.get("result")))
            if obj.get("session_id"):
                out.append(("session", obj["session_id"]))

    elif cli == "codex":
        if t == "thread.started":
            if obj.get("thread_id"):
                out.append(("session", obj["thread_id"]))
        elif t == "item.completed":
            item = obj.get("item", {})
            if item.get("type") == "agent_message":
                out.append(("delta", item.get("text", "")))
        elif t == "turn.completed":
            out.append(("done", None))

    elif cli == "gemini":
        if t == "init":
            if obj.get("session_id"):
                out.append(("session", obj["session_id"]))
        elif t == "message" and obj.get("role") == "assistant":
            out.append(("delta", obj.get("content", "")))
        elif t == "result":
            out.append(("done", None))

    return out


# ── Streaming subprocess runner ──────────────────────────────────────────────

OnDelta = Optional[Callable[[str, str], Awaitable[None]]]
OnCmd = Optional[Callable[[str, list[str]], None]]


async def call_session(
    sess: Session,
    prompt: str,
    on_delta: OnDelta = None,
    on_cmd: OnCmd = None,
) -> ModelResponse:
    """Run (or resume) `sess` with `prompt`, streaming deltas via on_delta.

    Mutates sess.session_id on the first call so subsequent calls resume.
    """
    cmd = _build_cmd(sess, prompt)
    if on_cmd:
        on_cmd(sess.key, cmd)

    loop = asyncio.get_event_loop()
    start = loop.time()

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,   # codex reads stdin otherwise
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout and proc.stderr

    # Drain stderr concurrently so a full stderr pipe can't deadlock stdout.
    stderr_task = asyncio.create_task(proc.stderr.read())

    buf: list[str] = []
    final: Optional[str] = None
    captured_sid: Optional[str] = None

    async for raw in proc.stdout:
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # ignore non-JSON noise on stdout
        for kind, payload in _parse_event(sess.cli, obj):
            if kind == "session" and payload:
                captured_sid = payload
            elif kind == "delta" and payload:
                buf.append(payload)
                if on_delta:
                    await on_delta(sess.key, payload)
            elif kind == "done" and payload:
                final = payload

    await proc.wait()
    stderr = (await stderr_task).decode(errors="replace").strip()

    if proc.returncode not in (0, None):
        raise RuntimeError(f"{sess.cli} exited {proc.returncode}: {stderr[:300] or '(no stderr)'}")

    content = (final if final is not None else "".join(buf)).strip()
    if not content:
        raise RuntimeError(f"{sess.cli} returned no content. {stderr[:200]}")

    if captured_sid and not sess.session_id:
        sess.session_id = captured_sid

    return ModelResponse(
        model=sess.key,
        content=content,
        session_id=sess.session_id,
        duration=loop.time() - start,
        cmd=cmd,
    )


# ── Prompt builders ──────────────────────────────────────────────────────────

def build_round_prompt(
    round_num: int,
    prompt: str,
    others: list[tuple[str, str]],
) -> str:
    """Prompt for one debater. Sessions are persistent, so round 2+ need only
    show the *other* debaters' latest answers (the model already has its own)."""
    if round_num == 1:
        return (
            "You are a debater in a multi-model AI debate. "
            "Answer this question concisely and clearly:\n\n" + prompt
        )

    others_text = "\n\n".join(f"[{m.upper()}]\n{a}" for m, a in others) or "(none)"
    return (
        "The other debaters answered:\n\n"
        f"{others_text}\n\n"
        "Refine your answer. Incorporate good points, push back on weak "
        "reasoning, or stand firm. Be concise."
    )


def build_judge_prompt(prompt: str, final_answers: list[tuple[str, str]]) -> str:
    answers = "\n\n---\n\n".join(
        f"Answer from {m} [{m.upper()}]:\n{a}" for m, a in final_answers
    )
    valid = " | ".join(m for m, _ in final_answers)
    return (
        f"You are the arbiter judging answers to this question:\n\n{prompt}\n\n"
        f"The debaters answered:\n\n{answers}\n\n"
        "Pick the best answer on reasoning quality, not confidence. "
        "Respond in this EXACT format:\n\n"
        f"WINNER: [one of: {valid}]\n\n"
        "REASON: [2-3 sentences on why it is best]\n\n"
        "BEST ANSWER:\n[the winning answer, improved and reproduced in full]"
    )


def build_followup_prompt(user_input: str) -> str:
    return (
        "The user has responded to your verdict:\n\n"
        f"\"{user_input}\"\n\n"
        "If you can address this directly, improve and restate your best answer. "
        "If it genuinely needs the debaters to weigh in again, reply with ONLY a "
        "single line:\n\nREPOLL: <the question to pose to the debaters>\n\n"
        "Otherwise, just give your improved answer."
    )
