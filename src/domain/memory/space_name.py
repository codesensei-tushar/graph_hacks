"""Port of company-brain `brain/memory/space-name.ts`. Display names for Slack-managed containers."""

SLACK_CHANNEL_CONTAINER_TAG_PREFIX = "slack_channel_"
SLACK_CHANNEL_SPACE_NAME_PREFIX = "slack_"
SLACK_MANAGED_SPACE_NAME_METADATA_KEY = "slack_managed_space_name"


def slack_channel_space_name(channel_id: str, channel_name: str | None = None) -> str:
    return f"{SLACK_CHANNEL_SPACE_NAME_PREFIX}{(channel_name or '').strip() or channel_id}"


def is_generated_brain_space_name(name: str | None, container_tag: str, desired_name: str | None = None) -> bool:
    normalized = (name or "").strip()
    if not normalized:
        return True
    if normalized in (container_tag, f"Space {container_tag}"):
        return True
    if not container_tag.startswith(SLACK_CHANNEL_CONTAINER_TAG_PREFIX):
        return False
    channel_id = container_tag[len(SLACK_CHANNEL_CONTAINER_TAG_PREFIX):]
    if normalized in (channel_id, f"#{channel_id}", f"{SLACK_CHANNEL_SPACE_NAME_PREFIX}{channel_id}"):
        return True
    # The short-lived `#channel-name` convention migrates to `slack_channel-name`.
    if desired_name and desired_name.startswith(SLACK_CHANNEL_SPACE_NAME_PREFIX):
        return normalized == f"#{desired_name[len(SLACK_CHANNEL_SPACE_NAME_PREFIX):]}"
    return False


def slack_managed_brain_space_name(metadata: object) -> str | None:
    if not isinstance(metadata, dict):
        return None
    managed = metadata.get(SLACK_MANAGED_SPACE_NAME_METADATA_KEY)
    return managed if isinstance(managed, str) and managed else None


def is_slack_managed_brain_space_name(name: str | None, container_tag: str, desired_name: str, metadata: object) -> bool:
    managed = slack_managed_brain_space_name(metadata)
    if managed is not None:
        return name == managed
    return name == desired_name or is_generated_brain_space_name(name, container_tag, desired_name)
