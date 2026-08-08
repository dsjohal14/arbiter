"""Prompt construction and judge blinding.

The judge used to be shown vendor-labelled answers ("Answer from claude
[CLAUDE]"), which is a self-preference confound: an Anthropic model grading an
Anthropic answer against competitors' knows which is which. Answers are now
relabelled A/B/C and shuffled.

Blinding and reproducibility pull in opposite directions, so the seed and the
label -> model mapping are returned alongside and persisted by the caller. A run
stays deterministic and attributable after the fact without the judge ever
seeing a vendor name.
"""

from __future__ import annotations

import random
import secrets
from dataclasses import dataclass

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass(frozen=True)
class Blinding:
    """The record needed to reverse a blinding after the fact."""

    seed: int
    label_to_model: dict[str, str]

    def resolve(self, label: str | None) -> str | None:
        if label is None:
            return None
        return self.label_to_model.get(label.strip().upper())

    def as_dict(self) -> dict:
        return {"seed": self.seed, "label_to_model": dict(self.label_to_model)}


def blind(
    answers: list[tuple[str, str]], seed: int | None = None
) -> tuple[list[tuple[str, str]], Blinding]:
    """Shuffle and relabel answers as A/B/C, hiding which model wrote which.

    Returns the blinded [(label, text)] plus the record to reverse it. Passing
    the same seed reproduces the same arrangement exactly.
    """
    if seed is None:
        seed = secrets.randbelow(2**32)
    shuffled = list(answers)
    random.Random(seed).shuffle(shuffled)

    blinded: list[tuple[str, str]] = []
    mapping: dict[str, str] = {}
    for i, (model, text) in enumerate(shuffled):
        label = _ALPHABET[i]
        blinded.append((label, text))
        mapping[label] = model
    return blinded, Blinding(seed=seed, label_to_model=mapping)


# ── Debate prompts ───────────────────────────────────────────────────────────


def build_round_prompt(round_num: int, prompt: str, others: list[tuple[str, str]]) -> str:
    """Prompt for one debater. Sessions are persistent, so round 2+ needs to show
    only the *other* debaters' latest answers — the model already has its own.

    Other debaters are shown blinded here too, so a model can't defer to a vendor
    it recognizes."""
    if round_num == 1:
        return (
            "You are a debater in a multi-model AI debate. "
            "Answer this question concisely and clearly:\n\n" + prompt
        )

    others_text = "\n\n".join(f"[ANSWER {lbl}]\n{a}" for lbl, a in others) or "(none)"
    return (
        "The other debaters answered:\n\n"
        f"{others_text}\n\n"
        "Refine your answer. Incorporate good points, push back on weak "
        "reasoning, or stand firm. Be concise."
    )


JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "winner": {"type": "string", "description": "The label of the best answer, e.g. 'A'"},
        "reason": {"type": "string", "description": "2-3 sentences on why it is best"},
        "best_answer": {"type": "string", "description": "The winning answer, improved, in full"},
    },
    "required": ["winner", "reason", "best_answer"],
    "additionalProperties": False,
}


def build_judge_prompt(prompt: str, blinded: list[tuple[str, str]]) -> str:
    answers = "\n\n---\n\n".join(f"ANSWER {lbl}:\n{a}" for lbl, a in blinded)
    valid = " | ".join(lbl for lbl, _ in blinded)
    return (
        f"You are the arbiter judging answers to this question:\n\n{prompt}\n\n"
        f"The answers, presented anonymously:\n\n{answers}\n\n"
        "Pick the best answer on reasoning quality, not confidence. "
        "You do not know which model wrote which answer; judge only the content. "
        "Respond in this EXACT format:\n\n"
        f"WINNER: <one of: {valid}>\n\n"
        "REASON: <2-3 sentences on why it is best>\n\n"
        "BEST ANSWER:\n<the winning answer, improved and reproduced in full>"
    )


def build_repair_prompt(valid: list[str]) -> str:
    return (
        "That response did not name a valid winner. Reply with ONLY a single "
        f"line, nothing else:\n\nWINNER: <one of: {' | '.join(valid)}>"
    )


def build_followup_prompt(user_input: str) -> str:
    return (
        "The user has responded to your verdict:\n\n"
        f'"{user_input}"\n\n'
        "If you can address this directly, improve and restate your best answer. "
        "If it genuinely needs the debaters to weigh in again, reply with ONLY a "
        "single line:\n\nREPOLL: <the question to pose to the debaters>\n\n"
        "Otherwise, just give your improved answer."
    )
