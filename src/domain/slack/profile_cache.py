"""Port of company-brain `brain/slack/profile-cache.ts`.

Cached users.info / conversations.info with the same TTLs, including short negative
and transient-error caching. Their per-agent SQL tables become Redis keys; their
in-flight promise maps become a per-process single-flight lock.
"""

import dataclasses
import json
import threading

from src.db.falkordb_client import kv
from src.domain.slack.lifecycle_api import SlackMember, SlackUserInfoLookup, lookup_slack_user_info, slack_call
from src.integrations.slack.client import SlackConversationInfo

SLACK_PROFILE_CACHE_TTL_MS = 24 * 60 * 60 * 1_000
SLACK_PROFILE_NEGATIVE_CACHE_TTL_MS = 60 * 60 * 1_000
SLACK_PROFILE_TRANSIENT_ERROR_CACHE_TTL_MS = 60 * 1_000
SLACK_CHANNEL_CACHE_TTL_MS = 60 * 60 * 1_000
SLACK_CHANNEL_NEGATIVE_CACHE_TTL_MS = 60 * 60 * 1_000
SLACK_CHANNEL_TRANSIENT_ERROR_CACHE_TTL_MS = 60 * 1_000

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _single_flight(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _user_key(team_id: str, user_id: str) -> str:
    return f"brain:slack-user-cache:{team_id}:{user_id}"


def _channel_key(team_id: str, channel_id: str) -> str:
    return f"brain:slack-channel-cache:{team_id}:{channel_id}"


def _read_profile(team_id: str, user_id: str) -> SlackUserInfoLookup | None:
    raw = kv().get(_user_key(team_id, user_id))
    if not raw:
        return None
    row = json.loads(raw)
    if not row["ok"]:
        reason = row.get("reason")
        if reason not in ("missing_user_id", "user_not_found", "profile_unavailable"):
            reason = "slack_api_error"
        return SlackUserInfoLookup(ok=False, reason=reason, error=row.get("error"))
    return SlackUserInfoLookup(ok=True, user=SlackMember(**row["user"]))


def _write_profile(team_id: str, user_id: str, lookup: SlackUserInfoLookup) -> None:
    if lookup.ok:
        ttl = SLACK_PROFILE_CACHE_TTL_MS
        row = {"ok": True, "user": dataclasses.asdict(lookup.user)}
    else:
        ttl = SLACK_PROFILE_TRANSIENT_ERROR_CACHE_TTL_MS if lookup.reason == "slack_api_error" else SLACK_PROFILE_NEGATIVE_CACHE_TTL_MS
        row = {"ok": False, "reason": lookup.reason, "error": lookup.error}
    kv().set(_user_key(team_id, user_id), json.dumps(row), px=ttl)


def get_cached_slack_user_info(web, *, team_id: str, user_id: str) -> SlackUserInfoLookup:
    """Cached users.info lookup with single-flight on cold misses."""
    cached = _read_profile(team_id, user_id)
    if cached:
        return cached
    with _single_flight(f"user:{team_id}:{user_id}"):
        cached = _read_profile(team_id, user_id)
        if cached:
            return cached
        result = lookup_slack_user_info(web, user_id)
        _write_profile(team_id, user_id, result)
        return result


def get_cached_slack_user_profiles(web, *, team_id: str, user_ids: list[str]) -> dict[str, SlackMember]:
    out: dict[str, SlackMember] = {}
    for user_id in dict.fromkeys(u for u in user_ids if u):
        lookup = get_cached_slack_user_info(web, team_id=team_id, user_id=user_id)
        if lookup.ok and lookup.user:
            out[user_id] = lookup.user
    return out


def get_cached_slack_channel_info(web, *, team_id: str, channel_id: str) -> SlackConversationInfo | None:
    """conversations.info cache; cold misses are single-flight per channel."""

    def read() -> tuple[bool, SlackConversationInfo | None]:
        raw = kv().get(_channel_key(team_id, channel_id))
        if not raw:
            return False, None
        row = json.loads(raw)
        if not row["ok"]:
            return True, None
        return True, SlackConversationInfo(**row["info"])

    hit, info = read()
    if hit:
        return info
    with _single_flight(f"channel:{team_id}:{channel_id}"):
        hit, info = read()
        if hit:
            return info
        data = slack_call(web, "conversations.info", channel=channel_id)
        if data.get("ok") is True:
            channel = data.get("channel") or {}
            info = SlackConversationInfo(
                id=channel.get("id") or channel_id,
                name=channel.get("name"),
                is_private=bool(channel.get("is_private") or channel.get("is_group") or channel.get("is_mpim")),
                is_member=channel.get("is_member"),
                is_archived=channel.get("is_archived"),
                is_im=channel.get("is_im"),
            )
            known = {f.name for f in dataclasses.fields(SlackConversationInfo)}
            row = {"ok": True, "info": {k: v for k, v in dataclasses.asdict(info).items() if k in known}}
            ttl = SLACK_CHANNEL_CACHE_TTL_MS
        else:
            info = None
            row = {"ok": False}
            transient = data.get("network_error") or data.get("error") in ("ratelimited", "internal_error", "service_unavailable", "fatal_error", "request_timeout") or (data.get("http_status") or 200) >= 500
            ttl = SLACK_CHANNEL_TRANSIENT_ERROR_CACHE_TTL_MS if transient else SLACK_CHANNEL_NEGATIVE_CACHE_TTL_MS
        kv().set(_channel_key(team_id, channel_id), json.dumps(row), px=ttl)
        return info
