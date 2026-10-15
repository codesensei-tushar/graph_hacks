"""Port of company-brain `brain/slack/debug-trace-delivery.ts`."""

from typing import Literal

from src.integrations.slack.client import SlackClient
from src.integrations.slack.events import is_direct_slack_channel

SlackDebugTraceDelivery = Literal["dm_channel", "ephemeral", "private_dm", "failed"]


def deliver_slack_debug_trace(
    client: SlackClient,
    *,
    channel: str,
    reactor_user: str,
    text: str,
    thread_ts: str | None = None,
) -> SlackDebugTraceDelivery:
    if is_direct_slack_channel(channel):
        return "dm_channel" if client.post_slack_message(channel, text, thread_ts) else "failed"

    if client.post_slack_ephemeral(channel, reactor_user, text, thread_ts):
        return "ephemeral"

    # A trace is internal observability data. If Slack cannot deliver the ephemeral,
    # fail over to the reactor's DM rather than the shared channel.
    dm = client.open_slack_conversation(reactor_user)
    return "private_dm" if dm and client.post_slack_message(dm, text) else "failed"
