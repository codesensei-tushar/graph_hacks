"""Slack event envelopes and classifiers. Port of company-brain ``brain/slack/events.ts``.

Chime-in and triage classifiers are ported too, but nothing routes to them until
the agent exists.
"""

import re

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SlackAssistantThread(_Model):
    user_id: str | None = None
    channel_id: str | None = None
    thread_ts: str | None = None


class SlackReactionItem(_Model):
    type: str | None = None
    channel: str | None = None
    ts: str | None = None


class SlackEventInner(_Model):
    type: str
    user: str | None = None
    text: str | None = None
    ts: str | None = None
    channel: str | None = None
    channel_type: str | None = None
    thread_ts: str | None = None
    bot_id: str | None = None
    subtype: str | None = None
    app_id: str | None = None
    event_ts: str | None = None
    assistant_thread: SlackAssistantThread | None = None
    reaction: str | None = None
    item: SlackReactionItem | None = None


class SlackUrlVerification(_Model):
    type: str
    challenge: str


class SlackEventCallback(_Model):
    type: str
    team_id: str
    api_app_id: str | None = None
    event_id: str
    event_time: int | None = None
    event: SlackEventInner


class SlackWorkspaceUser(_Model):
    id: str
    deleted: bool | None = None
    is_bot: bool | None = None
    is_restricted: bool | None = None
    is_ultra_restricted: bool | None = None
    is_stranger: bool | None = None
    team_id: str | None = None
    real_name: str | None = None
    name: str | None = None


def parse_envelope(body: dict) -> SlackUrlVerification | SlackEventCallback | None:
    kind = body.get("type")
    if kind == "url_verification" and isinstance(body.get("challenge"), str):
        return SlackUrlVerification.model_validate(body)
    if kind != "event_callback":
        return None
    event = body.get("event") or {}
    # team_join / user_change carry a nested user OBJECT; flatten it to the id.
    if isinstance(event.get("user"), dict):
        return None
    try:
        return SlackEventCallback.model_validate(body)
    except ValueError:
        return None


def parse_workspace_user_event(body: dict) -> tuple[str, str, str, SlackWorkspaceUser] | None:
    """team_join / user_change envelopes. Returns (team_id, event_id, type, user)."""
    if body.get("type") != "event_callback":
        return None
    event = body.get("event") or {}
    if event.get("type") not in ("team_join", "user_change") or not isinstance(event.get("user"), dict):
        return None
    return body["team_id"], body["event_id"], event["type"], SlackWorkspaceUser.model_validate(event["user"])


# agent_view user messages normally have no subtype. Preserve human-authored
# content subtypes while ignoring bot/app messages and structural events.
ALLOWED_MESSAGE_SUBTYPES = {"file_share", "me_message", "thread_broadcast", "reply_broadcast"}


def is_slack_content_message_subtype(subtype: str | None) -> bool:
    return not subtype or subtype in ALLOWED_MESSAGE_SUBTYPES


def should_ignore_message(ev: SlackEventInner) -> bool:
    if ev.bot_id or ev.app_id:
        return True
    return not is_slack_content_message_subtype(ev.subtype)


def is_direct_slack_channel(channel: str | None) -> bool:
    """Slack DM channel ids start with "D"."""
    return (channel or "").startswith("D")


def is_direct_message(ev: SlackEventInner) -> bool:
    if ev.type != "message":
        return False
    return ev.channel_type == "im" or is_direct_slack_channel(ev.channel)


def is_direct_conversation_event(ev: SlackEventInner) -> bool:
    return ev.channel_type == "im" or is_direct_slack_channel(ev.channel)


def is_private_slack_channel(channel: str | None, channel_type: str | None) -> bool:
    """Fails closed: a C-prefixed channel with no channel_type counts as private."""
    if channel_type in ("group", "mpim"):
        return True
    if channel_type in ("channel", "im"):
        return False
    if (channel or "").startswith("G"):
        return True
    return (channel or "").startswith("C")


def is_assistant_thread_message(ev: SlackEventInner) -> bool:
    if ev.type != "message":
        return False
    # Agent Messages follow-ups are ordinary IM messages with thread_ts.
    return is_direct_message(ev) and bool(ev.thread_ts)


