"""Port of company-brain ``brain/slack/public-channel-policy.ts`` and ``public-channel-thread-root.ts``."""

from typing import Any

from src.domain.slack.channels._compat import field

HOME_CHANNEL_NAME = "company-brain"


def is_eligible_public_channel(channel: Any, *, home_channel_id: str) -> bool:
    name = (field(channel, "name", "") or "").lower()
    return not (
        field(channel, "is_archived", False)
        or field(channel, "is_external", False)
        or field(channel, "is_general", False)
        or name == "general"
        or name == HOME_CHANNEL_NAME
        or field(channel, "id") == home_channel_id
    )


def is_slack_history_thread_root(message: Any) -> bool:
    """True for Slack thread parents that should receive conversations.replies."""
    if (field(message, "reply_count", 0) or 0) <= 0:
        return False
    ts = field(message, "ts")
    if not ts:
        return False
    # Roots often have thread_ts == ts; only exclude true replies (thread_ts != ts).
    thread_ts = field(message, "thread_ts")
    return not thread_ts or thread_ts == ts
