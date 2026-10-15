"""Port of company-brain `brain/slack/chime.ts`.

Proactive chime-in on top-level channel messages Company Brain was not @mentioned in:
record the event, filter structural noise, honor channel proactivity, verify the
speaker is a workspace member, warm the local channel context, triage, then spend
the per-channel speaking budget (or react, or investigate silently).

Dropped from upstream: the paid-entitlement gate and telemetry. Sibling modules
(profile cache, channel lookup formatting, channel observe, public-channel rollout)
are imported lazily so this works while they are optional.

The caller supplies ``run_turn(workspace, event, **options)``, the full turn runner,
with options ``force_full_turn``, ``force_thread_ts``, ``trace_id``,
``triage_result`` ("answer" | "investigate"), ``agent_main_effort`` and, for
investigations, ``passive_investigation`` (reason, priority, obs, identity, claim).
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from src.domain.slack.turn import event_store
from src.domain.slack.turn.chime_budget import (
    can_use_answer_fallback,
    release_passive_investigation,
    reserve_chime_answer,
    reserve_passive_investigation,
)
from src.domain.slack.turn.event_store import SlackEventIdentity
from src.domain.slack.turn.proactivity import PROACTIVITY_FILTER_REASON, get_brain_proactivity, resolve_channel_proactivity
from src.domain.slack.turn.reaction_queue import enqueue_passive_reaction
from src.domain.slack.turn.triage import (
    TriageAddressedTarget,
    TriageChannel,
    TriageObservabilityContext,
    TriageSpeaker,
    triage_chime_message,
)
from src.domain.slack.turn.triage_sampling import reserve_triage_trace_sample
from src.integrations.slack.bot_address import clean_mention
from src.integrations.slack.events import (
    is_bot_mentioned,
    is_slack_content_message_subtype,
    mentioned_user_ids,
)

log = logging.getLogger(__name__)

CHANNEL_CONTEXT_LIMIT = 20
TRIAGE_PROFILE_LOOKUP_LIMIT = 16

RunTurn = Callable[..., None]


@dataclass
class _Profile:
    name: str | None
    is_bot: bool
    tz_offset: int = 0
    email: str | None = None


# --- helpers ported from events.ts that the parent module does not expose yet --------


def is_emoji_only_slack_text(text: str | None) -> bool:
    """Slack shortcode/custom markup or Unicode pictographs with no prose."""
    import re

    value = (text or "").strip()
    if not value:
        return False
    without_slack = re.sub(r":[a-z0-9_+-]+:", "", re.sub(r"<a?:[^:>\s]+:[^>]+>", "", value), flags=re.IGNORECASE)
    remaining = [
        char
        for char in without_slack
        if not (char.isspace() or char in "‍️" or _is_pictograph(char))
    ]
    return not remaining


def _is_pictograph(char: str) -> bool:
    code = ord(char)
    return (
        0x1F000 <= code <= 0x1FAFF
        or 0x2600 <= code <= 0x27BF
        or 0x2300 <= code <= 0x23FF
        or 0x2B00 <= code <= 0x2BFF
        or 0x1F3FB <= code <= 0x1F3FF
        or code in (0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139, 0x3030, 0x303D, 0x3297, 0x3299)
    )


def triage_profile_user_ids(*, current_user_id: str | None, mentioned_ids: list[str], history: list[dict], bot_user_id: str | None) -> list[str]:
    """Current speaker + new-message mentions + history speakers and their mentions."""
    ids: list[str] = []

    def add(user_id: str | None) -> None:
        if user_id and user_id not in ids:
            ids.append(user_id)

    add(current_user_id)
    for user_id in mentioned_ids:
        add(user_id)
    for message in reversed(history):
        if message.get("user") and message.get("user") != bot_user_id:
            add(message["user"])
        for user_id in mentioned_user_ids(message.get("text"), bot_user_id):
            add(user_id)
    return ids[:TRIAGE_PROFILE_LOOKUP_LIMIT]


# --- sibling seams ---------------------------------------------------------------------


def _optional(module: str, name: str):
    try:
        imported = __import__(module, fromlist=[name])
    except ImportError:
        return None
    return getattr(imported, name, None)


def _format_channel_context(
    messages: list[dict],
    user_names: dict[str, str],
    bot_user_ids: set[str],
    exclude_ts: str | None,
    tz_offset_seconds: int = 0,
) -> str:
    format_channel_messages = _optional("src.domain.slack.channels.lookup", "format_channel_messages")
    kept = [message for message in messages if message.get("ts") != exclude_ts]
    if format_channel_messages:
        formatted = [(item.ts, item.speaker, item.text) for item in format_channel_messages(kept, user_names, tz_offset_seconds)]
    else:
        formatted = [
            (message.get("ts"), user_names.get(message.get("user") or "", message.get("user") or "user"), message.get("text") or "")
            for message in kept
            if message.get("text")
        ]
    bot_message_ts = {
        message.get("ts")
        for message in messages
        if message.get("bot_id")
        or message.get("app_id")
        or message.get("subtype") == "bot_message"
        or (message.get("user") and message.get("user") in bot_user_ids)
    }
    lines = [f"{speaker} (app): {text}" if ts in bot_message_ts else f"{speaker}: {text}" for ts, speaker, text in formatted]
    return "\n".join(lines[-CHANNEL_CONTEXT_LIMIT:])


def _format_ts_human(ts: str, tz_offset_seconds: int) -> str:
    formatter = _optional("src.domain.slack.channels.lookup", "format_slack_ts_human")
    return formatter(ts, tz_offset_seconds) if formatter else ts


def _channel_info(workspace, channel_id: str) -> TriageChannel | None:
    cached = _optional("src.domain.slack.profile_cache", "get_cached_slack_channel_info")
    web = getattr(workspace.client, "web", None)
    info = None
    if cached and web is not None:
        info = cached(web, team_id=workspace.team_id, channel_id=channel_id)
    elif hasattr(workspace.client, "get_cached_slack_conversation_info"):
        info = workspace.client.get_cached_slack_conversation_info(workspace.team_id, channel_id)
    else:
        info = workspace.client.conversation_info(channel_id)
    if info is None:
        return None
    get = info.get if isinstance(info, dict) else lambda key, default=None: getattr(info, key, default)
    return TriageChannel(name=get("name"), topic=get("topic"), purpose=get("purpose"))


def _user_profile(workspace, user_id: str) -> _Profile | None:
    info = workspace.client.user_info(user_id)
    if info is None:
        return None
    return _Profile(
        name=info.real_name or info.display_name or info.name,
        is_bot=bool(info.is_bot),
        tz_offset=getattr(info, "tz_offset", 0) or 0,
        email=info.email,
    )


def _user_profiles(workspace, user_ids: list[str]) -> dict[str, _Profile]:
    cached = _optional("src.domain.slack.profile_cache", "get_cached_slack_user_profiles")
    web = getattr(workspace.client, "web", None)
    if cached and web is not None:
        profiles = cached(web, team_id=workspace.team_id, user_ids=user_ids)
        out = {}
        for user_id, profile in profiles.items():
            get = profile.get if isinstance(profile, dict) else lambda key, default=None, p=profile: getattr(p, key, default)
            out[user_id] = _Profile(name=get("name") or get("display_name"), is_bot=bool(get("is_bot")))
        return out
    out = {}
    for user_id in user_ids:
        profile = _user_profile(workspace, user_id)
        if profile:
            out[user_id] = profile
    return out


def _home_channel_id(team_id: str) -> str | None:
    get_home_channel = _optional("src.domain.slack.workspace", "get_home_channel")
    home = get_home_channel(team_id) if get_home_channel else None
    return (home or {}).get("channel_id")


def _is_awaiting_rollout_introduction(team_id: str, channel_id: str) -> bool:
    check = _optional("src.domain.slack.channels.rollout", "is_channel_awaiting_rollout_introduction")
    return bool(check and check(team_id, channel_id))


def _arm_channel_observe(team_id: str, channel_id: str) -> None:
    arm = _optional("src.domain.slack.channels.observe", "arm_channel_observe")
    if arm is None:
        return
    try:
        arm({"team_id": team_id, "channel": channel_id})
    except Exception as error:
        log.warning("[company-brain] channel-observe arm failed: %s", error)


# --- chime ----------------------------------------------------------------------------


def _structural_filter_reason(ev) -> str | None:
    if ev.bot_id or ev.app_id or ev.subtype == "bot_message":
        return "Bot-authored messages are context only and are never triaged."
    if not is_slack_content_message_subtype(ev.subtype):
        return f"Slack message subtype {ev.subtype} is context-only noise."
    text = (ev.text or "").strip()
    if not text:
        return "The Slack message has no triageable text."
    if is_emoji_only_slack_text(text):
        return "Emoji-only Slack messages are filtered before model triage."
    return None


def _warm_local_channel_context(workspace, channel: str, current_message_ts: str) -> None:
    if not event_store.claim_local_context_warmup(team_id=workspace.team_id, channel=channel, current_message_ts=current_message_ts):
        return
    try:
        result = workspace.client.get_slack_channel_history(channel, limit=50)
        # Leave context cold on rate-limit or API error so the next event retries.
        if not result.ok:
            return
        event_store.merge_slack_history_into_local_context(team_id=workspace.team_id, channel=channel, messages=result.messages)
        # A successful read includes at least the triggering message; an empty one
        # stays cold so a transient Slack failure can retry later.
        if result.messages:
            event_store.mark_local_context_warm(team_id=workspace.team_id, channel=channel)
    except Exception as error:
        log.warning("[company-brain] local channel context warmup failed channel=%s error=%s", channel, error)


def _is_org_member(workspace, user_id: str) -> tuple[bool, str | None, _Profile | None]:
    """(member, failure reason, profile). Mirrors the speaker check upstream."""
    from src.domain.slack.workspace import resolve_actor

    profile = _user_profile(workspace, user_id)
    if profile is None:
        return False, "profile_lookup_failed", None
    actor = resolve_actor(workspace, user_id)
    return bool(actor and actor.is_member), None, profile


def run_slack_chime_in_for_workspace(workspace, ev, *, run_turn: RunTurn) -> None:
    identity = event_store.record_slack_event(workspace.team_id, ev)
    if identity is None:
        return
    structural_reason = _structural_filter_reason(ev)
    if structural_reason:
        event_store.mark_stored_event_filtered(identity, structural_reason)
        return
    claim = event_store.claim_stored_event_for_triage(identity)
    if claim is None:
        return

    if ev.thread_ts or not ev.channel or not ev.user:
        event_store.mark_stored_event_filtered(identity, "The channel chime path only judges top-level human messages.", claim.id)
        return
    question = clean_mention(ev.text)
    if not question:
        event_store.mark_stored_event_filtered(identity, "The cleaned message was empty.", claim.id)
        return

    bot_user_id = workspace.bot_user_id
    if is_bot_mentioned(ev.text, bot_user_id):
        event_store.mark_stored_event_filtered(identity, "Explicit bot mentions bypass unsolicited proactivity budgets.", claim.id)
        return

    channel_info = _channel_info(workspace, identity.channel)
    proactivity = resolve_channel_proactivity(
        settings=get_brain_proactivity(),
        channel_id=identity.channel,
        home_channel_id=_home_channel_id(workspace.team_id),
        channel_name=channel_info.name if channel_info else None,
    )
    if proactivity == "quiet":
        event_store.mark_stored_event_filtered(identity, PROACTIVITY_FILTER_REASON, claim.id)
        return

    member, failure, asker = _is_org_member(workspace, ev.user)
    if failure:
        event_store.mark_stored_event_filtered(identity, f"Slack speaker verification failed: {failure}.", claim.id)
        return
    if not member:
        event_store.mark_stored_event_filtered(identity, "The Slack speaker is not a current member of this organization.", claim.id)
        return

    _warm_local_channel_context(workspace, identity.channel, identity.message_ts)
    history = event_store.load_local_channel_context(
        team_id=workspace.team_id, channel=identity.channel, before_ts=identity.message_ts, limit=CHANNEL_CONTEXT_LIMIT
    )
    other_mention_ids = mentioned_user_ids(ev.text, bot_user_id)
    profiles = _user_profiles(
        workspace,
        triage_profile_user_ids(current_user_id=ev.user, mentioned_ids=other_mention_ids, history=history, bot_user_id=bot_user_id),
    )
    user_names = {user_id: profile.name or user_id for user_id, profile in profiles.items()}
    bot_user_ids = {user_id for user_id, profile in profiles.items() if profile.is_bot}
    if bot_user_id:
        bot_user_ids.add(bot_user_id)
    addressed_targets = [
        TriageAddressedTarget(slack_user_id=user_id, name=user_names.get(user_id), is_bot=bool(profiles.get(user_id) and profiles[user_id].is_bot))
        for user_id in other_mention_ids
    ]
    trace_id = uuid.uuid4().hex
    sampling = reserve_triage_trace_sample(org_id=workspace.team_id, trace_id=trace_id)
    obs = TriageObservabilityContext(
        org_id=workspace.team_id,
        distinct_id=ev.user,
        trace_id=trace_id,
        session_id=f"{identity.channel}:{identity.message_ts}",
        channel=identity.channel,
        message_ts=identity.message_ts,
        thread_ts=identity.message_ts,
        chime_context="channel",
        triage_trace_sampled=sampling.sampled,
        triage_trace_sample_rate=sampling.sample_rate,
    )
    triage = triage_chime_message(
        context="channel",
        question=question,
        context_text=_format_channel_context(history, user_names, bot_user_ids, identity.message_ts, asker.tz_offset if asker else 0),
        current_speaker=TriageSpeaker(name=asker.name if asker else None, slack_user_id=ev.user),
        message_stamp=_format_ts_human(identity.message_ts, asker.tz_offset if asker else 0),
        addressed_targets=addressed_targets,
        channel=channel_info,
        obs=obs,
    )
    finalized = event_store.record_stored_triage_decision(
        identity,
        decision=triage.decision,
        claim_id=claim.id,
        source=triage.source,
        priority=triage.priority,
        reason=triage.reason,
        emoji=triage.emoji if triage.decision == "ack" else None,
        fallback_emoji=triage.fallback_emoji if triage.decision == "answer" else None,
        trace_id=trace_id,
    )
    if not finalized:
        log.warning(
            "[company-brain] channel chime discarded stale triage result team=%s channel=%s message=%s",
            identity.team_id,
            identity.channel,
            identity.message_ts,
        )
        return
    log.info(
        "[company-brain] channel chime triage=%s source=%s priority=%s agentEffort=%s trace=%s team=%s channel=%s message=%s",
        triage.decision,
        triage.source,
        triage.priority or "-",
        triage.agent_main_effort or "-",
        trace_id,
        workspace.team_id,
        identity.channel,
        identity.message_ts,
    )

    if triage.decision == "pass":
        event_store.record_stored_action_outcome(identity, "silent_by_judgment")
        return
    if triage.decision == "ack":
        enqueue_passive_reaction(identity=identity, emoji=triage.emoji, kind="ack", reason=triage.reason or "", obs=obs)
        return
    if triage.decision == "answer":
        budget = reserve_chime_answer(
            channel_id=identity.channel,
            priority=triage.priority or "normal",
            last_channel_activity_at=event_store.get_previous_human_channel_activity_at(identity),
        )
        if not budget.allowed:
            fallback_used = can_use_answer_fallback(budget.suppression, bool(triage.fallback_emoji), triage.priority or "normal")
            event_store.record_stored_suppression(identity, suppression=budget.suppression, fallback_used=fallback_used)
            if fallback_used and triage.fallback_emoji:
                enqueue_passive_reaction(
                    identity=identity,
                    emoji=triage.fallback_emoji,
                    kind="answer_fallback",
                    reason="Triage explicitly supplied a semantically valid reaction fallback.",
                    obs=obs,
                )
            return
        event_store.record_stored_action_outcome(identity, "answer_budget_reserved")
        run_turn(
            workspace,
            ev,
            force_full_turn=True,
            force_thread_ts=identity.message_ts,
            trace_id=trace_id,
            triage_result="answer",
            agent_main_effort=triage.agent_main_effort,
        )
        event_store.record_stored_action_outcome(identity, "answer_turn_completed")
        return

    investigation = reserve_passive_investigation(trace_id=trace_id, channel_id=identity.channel, priority=triage.priority or "normal")
    if not investigation.allowed:
        event_store.record_stored_suppression(identity, suppression=investigation.suppression)
        return
    try:
        run_turn(
            workspace,
            ev,
            force_full_turn=True,
            force_thread_ts=identity.message_ts,
            trace_id=trace_id,
            triage_result="investigate",
            agent_main_effort=triage.agent_main_effort,
            passive_investigation={
                "reason": triage.reason,
                "priority": triage.priority,
                "obs": obs,
                "identity": identity,
                "claim": investigation.claim,
            },
        )
        # No-op if the turn's finalizer already completed the claim; closes an admitted
        # investigation that returned before creating a turn control.
        release_passive_investigation(investigation.claim, "silent")
    except Exception:
        release_passive_investigation(investigation.claim, "failed")
        raise


def run_slack_chime_in(workspace, ev, *, run_turn: RunTurn) -> None:
    if workspace is None:
        log.warning("[company-brain] chime: no workspace; dropping")
        return
    channel = ev.channel
    if channel:
        _arm_channel_observe(workspace.team_id, channel)
    if channel and _is_awaiting_rollout_introduction(workspace.team_id, channel):
        return
    run_slack_chime_in_for_workspace(workspace, ev, run_turn=run_turn)


__all__ = ["SlackEventIdentity", "run_slack_chime_in", "run_slack_chime_in_for_workspace"]