def is_agent_surface_event(ev: SlackEventInner) -> bool:
    return ev.type in ("app_home_opened", "app_context_changed")


def is_channel_membership_event(ev: SlackEventInner) -> bool:
    """Channel membership changes keep the DM cross-channel read scope in sync."""
    return ev.type in ("member_joined_channel", "member_left_channel")


_MENTION = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]+)?>")


def _slack_mentioned_user_ids(text: str | None) -> list[str]:
    ids: list[str] = []
    for match in _MENTION.finditer(text or ""):
        if match.group(1) not in ids:
            ids.append(match.group(1))
    return ids


def is_bot_mentioned(text: str | None, bot_user_id: str | None) -> bool:
    if not text or not bot_user_id:
        return False
    return bot_user_id in _slack_mentioned_user_ids(text)


def mentioned_user_ids(text: str | None, bot_user_id: str | None) -> list[str]:
    """Slack user ids @mentioned in the text, excluding the bot."""
    return [user_id for user_id in _slack_mentioned_user_ids(text) if user_id != bot_user_id]


def is_addressed_to_other_slack_user(text: str | None, bot_user_id: str | None) -> bool:
    """A direct mention of someone else is context only, unless the bot is mentioned too."""
    if not bot_user_id:
        return False
    if is_bot_mentioned(text, bot_user_id):
        return False
    return len(mentioned_user_ids(text, bot_user_id)) > 0


def is_answered_event(ev: SlackEventInner, bot_user_id: str | None = None) -> bool:
    if should_ignore_message(ev):
        return False
    if is_direct_conversation_event(ev):
        return ev.type == "message"
    # Slack sends both app_mention and message for @mentions; only answer once.
    if is_bot_mentioned(ev.text, bot_user_id):
        return ev.type == "app_mention"
    if ev.type == "app_mention":
        return True
    if ev.type == "message" and ev.thread_ts:
        return True
    return False


# Emoji whose reaction on a bot message surfaces its debug trace (ephemeral).
BRAIN_DEBUG_REACTIONS = {"mag", "bug"}
BRAIN_MUTE_REACTION = "black_square_for_stop"


def is_debug_reaction_event(ev: SlackEventInner) -> bool:
    if ev.type != "reaction_added" or ev.reaction not in BRAIN_DEBUG_REACTIONS:
        return False
    return bool(ev.item and ev.item.type == "message" and ev.item.channel and ev.item.ts)


def is_mute_reaction_event(ev: SlackEventInner) -> bool:
    if ev.type not in ("reaction_added", "reaction_removed") or ev.reaction != BRAIN_MUTE_REACTION:
        return False
    return bool(ev.item and ev.item.type == "message" and ev.item.channel and ev.item.ts)


def is_name_wake_candidate(ev: SlackEventInner, bot_user_id: str | None = None) -> bool:
    """Channel/thread message that might address the bot by plain name (not <@id>)."""
    if should_ignore_message(ev) or ev.type != "message":
        return False
    if is_bot_mentioned(ev.text, bot_user_id):
        return False
    if is_direct_message(ev) or is_assistant_thread_message(ev):
        return False
    return bool((ev.text or "").strip())


def is_context_retention_event(ev: SlackEventInner, bot_user_id: str | None = None) -> bool:
    """Messages kept as local context. The human twin of an app_mention is excluded."""
    if ev.type != "message" or not ev.channel:
        return False
    is_human_app_mention_twin = not should_ignore_message(ev) and is_bot_mentioned(ev.text, bot_user_id)
    return not is_human_app_mention_twin


def is_chime_in_event(ev: SlackEventInner, bot_user_id: str | None = None) -> bool:
    """Top-level channel message eligible for proactive chime-in (bot must be in channel)."""
    if should_ignore_message(ev) or ev.type != "message" or ev.thread_ts:
        return False
    if is_direct_message(ev) or is_assistant_thread_message(ev):
        return False
    if is_bot_mentioned(ev.text, bot_user_id):
        return False
    return bool((ev.text or "").strip())
