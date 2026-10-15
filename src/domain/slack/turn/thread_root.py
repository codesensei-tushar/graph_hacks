"""Port of company-brain `brain/slack/public-channel-thread-root.ts`."""


def is_slack_history_thread_root(message: dict) -> bool:
    """True for Slack thread parents that should receive conversations.replies."""
    if (message.get("reply_count") or 0) <= 0:
        return False
    if not message.get("ts"):
        return False
    # Roots often have thread_ts == ts; only exclude true replies (thread_ts != ts).
    return not message.get("thread_ts") or message.get("thread_ts") == message.get("ts")
