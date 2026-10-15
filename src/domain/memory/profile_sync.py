"""Port of company-brain `brain/memory/profile-sync.ts`.

Keeps each container's entity context and profile buckets current (on its Scope
node), naming it unless someone renamed it. Throttled to once a day per scope with
Redis keys that expire, in place of the DO ``brain_profile_sync`` table.
"""

import logging
from dataclasses import dataclass

from src.db.falkordb_client import kv
from src.domain.memory.containers import (
    AGENT_SELF_CONTAINER_TAG,
    SHARED_TEAM_BRAIN_CONTAINER_TAG,
    ProfileBucketDef,
    get_container_tag_settings,
    private_container_tag_for,
    private_slack_channel_container_tag,
    update_container_tag_settings,
)
from src.domain.memory.profile_config import (
    BRAIN_MEMORY_BUCKETS,
    BRAIN_SELF_BUCKETS,
    build_brain_personal_entity_context,
    build_brain_private_channel_entity_context,
    build_brain_self_entity_context,
    build_brain_shared_entity_context,
)
from src.domain.memory.space_name import (
    is_generated_brain_space_name,
    is_slack_managed_brain_space_name,
    slack_channel_space_name,
)

log = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 24 * 60 * 60
_SYNC_PREFIX = "brain:profile_sync:"


def _needs_sync(scope_key: str) -> bool:
    return not kv().exists(f"{_SYNC_PREFIX}{scope_key}")


def _mark_synced(scope_key: str) -> None:
    kv().set(f"{_SYNC_PREFIX}{scope_key}", "1", ex=SYNC_INTERVAL_SECONDS)


def clear_brain_profile_sync() -> None:
    """Drop the throttle so provisioning re-runs immediately after a reset."""
    keys = list(kv().scan_iter(match=f"{_SYNC_PREFIX}*"))
    if keys:
        kv().delete(*keys)


def _upsert_brain_space_config(
    container_tag: str,
    name: str,
    entity_context: str,
    profile_buckets: list[ProfileBucketDef],
    track_slack_managed_name: bool = False,
) -> None:
    existing = get_container_tag_settings(container_tag)
    keep_name = bool(
        existing
        and existing.name
        and not is_generated_brain_space_name(existing.name, container_tag)
        and not (
            track_slack_managed_name
            and is_slack_managed_brain_space_name(existing.name, container_tag, name, None)
        )
    )
    update_container_tag_settings(
        container_tag,
        name=None if keep_name else name,
        entity_context=entity_context,
        profile_buckets=profile_buckets,
    )


@dataclass
class BrainProfileSyncContext:
    org_name: str
    domain: str | None = None
    about: str | None = None
    # The member who sent the message, if resolved: (user_id, name).
    asker_user_id: str | None = None
    asker_name: str | None = None
    # Present when the turn happened in a private channel.
    private_channel_id: str | None = None
    private_channel_name: str | None = None


def maybe_sync_brain_profile_config(ctx: BrainProfileSyncContext) -> None:
    """Refresh this turn's container configs. Best-effort: failures are logged, never raised."""
    jobs = [
        (
            "shared",
            lambda: _upsert_brain_space_config(
                SHARED_TEAM_BRAIN_CONTAINER_TAG,
                "Team Brain",
                build_brain_shared_entity_context(ctx.org_name, ctx.domain, ctx.about),
                BRAIN_MEMORY_BUCKETS,
            ),
        ),
        (
            "self",
            lambda: _upsert_brain_space_config(AGENT_SELF_CONTAINER_TAG, "Agent Self", build_brain_self_entity_context(), BRAIN_SELF_BUCKETS),
        ),
    ]
    if ctx.asker_user_id:
        jobs.append(
            (
                f"user:{ctx.asker_user_id}",
                lambda: _upsert_brain_space_config(
                    private_container_tag_for(ctx.asker_user_id),
                    "My Brain",
                    build_brain_personal_entity_context(ctx.asker_name),
                    BRAIN_MEMORY_BUCKETS,
                ),
            )
        )
    if ctx.private_channel_id:
        jobs.append(
            (
                f"channel:{ctx.private_channel_id}",
                lambda: _upsert_brain_space_config(
                    private_slack_channel_container_tag(ctx.private_channel_id),
                    slack_channel_space_name(ctx.private_channel_id, ctx.private_channel_name),
                    build_brain_private_channel_entity_context(ctx.private_channel_name),
                    BRAIN_MEMORY_BUCKETS,
                    track_slack_managed_name=True,
                ),
            )
        )
    for scope_key, job in jobs:
        if not _needs_sync(scope_key):
            continue
        try:
            job()
            _mark_synced(scope_key)
        except Exception:
            log.exception("[brain-profile-sync] scope=%s failed", scope_key)
