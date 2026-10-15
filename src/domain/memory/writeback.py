"""Which container a Slack memory is written to.

Port of company-brain ``brain/memory/writeback.ts`` (container tags) and
``slackMemoryScopeForTurn`` from ``brain/slack/turn.ts``. Containers are FalkorDB
Scope nodes here instead of supermemory container tags.

| Scope            | Container               |
|------------------|-------------------------|
| public channel   | org_shared              |
| DM (personal)    | user_<slack user id>    |
| private / mpim   | slack_channel_<id>      |

A message writes to exactly one container, the narrowest room it was said in.
A DM without a known user writes nothing.
"""

from dataclasses import dataclass
from typing import Literal

from src.integrations.slack.client import SlackConversationInfo
from src.integrations.slack.events import is_private_slack_channel

SHARED_TEAM_BRAIN_CONTAINER_TAG = "org_shared"

ScopeKind = Literal["shared", "dm", "private_channel"]


def private_container_tag_for(user_id: str) -> str:
    return f"user_{user_id}"


def private_slack_channel_container_tag(channel_id: str) -> str:
    return f"slack_channel_{channel_id}"


@dataclass(frozen=True)
class SlackMemoryScope:
    kind: ScopeKind
    channel_id: str | None = None
    channel_type: str | None = None
    # Person the personal write tag belongs to.
    user_id: str | None = None
    # Slack user id, used to resolve the asker's private-channel read access.
    slack_user_id: str | None = None


def slack_memory_container_tag(scope: SlackMemoryScope | None) -> str | None:
    if scope and scope.kind == "dm":
        return private_container_tag_for(scope.user_id) if scope.user_id else None
    if scope and scope.kind == "private_channel" and scope.channel_id:
        return private_slack_channel_container_tag(scope.channel_id)
    return SHARED_TEAM_BRAIN_CONTAINER_TAG


def slack_memory_scope_for_turn(
    *,
    is_dm: bool,
    channel: str,
    channel_type: str | None = None,
    user_id: str | None = None,
    slack_user_id: str | None = None,
    conversation_info: SlackConversationInfo | None = None,
) -> SlackMemoryScope:
    if is_dm:
        return SlackMemoryScope(
            kind="dm",
            channel_id=channel,
            channel_type=channel_type,
            user_id=user_id,
            slack_user_id=slack_user_id,
        )

    # Privacy signals are monotonic: a live private event wins over cached public
    # conversation info, and cached private info stays the safer scope when the
    # event is ambiguous.
    is_private = is_private_slack_channel(channel, channel_type) or (
        conversation_info is not None and conversation_info.is_private is True
    )
    if is_private:
        return SlackMemoryScope(kind="private_channel", channel_id=channel, channel_type=channel_type, user_id=user_id)
    return SlackMemoryScope(kind="shared", channel_id=channel, channel_type=channel_type)


def scope_label(tag: str, channel_names: dict[str, str] | None = None) -> str:
    """How a container is cited back to the asker."""
    if tag == SHARED_TEAM_BRAIN_CONTAINER_TAG:
        return "public channels"
    if tag.startswith("slack_channel_"):
        channel_id = tag.removeprefix("slack_channel_")
        name = (channel_names or {}).get(channel_id)
        return f"#{name}" if name else f"<#{channel_id}>"
    if tag.startswith("user_"):
        return "your DMs"
    return tag
