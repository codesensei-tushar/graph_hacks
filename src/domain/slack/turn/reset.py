"""Clear one workspace's turn state on disconnect.

Upstream ``resetSlackWorkspaceState`` clears the org DO's Slack state and cancels
Slack-owned schedules. Org-wide counters (chime budget, investigations) are keyed by
channel id, which is unique across workspaces, so they are left to age out.
"""

from src.db.falkordb_client import kv
from src.domain.slack.turn._state import LOCK, hdel, hgetall_json


def _delete_matching(pattern: str) -> int:
    deleted = 0
    for key in kv().scan_iter(match=pattern, count=500):
        deleted += kv().delete(key)
    return deleted


def reset_turn_state(team_id: str) -> None:
    from src.domain.slack.turn.event_store import _CHANNELS_KEY
    from src.domain.slack.turn.reaction_queue import _OUTBOX, _UNIQUE, _unique_key

    with LOCK:
        _delete_matching(f"brain:slack-event:{team_id}:*")
        _delete_matching(f"brain:slack-context:{team_id}:*")
        for member in list(kv().smembers(_CHANNELS_KEY) or []):
            value = member.decode() if isinstance(member, bytes) else member
            if value.startswith(f"{team_id}|"):
                kv().srem(_CHANNELS_KEY, member)
        _delete_matching(f"brain:thread-turn:{team_id}:*")
        _delete_matching(f"brain:thread-turn-update:{team_id}:*")
        for row in hgetall_json(_OUTBOX).values():
            if row["team_id"] == team_id:
                hdel(_OUTBOX, str(row["id"]))
                hdel(_UNIQUE, _unique_key(row["team_id"], row["channel_id"], row["message_ts"], row["emoji"]))


def register() -> None:
    try:
        from src.domain.slack.workspace import register_workspace_reset_hook
    except ImportError:
        return
    register_workspace_reset_hook(reset_turn_state)
