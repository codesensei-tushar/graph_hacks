"""Port of company-brain `brain/slack/triage.ts`.

Passive triage for messages Company Brain was not explicitly asked about: ANSWER,
ACK (one emoji), INVESTIGATE (silent check first), or PASS (complete silence).
Prompts live in ``triage_prompts`` verbatim.

The model call goes through ``src.integrations.llm.model``. Upstream falls back to
PASS when generation or parsing fails, and so does this. One deliberate addition:
when no model is configured at all (``ModelUnavailable``), the deterministic thread
overrides still run on top of that PASS, so an explicit follow-up or a "yes" to the
bot's own offer in its thread is still answered without a model.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from src.domain.slack.turn.triage_prompts import TRIAGE_ACK_EMOJIS, TRIAGE_CHANNEL_PROMPT, TRIAGE_THREAD_PROMPT
from src.integrations.llm.model import ModelRequest, ModelUnavailable, generate_text

log = logging.getLogger(__name__)

REASON_MAX_CHARS = 900
PROMPT_FIELD_MAX_CHARS = 500

# Upstream TRIAGE_MODEL is claude-haiku-4.5 at BRAIN_TRIAGE_EFFORT "low"; the
# "triage" profile names that slot.
TRIAGE_PROFILE = "triage"
BRAIN_TRIAGE_EFFORT = "low"

# TriagePriority: summons | urgent | normal | low
# ChimeContext: thread | channel
# Effort: low | medium | high | xhigh


@dataclass
class TriageResult:
    decision: str  # answer | ack | investigate | pass
    # model | explicit_followup_override | affirmative_override | parse_fallback | error_fallback
    source: str
    priority: str | None = None
    priority_normalized: bool = False
    fallback_emoji: str | None = None
    agent_main_effort: str | None = None
    emoji: str | None = None
    reason: str | None = None


@dataclass
class TriageSpeaker:
    name: str | None = None
    slack_user_id: str | None = None


@dataclass
class TriageAddressedTarget:
    slack_user_id: str
    is_bot: bool
    name: str | None = None


@dataclass
class TriageChannel:
    name: str | None = None
    topic: str | None = None
    purpose: str | None = None


@dataclass
class TriageObservabilityContext:
    org_id: str
    distinct_id: str
    trace_id: str
    session_id: str | None = None
    channel: str | None = None
    message_ts: str | None = None
    thread_ts: str | None = None
    chime_context: str | None = None
    triage_trace_sampled: bool | None = None
    triage_trace_sample_rate: float | None = None


@dataclass
class TriageOutcome:
    decision: str  # ack | investigate
    reason: str
    outcome: str
    emoji: str | None = None
    error: str | None = None
    delivery_succeeded: bool | None = None
    terminal_reason: str | None = None
    extra: dict = field(default_factory=dict)


def _truncate_text(text: str, max_chars: int) -> str:
    trimmed = re.sub(r"\s+", " ", text.strip())
    if len(trimmed) <= max_chars:
        return trimmed
    budget = max_chars - 1
    sliced = trimmed[:budget]
    last_break = max(sliced.rfind(". "), sliced.rfind(" "))
    cut = sliced[:last_break] if last_break >= budget // 2 else sliced
    return f"{cut.rstrip()}…"


def _truncate_reason(text: str) -> str:
    return _truncate_text(text, REASON_MAX_CHARS)


def _prompt_field(value: str | None) -> str:
    return _truncate_text(value or "", PROMPT_FIELD_MAX_CHARS).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_FALLBACK_REASON = {
    "thread": {
        "parse_fallback": "Thread triage output was invalid; Company Brain stayed silent.",
        "error_fallback": "Thread triage generation failed; Company Brain stayed silent.",
    },
    "channel": {
        "parse_fallback": "Channel triage output was invalid; Company Brain stayed silent.",
        "error_fallback": "Channel triage generation failed; Company Brain stayed silent.",
    },
}


def _fallback_result(context: str, source: str) -> TriageResult:
    return TriageResult(decision="pass", source=source, reason=_FALLBACK_REASON[context][source])


def _normalized_payload_lines(text: str) -> list[str]:
    return [line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip()]


_TRIAGE_TOKEN_LINE = re.compile(r"^(?:ANSWER|ACK|INVESTIGATE|PASS|CHIME|IGNORE)$", re.IGNORECASE)
_TRIAGE_FIELD_LINE = re.compile(r"^(?:Emoji|Reason|Priority|Fallback|AgentMainEffort):", re.IGNORECASE)


def _parse_agent_main_effort(lines: list[str]) -> str | None:
    line = next((line for line in lines if re.match(r"^AgentMainEffort:", line, re.IGNORECASE)), None)
    match = re.match(r"^AgentMainEffort:\s*(low|medium|high|xhigh)$", line or "", re.IGNORECASE)
    return match.group(1).lower() if match else None


def _parse_reason(lines: list[str], start: int) -> str | None:
    if start < 0 or start >= len(lines):
        return None
    match = re.match(r"^Reason:\s*(.*)$", lines[start])
    if not match:
        return None
    continuation = lines[start + 1 :]
    if any(_TRIAGE_TOKEN_LINE.match(line) or _TRIAGE_FIELD_LINE.match(line) for line in continuation):
        return None
    reason = _truncate_reason(" ".join([match.group(1) or "", *continuation]))
    return reason or None


def _is_triage_ack_emoji(value: str | None) -> bool:
    return value in TRIAGE_ACK_EMOJIS


def parse_triage_result(text: str, context: str = "thread") -> TriageResult:
    lines = _normalized_payload_lines(text)
    if not lines:
        return _fallback_result(context, "parse_fallback")

    if lines[0] == "ANSWER":
        if any(not re.match(r"^(?:Priority|Fallback|Reason|AgentMainEffort):", line, re.IGNORECASE) for line in lines[1:]):
            return _fallback_result(context, "parse_fallback")
        priority_lines = [line for line in lines if re.match(r"^Priority:", line, re.IGNORECASE)]
        fallback_lines = [line for line in lines if re.match(r"^Fallback:", line, re.IGNORECASE)]
        reason_lines = [line for line in lines if re.match(r"^Reason:", line, re.IGNORECASE)]
        if (
            len(priority_lines) > 1
            or len(fallback_lines) > 1
            or len(reason_lines) > 1
            or any(not re.match(r"^Reason:\s*\S", line, re.IGNORECASE) for line in reason_lines)
        ):
            return _fallback_result(context, "parse_fallback")
        raw_match = re.match(r"^Priority:\s*([a-z]+)$", priority_lines[0], re.IGNORECASE) if priority_lines else None
        raw_priority = raw_match.group(1).lower() if raw_match else None
        priority_is_valid = raw_priority in ("summons", "urgent", "normal", "low")
        fallback_match = re.match(r"^Fallback:\s*ack\s+([a-z0-9_]+)$", fallback_lines[0], re.IGNORECASE) if fallback_lines else None
        fallback = fallback_match.group(1) if fallback_match else None
        return TriageResult(
            decision="answer",
            source="model",
            priority=raw_priority if priority_is_valid else "normal",
            priority_normalized=not priority_is_valid,
            fallback_emoji=fallback if _is_triage_ack_emoji(fallback) else None,
            agent_main_effort=_parse_agent_main_effort(lines),
        )

    if lines[0] == "ACK":
        if len(lines) < 3:
            return _fallback_result(context, "parse_fallback")
        emoji_match = re.match(r"^Emoji:\s*([a-z0-9_]+)$", lines[1])
        emoji = emoji_match.group(1) if emoji_match else None
        reason = _parse_reason(lines, 2)
        if not emoji or not _is_triage_ack_emoji(emoji) or not reason:
            return _fallback_result(context, "parse_fallback")
        return TriageResult(decision="ack", source="model", emoji=emoji, reason=reason)

    if lines[0] == "INVESTIGATE":
        priority_match = re.match(r"^Priority:\s*([a-z]+)$", lines[1], re.IGNORECASE) if len(lines) > 1 else None
        normalized_priority = priority_match.group(1).lower() if priority_match else None
        priority_is_valid = normalized_priority in ("urgent", "normal")
        reason_start = next((index for index, line in enumerate(lines) if re.match(r"^Reason:", line, re.IGNORECASE)), -1)
        reason = _parse_reason(lines, reason_start)
        if not reason:
            return _fallback_result(context, "parse_fallback")
        return TriageResult(
            decision="investigate",
            source="model",
            priority="urgent" if normalized_priority == "urgent" else "normal",
            priority_normalized=not priority_is_valid,
            reason=reason,
            agent_main_effort=_parse_agent_main_effort(lines),
        )

    if lines[0] == "PASS":
        reason = _parse_reason(lines, 1)
        return TriageResult(decision="pass", source="model", reason=reason) if reason else _fallback_result(context, "parse_fallback")

    return _fallback_result(context, "parse_fallback")


_SHORT_AFFIRMATIVE = re.compile(
    r"^(?:(?:yeah|yes|yep|yup|sure|ok(?:ay)?)(?:\s+please)?|please(?:\s+do)?|go\s+ahead|do\s+it|sounds\s+good|that\s+works|absolutely|definitely|👍|✅)(?:[!.,]?\s*)*$",
    re.IGNORECASE,
)
_BOT_OFFER = re.compile(
    r"\b(?:(?:want|would)\s+(?:me|you)\s+(?:to|like)|should\s+i|shall\s+i|i\s+can\s+(?:check|search|look|dig|find)|want\s+me\s+to|would\s+you\s+like\s+me\s+to|like\s+me\s+to)\b",
    re.IGNORECASE,
)
_DIRECT_REQUEST_TO_BOT = re.compile(r"\b(?:can|could|would|will|should)\s+(?:you|u)\b|\bcan\s+i\s+(?:get|have)\b", re.IGNORECASE)
_OPEN_QUESTION = re.compile(r"(?:^|[.!]\s+)(?:what|why|how|where|when|who|which)\b", re.IGNORECASE)
_IMPERATIVE_REQUEST = re.compile(
    r"(?:^|[.!]\s+)(?:please\s+)?(?:send|share|show|tell|give|find|check|look|search|connect|reconnect|authorize|open|create|update|summarize|explain|retry|regenerate|refresh|run|do)\b",
    re.IGNORECASE,
)

# From company-brain `brain/tools/mcp/access-intent.ts`: the requester says they cannot
# use their own app account (selects the temporary-access fallback upstream).
_PERSONAL_ACCESS_DENIAL_PATTERNS = (
    re.compile(r"\b(?:i|we)\s+(?:do\s+not|don['’]?t|dont)\s+have(?:\s+[\w-]+){0,5}\s+(?:access|permissions?|account)\b", re.IGNORECASE),
    re.compile(r"\b(?:i|we)\s+have\s+no(?:\s+[\w-]+){0,5}\s+(?:access|permissions?|account)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:i|we)\s+(?:can['’]?t|cant|cannot|can\s+not|couldn['’]?t|couldnt)\s+(?:access|authorize|authenticate|log\s*in|sign\s*in)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:access|permissions?)\s+(?:(?:is|was|got)\s+)?denied\b", re.IGNORECASE),
    re.compile(r"\b(?:i(?:['’]?m|\s+am)|we(?:['’]?re|\s+are))\s+not\s+(?:authorized|permitted|a\s+member)\b", re.IGNORECASE),
)


def requester_lacks_personal_app_access(text: str | None) -> bool:
    import unicodedata

    normalized = unicodedata.normalize("NFKC", text or "").strip()
    return bool(normalized) and any(pattern.search(normalized) for pattern in _PERSONAL_ACCESS_DENIAL_PATTERNS)


def is_short_affirmative(text: str) -> bool:
    return _SHORT_AFFIRMATIVE.match(text.strip()) is not None


def bot_last_thread_message_looks_like_offer(thread_text: str) -> bool:
    lines = [line.strip() for line in re.split(r"\r?\n", thread_text.strip()) if line.strip()]
    if not lines:
        return False
    bot_line = re.match(r"^(?:\[[^\]\n]+\]\s+)?Company Brain:\s*(.*)$", lines[-1])
    if not bot_line:
        return False
    return _BOT_OFFER.search((bot_line.group(1) or "").strip()) is not None


def is_explicit_bot_thread_follow_up(*, question: str, bot_spoke_previous: bool, addressed_to: str | None = None) -> bool:
    if not bot_spoke_previous or (addressed_to or "").strip():
        return False
    question = question.strip()
    if not question:
        return False
    return bool(
        re.search(r"\?\s*(?:[\"'’”\])}]*)$", question)
        or _DIRECT_REQUEST_TO_BOT.search(question)
        or _OPEN_QUESTION.search(question)
        or _IMPERATIVE_REQUEST.search(question)
        or requester_lacks_personal_app_access(question)
    )


def apply_thread_explicit_follow_up_override(
    triage: TriageResult, *, question: str, bot_spoke_previous: bool, addressed_to: str | None = None
) -> TriageResult:
    if is_explicit_bot_thread_follow_up(question=question, bot_spoke_previous=bot_spoke_previous, addressed_to=addressed_to):
        return TriageResult(decision="answer", source="explicit_followup_override", priority="summons")
    return triage


def apply_thread_affirmative_override(triage: TriageResult, *, question: str, thread_text: str, bot_spoke_previous: bool) -> TriageResult:
    if (
        triage.source != "model"
        or not bot_spoke_previous
        or not is_short_affirmative(question)
        or not bot_last_thread_message_looks_like_offer(thread_text)
    ):
        return triage
    return TriageResult(decision="answer", source="affirmative_override", priority="summons")


def _format_channel_prompt(channel: TriageChannel | None) -> str:
    if channel is None:
        return ""
    name = _prompt_field(channel.name)
    topic = _prompt_field(channel.topic) or _prompt_field(channel.purpose)
    if not name and not topic:
        return ""
    label = f"#{name.lstrip('#')}" if name else "unknown"
    return f"<channel>name: {label}; topic: {topic or 'not set'}</channel>"


def _format_address_signals(targets: list[TriageAddressedTarget]) -> list[str]:
    apps = [target for target in targets if target.is_bot]
    people = [target for target in targets if not target.is_bot]
    lines = []
    if apps:
        labels = [_prompt_field(target.name) or _prompt_field(target.slack_user_id) for target in apps]
        lines.append(
            f"Signal: this message @mentions the app{'s' if len(apps) > 1 else ''} {', '.join(labels)}. "
            "PASS unless Company Brain is also explicitly asked."
        )
    if people:
        labels = [_prompt_field(target.name) or _prompt_field(target.slack_user_id) for target in people]
        lines.append(
            f"Signal: this message @mentions {', '.join(labels)} (not Company Brain). Prefer PASS unless they are only "
            "being kept in the loop and the request still clearly asks Company Brain."
        )
    return lines


def build_triage_user_prompt(
    *,
    question: str,
    context_text: str,
    context: str,
    current_speaker: TriageSpeaker | None = None,
    message_stamp: str | None = None,
    history_complete: bool | None = None,
    addressed_targets: list[TriageAddressedTarget] | None = None,
    channel: TriageChannel | None = None,
) -> str:
    parts: list[str] = []
    channel_line = _format_channel_prompt(channel)
    if channel_line:
        parts += [channel_line, ""]
    has_history = bool(context_text.strip())
    if has_history:
        label = "channel_history" if context == "channel" else "thread_history"
        parts += [f"<{label}>", context_text, f"</{label}>", ""]
    if context == "thread" and history_complete is False:
        parts += [
            "<thread_history_status>",
            "The thread history above is incomplete. Prefer ANSWER for an open contextual request unless it is clearly directed at someone else or another app.",
            "</thread_history_status>",
            "",
        ]
    signals = _format_address_signals(addressed_targets or [])
    if signals:
        parts += [*signals, ""]
    name = _prompt_field(current_speaker.name if current_speaker else None)
    user_id = _prompt_field(current_speaker.slack_user_id if current_speaker else None)
    speaker_label = f"{name or user_id}{f' (slack_user_id={user_id})' if user_id else ''}" if (name or user_id) else ""
    stamp = f"[{_prompt_field(message_stamp)}] " if message_stamp else ""
    message_line = f"{stamp}{speaker_label}: {question}" if speaker_label else f"{stamp}{question}"
    if has_history:
        parts.append(
            'The message below is the next turn of the conversation above, in the same format. Read it as part of that '
            'conversation: "you" means whoever the speaker is talking to, which is Company Brain only when Company Brain '
            "is named, was asked, or spoke last."
        )
    parts += [
        "Return exactly ANSWER, ACK, INVESTIGATE, or PASS using the required fields for that token, classifying only this final message:",
        f"<new_message>\n{message_line}\n</new_message>",
    ]
    return "\n".join(parts)


def schedule_triage_outcome(obs: TriageObservabilityContext, outcome: TriageOutcome) -> None:
    """Upstream sends this to analytics; here it is a structured log line."""
    log.info(
        "[company-brain] triage outcome trace=%s context=%s decision=%s outcome=%s",
        obs.trace_id,
        obs.chime_context or "thread",
        outcome.decision,
        outcome.outcome,
    )


def triage_chime_message(
    *,
    context: str,
    question: str,
    context_text: str,
    current_speaker: TriageSpeaker | None = None,
    message_stamp: str | None = None,
    history_complete: bool | None = None,
    obs: TriageObservabilityContext | None = None,
    thread_follow_up_override: dict | None = None,
    addressed_targets: list[TriageAddressedTarget] | None = None,
    channel: TriageChannel | None = None,
    generate: Callable[[str, str], str] | None = None,
) -> TriageResult:
    """``thread_follow_up_override`` is ``{"bot_spoke_previous": bool}`` (thread only)."""
    system = TRIAGE_CHANNEL_PROMPT if context == "channel" else TRIAGE_THREAD_PROMPT
    prompt = build_triage_user_prompt(
        question=question,
        context_text=context_text,
        context=context,
        current_speaker=current_speaker,
        message_stamp=message_stamp,
        history_complete=history_complete,
        addressed_targets=addressed_targets,
        channel=channel,
    )
    no_model = False
    try:
        text = generate(system, prompt) if generate else generate_text(ModelRequest(profile=TRIAGE_PROFILE, system=system, prompt=prompt))
        result = parse_triage_result(text, context)
    except ModelUnavailable:
        no_model = True
        result = _fallback_result(context, "error_fallback")
    except Exception as caught:
        result = _fallback_result(context, "error_fallback")
        log.warning(
            "[company-brain] triage_generation_failed trace=%s context=%s name=%s",
            obs.trace_id if obs else "-",
            context,
            type(caught).__name__,
        )
        return result

    model_pass = result.decision == "pass" and (result.source == "model" or no_model)
    if context == "thread" and thread_follow_up_override and model_pass:
        bot_spoke_previous = bool(thread_follow_up_override.get("bot_spoke_previous"))
        candidate = result if result.source == "model" else TriageResult(decision="pass", source="model", reason=result.reason)
        overridden = apply_thread_explicit_follow_up_override(
            candidate,
            question=question,
            bot_spoke_previous=bot_spoke_previous,
            addressed_to=", ".join(target.name or target.slack_user_id for target in (addressed_targets or [])),
        )
        if overridden.decision == "pass":
            overridden = apply_thread_affirmative_override(
                candidate, question=question, thread_text=context_text, bot_spoke_previous=bot_spoke_previous
            )
        if overridden.decision == "answer":
            log.info("[company-brain] triage override pass -> answer context=%s source=%s", context, overridden.source)
            result = overridden
    return result
