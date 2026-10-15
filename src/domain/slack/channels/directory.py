"""Port of company-brain ``brain/slack/channel-directory.ts``.

The channels the bot is in, cached for ten minutes, plus channel-reference
resolution and the access gate for reading another channel on someone's behalf.
"""

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

from src.db.falkordb_client import kv
from src.domain.slack.channels._compat import field

CHANNELS_CACHE_TTL_SECONDS = 10 * 60


@dataclass(frozen=True)
class SlackConversation:
    id: str
    name: str
    is_private: bool


ChannelDirectory = list[SlackConversation]


def _cache_key(team_id: str) -> str:
    return f"brain:slack:bot-channels:{team_id}"


def _conversation(item: Any) -> SlackConversation | None:
    channel_id, name = field(item, "id"), field(item, "name")
    if not channel_id or not name:
        return None
    return SlackConversation(id=channel_id, name=name, is_private=bool(field(item, "is_private", False)))


def invalidate_channel_directory(team_id: str) -> None:
    try:
        kv().delete(_cache_key(team_id))
    except Exception:
        pass


def get_channel_directory(client, team_id: str | None, *, force_refresh: bool = False) -> ChannelDirectory:
    cache_key = _cache_key(team_id) if team_id else None
    if not force_refresh and cache_key:
        try:
            cached = kv().get(cache_key)
            if cached:
                parsed = json.loads(cached)
                if isinstance(parsed, list):
                    return [SlackConversation(**row) for row in parsed]
        except Exception:
            pass
    channels = [c for c in (_conversation(item) for item in client.get_bot_conversations()) if c]
    if cache_key and channels:
        try:
            kv().set(cache_key, json.dumps([asdict(c) for c in channels]), ex=CHANNELS_CACHE_TTL_SECONDS)
        except Exception:
            pass
    return channels


def parse_channel_ref(ref: str) -> dict[str, str | None]:
    t = ref.strip()
    mention = re.match(r"^<#([A-Z0-9]+)(?:\|([^>]*))?>$", t, re.IGNORECASE)
    if mention:
        label = (mention.group(2) or "").strip()
        return {"id": mention.group(1), "name": label or None}
    if re.match(r"^[CGD][A-Z0-9]{5,}$", t, re.IGNORECASE):
        return {"id": t, "name": None}
    return {"id": None, "name": re.sub(r"^[#@]", "", t)}


def resolve_channel_ref(directory: ChannelDirectory, ref: str) -> SlackConversation | None:
    parsed = parse_channel_ref(ref)
    if parsed["id"]:
        for channel in directory:
            if channel.id == parsed["id"]:
                return channel
    if parsed["name"]:
        lower = parsed["name"].lower()
        for channel in directory:
            if channel.name.lower() == lower:
                return channel
    return None


@dataclass(frozen=True)
class ChannelResolution:
    status: Literal["ok", "not_member", "unknown"]
    channel: SlackConversation | None = None
    name: str | None = None
    is_private: bool = False
    ref: str | None = None


def resolve_channel(client, team_id: str | None, ref: str) -> ChannelResolution:
    directory = get_channel_directory(client, team_id)
    cached = resolve_channel_ref(directory, ref)
    if cached:
        return ChannelResolution(status="ok", channel=cached)

    parsed = parse_channel_ref(ref)
    if parsed["id"]:
        info = client.get_slack_conversation_info(parsed["id"])
        if field(info, "id") and field(info, "name"):
            channel = SlackConversation(id=field(info, "id"), name=field(info, "name"), is_private=bool(field(info, "is_private", False)))
            if field(info, "is_member", False):
                return ChannelResolution(status="ok", channel=channel)
            return ChannelResolution(status="not_member", name=channel.name, is_private=channel.is_private)
        return ChannelResolution(status="unknown", ref=ref)

    if parsed["name"]:
        fresh = get_channel_directory(client, team_id, force_refresh=True)
        hit = resolve_channel_ref(fresh, ref)
        if hit:
            return ChannelResolution(status="ok", channel=hit)
    return ChannelResolution(status="unknown", ref=ref)


def _query_tokens(query: str) -> set[str]:
    return {t for t in re.split(r"\s+", re.sub(r"[^a-z0-9\s]", " ", query.lower())) if len(t) > 2}


def find_channels_named_in_query(directory: ChannelDirectory, query: str, limit: int) -> list[SlackConversation]:
    q_tokens = _query_tokens(query)
    if not q_tokens:
        return []
    scored = []
    for channel in directory:
        score = sum(1 for token in re.split(r"[-_\s]+", channel.name.lower()) if token in q_tokens)
        if score > 0:
            scored.append((channel, score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return [channel for channel, _ in scored][:limit]


def rank_channels_for_query(directory: ChannelDirectory, query: str, limit: int) -> list[SlackConversation]:
    matched = find_channels_named_in_query(directory, query, limit)
    if len(matched) >= limit:
        return matched[:limit]
    picked = list(matched)
    backfill = sorted(directory, key=lambda c: int(c.is_private))
    for channel in backfill:
        if len(picked) >= limit:
            break
        if channel not in picked:
            picked.append(channel)
    return picked[:limit]


ChannelSearchDenial = Literal[
    "not_org_member",
    "private_channel_requires_membership",
    "private_channel_non_dm_response",
    "unknown_asker",
]
SlackResponseSurface = Literal["public_channel", "private_channel", "dm"]


@dataclass(frozen=True)
class ChannelSearchAccess:
    ok: bool
    reason: ChannelSearchDenial | None = None


def check_asker_can_search_channel(
    client,
    team_id: str | None,
    channel: SlackConversation,
    asker_slack_user_id: str | None,
    *,
    current_channel_id: str | None = None,
    is_org_member: bool = False,
    response_surface: SlackResponseSurface | None = None,
    response_channel_id: str | None = None,
    asker_is_restricted: bool = False,
) -> ChannelSearchAccess:
    """``response_channel_id``: a future response delivered into this channel is not a leak.
    ``asker_is_restricted``: Slack guests only see channels they're in, even public ones."""
    is_current_channel = channel.id == current_channel_id
    if not is_current_channel and not is_org_member:
        return ChannelSearchAccess(ok=False, reason="not_org_member")

    # Full members can read any public channel, but guests only see channels
    # they've been added to, so verify membership for a guest even on public.
    guest_needs_membership = asker_is_restricted and not is_current_channel
    if not channel.is_private and not guest_needs_membership:
        return ChannelSearchAccess(ok=True)
    if not asker_slack_user_id:
        return ChannelSearchAccess(ok=False, reason="unknown_asker")

    members = client.get_conversation_members(channel.id)
    if asker_slack_user_id not in members:
        return ChannelSearchAccess(ok=False, reason="private_channel_requires_membership")
    # Private-channel content must not be echoed outside that channel or a DM.
    if (
        channel.is_private
        and not is_current_channel
        and response_surface != "dm"
        and response_channel_id != channel.id
    ):
        return ChannelSearchAccess(ok=False, reason="private_channel_non_dm_response")
    return ChannelSearchAccess(ok=True)
