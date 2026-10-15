"""Port of company-brain `brain/slack/active-turn-gate.ts`.

Classifies a message that lands in a thread while Company Brain is already working:
IGNORE, APPEND, or REPLACE. The model call goes through ``src.integrations.llm.model``;
when it fails or no model is configured, the gate defaults to APPEND exactly as upstream.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from src.integrations.llm.model import ModelRequest, generate_text

log = logging.getLogger(__name__)

# Upstream pins claude-haiku-4.5 here; the "gate" profile names that slot.
ACTIVE_TURN_GATE_PROFILE = "gate"

# ActiveTurnGateResult: ignore | append | replace
# ActiveTurnAction: ignore | queue | restart

ACTIVE_TURN_GATE_PROMPT = """You classify the semantic effect of a new Slack message received while Company Brain is already working on a task. Classify the message the same way regardless of who authored it; authorization is enforced separately.

Return exactly one token:
- IGNORE: the message does not change the active task, such as commentary, encouragement, acknowledgements, or side conversation.
- APPEND: the message adds compatible scope or requirements that can be completed alongside the active task.
- REPLACE: the message explicitly contradicts, negates, or redirects away from the active task so both cannot be satisfied together.

Prefer IGNORE when there is no actionable instruction. Prefer APPEND whenever both requests can be satisfied in one coherent result. Additional research dimensions, comparisons, pricing, implications, recommendations, or strategy for the same subject are APPEND, even when they introduce a new desired outcome. Never choose REPLACE merely because the new message broadens the goal. Use REPLACE only for a real incompatibility, such as "instead", "stop doing that", "forget the earlier request", or requirements that cannot both be true. Short, conversational correction fragments are still actionable: for example, "actually no, Ines" or "not Marlo, Quentin" must be REPLACE rather than IGNORE. When uncertain, choose APPEND."""


@dataclass(frozen=True)
class ActiveTurnGateDecision:
    outcome: str


@dataclass
class ActiveTurnGateInput:
    active_question: str
    message: str
    author_owns_turn: bool
    current_instructions: list[str] | None = None
    author_name: str | None = None


def _parse_active_turn_gate_result(text: str) -> str:
    parts = text.strip().split(None, 1)
    token = parts[0].upper() if parts else ""
    if token == "IGNORE":
        return "ignore"
    if token == "REPLACE":
        return "replace"
    return "append"


def _has_explicit_correction_cue(message: str) -> bool:
    normalized = message.strip().lower()
    if not normalized:
        return False
    return bool(
        re.search(r"\b(?:instead|rather than|correction)\b", normalized)
        or re.search(r"\bactually[\s,:-]+(?:no|not|instead)\b", normalized)
        or re.search(r"^\s*(?:no|not)\s+(?!(?:problem|problems|worries|rush|needed|necessary|now)\b)\S+", normalized)
    )


def _preserve_explicit_corrections(outcome: str, message: str) -> str:
    return "replace" if outcome == "ignore" and _has_explicit_correction_cue(message) else outcome


def apply_active_turn_policy(*, outcome: str, author_owns_turn: bool, turn_status: str) -> str:
    if outcome == "ignore":
        return "ignore"
    if outcome == "replace":
        return "restart" if author_owns_turn else "queue"
    # Only the requester can restart a turn awaiting approval.
    if author_owns_turn and turn_status == "waiting_approval":
        return "restart"
    return "queue"


def _build_active_turn_gate_prompt(gate_input: ActiveTurnGateInput) -> str:
    parts = [f"<active_task>\n{gate_input.active_question}\n</active_task>"]
    if gate_input.current_instructions:
        parts.append("<current_instructions>\n" + "\n".join(gate_input.current_instructions) + "\n</current_instructions>")
    parts.append(
        f'<new_message author="{gate_input.author_name or "unknown"}" '
        f'author_owns_turn="{"true" if gate_input.author_owns_turn else "false"}">\n{gate_input.message}\n</new_message>'
    )
    return "\n\n".join(parts)


def triage_active_turn_message(
    gate_input: ActiveTurnGateInput,
    generate: Callable[[str, str], str] | None = None,
) -> ActiveTurnGateDecision:
    prompt = _build_active_turn_gate_prompt(gate_input)
    try:
        raw = (
            generate(ACTIVE_TURN_GATE_PROMPT, prompt)
            if generate
            else generate_text(ModelRequest(profile=ACTIVE_TURN_GATE_PROFILE, system=ACTIVE_TURN_GATE_PROMPT, prompt=prompt))
        )
        return ActiveTurnGateDecision(outcome=_preserve_explicit_corrections(_parse_active_turn_gate_result(raw), gate_input.message))
    except Exception as error:
        log.warning("[company-brain] active-turn gate failed, defaulting to append: %s", error)
        return ActiveTurnGateDecision(outcome="append")
