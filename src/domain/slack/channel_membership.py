"""Which Slack users belong to which private channels.

Port of company-brain ``brain/slack/channel-membership.ts``. The DO SQL tables become
FalkorDB edges, so a personal DM can read the memory of every private channel the
asker belongs to:

    (SlackUser)-[:MEMBER_OF {updated_at}]->(Channel {is_private: true, backfilled_at})
"""

import logging
import time

from src.db.falkordb_client import connect
from src.domain.memory.writeback import private_slack_channel_container_tag
from src.integrations.slack.client import SlackClient
from src.integrations.slack.events import SlackEventInner

log = logging.getLogger(__name__)


def _now() -> int:
    return int(time.time() * 1000)


def record_membership(channel_id: str, slack_user_id: str, now: int | None = None, is_private: bool | None = True) -> None:
    """``is_private`` is set on the channel only when known (None leaves it as recorded)."""
    _, graph = connect()
    graph.query(
        """
        MERGE (c:Channel {id: $channel})
        SET c.is_private = coalesce($private, c.is_private)
        MERGE (u:SlackUser {id: $user})
        MERGE (u)-[r:MEMBER_OF]->(c)
        SET r.updated_at = $now
        """,
        {"channel": channel_id, "user": slack_user_id, "now": now or _now(), "private": is_private},
    )


def remove_membership(channel_id: str, slack_user_id: str) -> None:
    _, graph = connect()
    graph.query(
        "MATCH (:SlackUser {id: $user})-[r:MEMBER_OF]->(:Channel {id: $channel}) DELETE r",
        {"channel": channel_id, "user": slack_user_id},
    )


def purge_channel(channel_id: str) -> None:
    """Bot left or lost access: nothing should surface this channel in a DM anymore."""
    _, graph = connect()
    graph.query("MATCH (:SlackUser)-[r:MEMBER_OF]->(:Channel {id: $channel}) DELETE r", {"channel": channel_id})
    graph.query("MATCH (c:Channel {id: $channel}) SET c.backfilled_at = NULL", {"channel": channel_id})


def _members_on_record(channel_id: str) -> list[str]:
    _, graph = connect()
    rows = graph.query(
        "MATCH (u:SlackUser)-[:MEMBER_OF]->(:Channel {id: $channel}) RETURN u.id",
        {"channel": channel_id},
    ).result_set
    return [row[0] for row in rows]


def backfill_channel_members(client: SlackClient, channel_id: str, now: int | None = None) -> int:
    """Authoritative snapshot via conversations.members.

    Upserts everyone in the channel and removes anyone still listed who left, which
    corrects dropped member_left_channel events. A private channel always has at
    least one member (the bot), so an empty result is a failed fetch: skip the
    rewrite so a transient Slack error can't wipe legitimate access.
    """
    now = now or _now()
    members = client.conversation_members(channel_id)
    if not members:
        log.warning("[membership] backfill skipped channel=%s: no members returned", channel_id)
        return 0
    current = set(members)
    for user_id in _members_on_record(channel_id):
        if user_id not in current:
            remove_membership(channel_id, user_id)
    for member in members:
        record_membership(channel_id, member, now)
    _, graph = connect()
    graph.query("MATCH (c:Channel {id: $channel}) SET c.backfilled_at = $now", {"channel": channel_id, "now": now})
    return len(members)


def resolve_channel_privacy(client: SlackClient, channel_id: str) -> bool | None:
    info = client.conversation_info(channel_id)
    if info is not None and info.name:
        remember_channel_name(channel_id, info.name, bool(info.is_private))
    return info.is_private if info is not None and isinstance(info.is_private, bool) else None


def remember_channel_name(channel_id: str, name: str, is_private: bool) -> None:
    _, graph = connect()
    graph.query(
        "MERGE (c:Channel {id: $channel}) SET c.name = $name, c.is_private = $private",
        {"channel": channel_id, "name": name, "private": is_private},
    )


def run_slack_membership_event(client: SlackClient, bot_user_id: str | None, ev: SlackEventInner) -> None:
    """member_joined_channel / member_left_channel. Slack only sends these for channels the bot is in."""
    channel, user = ev.channel, ev.user
    if not channel or not user:
        return
    is_join = ev.type == "member_joined_channel"
    is_leave = ev.type == "member_left_channel"
    if not is_join and not is_leave:
        return
    if resolve_channel_privacy(client, channel) is not True:
        return
    is_bot = bool(bot_user_id) and user == bot_user_id
    now = _now()

    if is_leave:
        if is_bot:
            purge_channel(channel)
            log.info("[membership] bot left private channel=%s; purged", channel)
        else:
            remove_membership(channel, user)
            log.info("[membership] leave channel=%s user=%s", channel, user)
        return

    if is_bot:
        count = backfill_channel_members(client, channel, now)
        log.info("[membership] bot joined private channel=%s; backfilled %d members", channel, count)
    else:
        record_membership(channel, user, now)
        log.info("[membership] join channel=%s user=%s", channel, user)


def reconcile_channel_membership(client: SlackClient) -> None:
    """Safety net: re-enumerate every private channel the bot is in, then prune channels it left."""
    private_channels = client.bot_private_channels()
    now = _now()
    keep = set()
    for channel in private_channels:
        if not channel.id:
            continue
        keep.add(channel.id)
        if channel.name:
            remember_channel_name(channel.id, channel.name, True)
        backfill_channel_members(client, channel.id, now)

    _, graph = connect()
    known = graph.query("MATCH (:SlackUser)-[:MEMBER_OF]->(c:Channel) RETURN DISTINCT c.id").result_set
    pruned = 0
    for (channel_id,) in known:
        if channel_id not in keep:
            purge_channel(channel_id)
            pruned += 1
    log.info("[membership] reconciled private_channels=%d pruned=%d", len(private_channels), pruned)


def readable_slack_channel_container_tags_for_user(slack_user_id: str | None, limit: int = 1000) -> list[str]:
    """Every private-channel container this Slack user can read, most recently active first."""
    if not (slack_user_id or "").strip():
        return []
    try:
        _, graph = connect()
        rows = graph.query(
            """
            MATCH (:SlackUser {id: $user})-[r:MEMBER_OF]->(c:Channel {is_private: true})
            RETURN c.id ORDER BY r.updated_at DESC LIMIT $limit
            """,
            {"user": slack_user_id, "limit": limit},
        ).result_set
    except Exception:
        log.exception("[membership] readable channel lookup failed user=%s", slack_user_id)
        return []
    return [private_slack_channel_container_tag(row[0]) for row in rows]


def channel_names(channel_ids: list[str]) -> dict[str, str]:
    if not channel_ids:
        return {}
    _, graph = connect()
    rows = graph.query(
        "MATCH (c:Channel) WHERE c.id IN $ids AND c.name IS NOT NULL RETURN c.id, c.name",
        {"ids": channel_ids},
    ).result_set
    return {channel_id: name for channel_id, name in rows}
