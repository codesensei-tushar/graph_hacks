"""Whether everyone who can read a Slack conversation is a full member of the workspace.

``org_internal`` (connector data: Jira assignees, CRM accounts) is for full members of the
org's workspaces only. An answer posted in a channel is read by everyone in it, so a turn
reads ``org_internal`` only when the room's whole audience is internal:

- a DM: the asker is a full member;
- a channel or group DM: it is not shared with another organisation (Slack Connect,
  ``is_ext_shared``), and every member is a full member of this workspace: no guest
  (``is_restricted``/``is_ultra_restricted``), no external user (``is_stranger``, or simply
  not in this team's directory).

Every lookup failure answers False (fail closed). Results are cached in kv for two minutes:
the team directory per team, the verdict per channel. ``forget`` drops them at once:

- ``member_joined_channel`` / ``member_left_channel``: that channel's verdict;
- ``channel_shared`` / ``channel_unshared`` (Slack Connect): that channel's verdict;
- ``team_join`` / ``user_change`` (someone joined, became a guest, was deactivated): the team's
  directory and **every** channel verdict of the team, through a per-team epoch in the verdict keys.

A channel of ``MAX_CHANNEL_MEMBERS`` (5,000) people or more, in a team that has any guest or
external account, is always treated as external: its member list is not read whole.
"""

import json
import logging

from src.db.falkordb_client import kv

log = logging.getLogger(__name__)

CACHE_SECONDS = 2 * 60
MAX_DIRECTORY_PAGES = 50  # 10,000 people
MAX_CHANNEL_MEMBERS = 5000


def _directory(client, team_id: str) -> tuple[frozenset[str], bool] | None:
    """(full members of the team, whether the team has any guest or external account), or None on failure."""
    key = f"brain:slack:directory:{team_id}"
    try:
        cached = kv().get(key)
        if cached:
            data = json.loads(cached)
            return frozenset(data["members"]), bool(data["outsiders"])
    except Exception:
        pass
    members: set[str] = set()
    outsiders = False
    cursor = None
    for _ in range(MAX_DIRECTORY_PAGES):
        page = client.list_slack_users_page(cursor)
        if page is None or not getattr(page, "ok", False):
            log.warning("[slack] directory unavailable team=%s: %s", team_id, getattr(page, "error", "no answer"))
            return None
        for member in page.items:
            foreign = bool(getattr(member, "team_id", None)) and member.team_id != team_id
            if getattr(member, "deleted", False):
                continue
            if member.is_restricted or member.is_ultra_restricted or member.is_stranger or foreign:
                outsiders = True
            else:
                members.add(member.id)
        if page.complete:
            break
        cursor = page.next_cursor
    else:
        log.warning("[slack] directory of team=%s is larger than %d pages; treated as unknown", team_id, MAX_DIRECTORY_PAGES)
        return None
    try:
        kv().set(key, json.dumps({"members": sorted(members), "outsiders": outsiders}), ex=CACHE_SECONDS)
    except Exception:
        pass
    return frozenset(members), outsiders


def _epoch_key(team_id: str) -> str:
    return f"brain:slack:audience-epoch:{team_id}"


def _verdict_key(team_id: str, channel: str) -> str:
    """The cached verdict's key, under the team's current epoch: a new epoch leaves every old one unread."""
    epoch = kv().get(_epoch_key(team_id)) or "0"
    return f"brain:slack:audience:{team_id}:{epoch}:{channel}"


def forget(team_id: str, channel: str | None = None) -> None:
    """With ``channel``: drop that channel's verdict (its members or its sharing changed). Without: drop the
    team's directory and every channel verdict of the team (a person joined, left or changed kind)."""
    try:
        if channel:
            kv().delete(_verdict_key(team_id, channel), f"brain:slack:directory:{team_id}")
        else:
            kv().delete(f"brain:slack:directory:{team_id}")
            kv().incr(_epoch_key(team_id))
            kv().expire(_epoch_key(team_id), 7 * 24 * 60 * 60)
    except Exception:
        log.warning("[slack] audience cache not cleared team=%s", team_id)


def is_full_member(client, team_id: str, slack_user_id: str | None) -> bool:
    if not slack_user_id:
        return False
    try:
        directory = _directory(client, team_id)
    except Exception:
        log.exception("[slack] directory lookup failed team=%s", team_id)
        return False
    return directory is not None and slack_user_id in directory[0]


def is_internal_audience(client, team_id: str, channel: str, info=None, *, is_dm: bool = False, asker: str | None = None) -> bool:
    """True only when everyone who reads ``channel`` is a full member of ``team_id``. Fails closed."""
    try:
        if asker and not is_full_member(client, team_id, asker):
            return False
        if is_dm:
            return bool(asker)
        return _channel_is_internal(client, team_id, channel, info)
    except Exception:
        log.exception("[slack] audience check failed team=%s channel=%s; treated as external", team_id, channel)
        return False


def _channel_is_internal(client, team_id: str, channel: str, info) -> bool:
    if not channel:
        return False
    try:
        key = _verdict_key(team_id, channel)
        cached = kv().get(key)
        if cached is not None:
            return (cached.decode() if isinstance(cached, bytes) else cached) == "1"
    except Exception:
        key = None
    # The live lookup, not the cached identity: is_ext_shared is what this check is for.
    info = client.get_slack_conversation_info(channel)
    if info is None:
        return False
    if getattr(info, "is_ext_shared", None) is not False:
        verdict = False
    else:
        directory = _directory(client, team_id)
        if directory is None:
            return False
        members, outsiders = directory
        if not outsiders:
            # Not shared, and the team has no guest or external account: everyone in the channel is a member.
            verdict = True
        else:
            in_channel = client.get_conversation_members(channel, max_members=MAX_CHANNEL_MEMBERS)
            if not in_channel or len(in_channel) >= MAX_CHANNEL_MEMBERS:
                return False
            verdict = all(user in members for user in in_channel)
    try:
        if key:
            kv().set(key, "1" if verdict else "0", ex=CACHE_SECONDS)
    except Exception:
        pass
    return verdict
