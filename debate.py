"""Debate orchestration: runs N rounds concurrently, then calls the judge."""

import asyncio
from dataclasses import dataclass, field

from models import call_model, build_round_messages, build_judge_messages
import display


@dataclass
class DebateResult:
    winner: str
    reason: str
    best_answer: str
    judge: str
    rounds_history: list[list[tuple[str, str]]] = field(default_factory=list)


def _parse_judge_response(raw: str) -> tuple[str, str, str]:
    """Extract WINNER, REASON, BEST ANSWER from judge output."""
    markers = ["WINNER:", "REASON:", "BEST ANSWER:"]

    def extract(label: str) -> str:
        start = raw.find(label)
        if start == -1:
            return ""
        content_start = start + len(label)
        # Find the next marker after this one
        end = len(raw)
        for other in markers:
            if other == label:
                continue
            pos = raw.find(other, content_start)
            if pos != -1 and pos < end:
                end = pos
        return raw[content_start:end].strip()

    winner      = extract("WINNER:").lower().strip()
    reason      = extract("REASON:") or "No reason provided."
    best_answer = extract("BEST ANSWER:") or raw.strip()

    return winner, reason, best_answer


async def _run_round(
    debaters: list[str],
    prompt: str,
    round_num: int,
    prior_answers: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Call all debaters concurrently for one round. Returns answers."""

    async def fetch(model: str) -> tuple[str, str] | None:
        display.print_thinking(model)
        try:
            messages = build_round_messages(model, prompt, round_num, prior_answers)
            resp = await call_model(model, messages)
            return (resp.model, resp.content)
        except Exception as e:
            display.print_error(f"{model} failed: {e}")
            return None

    results = await asyncio.gather(*[fetch(m) for m in debaters])
    return [r for r in results if r is not None]


async def run_debate(
    prompt: str,
    debaters: list[str],
    judge: str,
    num_rounds: int,
) -> DebateResult:
    rounds_history: list[list[tuple[str, str]]] = []

    for round_num in range(1, num_rounds + 1):
        display.print_round_header(round_num, num_rounds)

        # All prior answers flat (for context in each message)
        prior_flat = [pair for round_ans in rounds_history for pair in round_ans]

        round_answers = await _run_round(debaters, prompt, round_num, prior_flat)

        if not round_answers:
            raise RuntimeError(f"All models failed in round {round_num}.")

        # Print results as they come in (they arrived in asyncio.gather order)
        for model, content in round_answers:
            display.print_model_answer(model, content, round_num)

        rounds_history.append(round_answers)

    # Judge gets only the final round's answers
    final_answers = rounds_history[-1]

    display.print_judging(judge)

    judge_messages = build_judge_messages(prompt, final_answers)
    judge_resp = await call_model(judge, judge_messages)

    winner, reason, best_answer = _parse_judge_response(judge_resp.content)

    return DebateResult(
        winner=winner,
        reason=reason,
        best_answer=best_answer,
        judge=judge,
        rounds_history=rounds_history,
    )
