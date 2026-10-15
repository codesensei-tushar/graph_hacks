"""Clear one workspace's channel state on disconnect.

The rollout, beachhead and channel-observe halves of company-brain's
``resetSlackWorkspaceState``: drop their Redis state and cancel their schedules so a
reinstall starts clean.
"""

from src.core import scheduler
from src.db.falkordb_client import kv
from src.domain.slack.channels.observe import _CURSOR, _PROBE, _RETRY, _ROWS, _SCAN, JOB_RUN_CHANNEL_OBSERVE
from src.domain.slack.channels.rollout import JOB_RUN_BEACHHEAD, JOB_RUN_ROLLOUT


def _delete_matching(pattern: str) -> None:
    for key in kv().scan_iter(match=pattern, count=500):
        kv().delete(key)


def _drop_hash_fields(key: str, prefix: str) -> None:
    for field in list(kv().hkeys(key) or []):
        name = field.decode() if isinstance(field, bytes) else field
        if name.startswith(prefix):
            kv().hdel(key, field)


def reset_channel_state(team_id: str) -> None:
    for job in (JOB_RUN_ROLLOUT, JOB_RUN_BEACHHEAD, JOB_RUN_CHANNEL_OBSERVE):
        scheduler.cancel_matching(job, lambda payload: payload.get("team_id") == team_id)
    _delete_matching(f"brain:rollout:{team_id}*")
    _delete_matching(f"brain:observe:pending:{team_id}:*")
    _delete_matching(f"brain:observe:batch:{team_id}:*")
    for key in (_ROWS, _CURSOR, _SCAN, _RETRY, _PROBE):
        _drop_hash_fields(key, f"{team_id}:")
    _delete_matching(f"brain:slack:bot-channels:{team_id}")
    _delete_matching(f"brain:bot-thread:{team_id}:*")


def register() -> None:
    from src.domain.slack.workspace import register_workspace_reset_hook

    register_workspace_reset_hook(reset_channel_state)
