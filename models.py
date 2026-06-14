"""CLI callers for Claude Code, Codex, and Gemini CLI.

Each model is called via subprocess using its headless flag:
  claude  →  claude -p "prompt" --output-format json
  codex   →  codex exec "prompt"  (stdout is plain text)
  gemini  →  gemini -p "prompt" --output-format json
"""

import asyncio
import json
import subprocess
from dataclasses import dataclass


@dataclass
class ModelResponse:
    model: str
    content: str


# ── Subprocess runner ────────────────────────────────────────────────────────

async def _run(cmd: list[str]) -> str:
    """Run a CLI command async, return stdout as a string."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err = stderr.decode().strip() or "(no stderr)"
        raise RuntimeError(f"Command {cmd[0]} exited {proc.returncode}: {err}")

    return stdout.decode().strip()


# ── Claude Code ──────────────────────────────────────────────────────────────

async def call_claude(messages: list[dict]) -> ModelResponse:
    prompt = _messages_to_text(messages)
    raw = await _run(["claude", "-p", prompt, "--output-format", "json"])

    # Claude JSON output: {"type":"result","subtype":"success","result":"..."}
    try:
        data = json.loads(raw)
        content = data.get("result") or data.get("response") or raw
    except json.JSONDecodeError:
        content = raw  # fallback to plain text

    return ModelResponse(model="claude", content=content)


# ── Codex ────────────────────────────────────────────────────────────────────

async def call_codex(messages: list[dict]) -> ModelResponse:
    prompt = _messages_to_text(messages)
    # codex exec outputs plain text to stdout
    raw = await _run(["codex", "exec", "--skip-git-repo-check", prompt])
    return ModelResponse(model="codex", content=raw)


# ── Gemini CLI ───────────────────────────────────────────────────────────────

async def call_gemini(messages: list[dict]) -> ModelResponse:
    prompt = _messages_to_text(messages)
    raw = await _run(["gemini", "-p", prompt, "--output-format", "json"])

    # Gemini JSON output: {"response": "...", "stats": {...}}
    try:
        data = json.loads(raw)
        content = data.get("response") or raw
    except json.JSONDecodeError:
        content = raw

    return ModelResponse(model="gemini", content=content)


# ── Dispatcher ───────────────────────────────────────────────────────────────

async def call_model(model: str, messages: list[dict]) -> ModelResponse:
    match model:
        case "claude":
            return await call_claude(messages)
        case "codex":
            return await call_codex(messages)
        case "gemini":
            return await call_gemini(messages)
        case _:
            raise ValueError(f"Unknown model: {model}")


# ── Message helpers ───────────────────────────────────────────────────────────

def _messages_to_text(messages: list[dict]) -> str:
    """Flatten a message list to a single prompt string for CLI tools.

    CLIs don't have a native chat-history format, so we embed prior
    turns as labeled text blocks inside the final user message.
    """
    if len(messages) == 1:
        return messages[0]["content"]

    # Multi-turn: concatenate with role labels so context is preserved
    parts = []
    for m in messages:
        role = m.get("role", "user").upper()
        parts.append(f"[{role}]\n{m['content']}")
    return "\n\n".join(parts)


# ── Message builders (shared with debate.py) ─────────────────────────────────

def build_round_messages(
    model: str,
    prompt: str,
    round_num: int,
    prior_answers: list[tuple[str, str]],
) -> list[dict]:
    if round_num == 1:
        return [{"role": "user", "content": (
            "You are participating in a multi-model AI debate. "
            "Answer this question concisely and clearly:\n\n" + prompt
        )}]

    others = "\n\n".join(
        f"[{m.upper()}]\n{a}"
        for m, a in prior_answers
        if m != model
    )
    own_prev = next(
        (a for m, a in prior_answers if m == model),
        "(no prior answer)"
    )

    return [{"role": "user", "content": (
        f"Original question:\n{prompt}\n\n"
        f"Your previous answer:\n{own_prev}\n\n"
        f"Other models answered:\n{others}\n\n"
        "Now refine your answer. You may incorporate good points from others, "
        "push back on weak reasoning, or stand firm. Be concise."
    )}]


def build_judge_messages(
    prompt: str,
    final_answers: list[tuple[str, str]],
) -> list[dict]:
    answers_text = "\n\n---\n\n".join(
        f"Answer {i+1} [{m.upper()}]:\n{a}"
        for i, (m, a) in enumerate(final_answers)
    )
    return [{"role": "user", "content": (
        f"You are a judge evaluating answers to this question:\n\n{prompt}\n\n"
        f"Here are the answers from different AI models:\n\n{answers_text}\n\n"
        "Pick the best answer. Respond in this EXACT format:\n\n"
        "WINNER: [model name]\n\n"
        "REASON: [2-3 sentences explaining why this answer is best]\n\n"
        "BEST ANSWER:\n[the winning answer, reproduced in full]"
    )}]
