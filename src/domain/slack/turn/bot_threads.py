"""Threads the bot has spoken in, and per-thread mutes.

Port of the ``brain_bot_thread`` table helpers in company-brain ``brain/slack/turn.ts``
(``markBotThread``, ``hasBotThread``, ``isThreadMuted``, ``setThreadMuted``,
``getThreadMutedBy``, ``clearThreadMuted``). One Redis hash per thread.
"""

from src.core.config import settings
from src.db.falkordb_client import kv
from src.domain.slack.turn._state import now_ms

MUTE_OFFER_FOOTER = "React :black_square_for_stop: to mute me in this thread"


def muted_footer(muted_by: str) -> str:
    return f"Muted by <@{muted_by}> — mention me anytime"


def _key(team_id: str, channel: str, thread_ts: str) -> str:
    return f"brain:bot-thread:{team_id}:{channel}:{thread_ts}"


def _str(value) -> str | None:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


def mark_bot_thread(team_id: str, channel: str, thread_ts: str) -> None:
    key = _key(team_id, channel, thread_ts)
    kv().hset(key, "updated_at", now_ms())
    kv().expire(key, settings.bot_thread_ttl_seconds)


def has_bot_thread(team_id: str, channel: str | None, thread_ts: str | None) -> bool:
    if not channel or not thread_ts:
        return False
    return bool(kv().exists(_key(team_id, channel, thread_ts)))


def is_thread_muted(team_id: str, channel: str, thread_ts: str) -> bool:
    return bool(_str(kv().hget(_key(team_id, channel, thread_ts), "muted_at")))


def set_thread_muted(team_id: str, channel: str, thread_ts: str, muted_by: str) -> None:
    """First mute wins: a second reactor never takes over the mute."""
    key = _key(team_id, channel, thread_ts)
    now = now_ms()
    kv().hsetnx(key, "updated_at", now)
    kv().hsetnx(key, "muted_at", now)
    kv().hsetnx(key, "muted_by", muted_by)
    kv().expire(key, settings.bot_thread_ttl_seconds)


def get_thread_muted_by(team_id: str, channel: str, thread_ts: str) -> str | None:
    key = _key(team_id, channel, thread_ts)
    if not _str(kv().hget(key, "muted_at")):
        return None
    return _str(kv().hget(key, "muted_by"))


def clear_thread_muted(team_id: str, channel: str, thread_ts: str) -> None:
    kv().hdel(_key(team_id, channel, thread_ts), "muted_at", "muted_by")
