"""Where a reply goes. Port of company-brain ``brain/slack/routing.ts``."""

import logging
import re
from dataclasses import dataclass

from src.integrations.slack.events import SlackEventInner

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SlackReplyTarget:
    channel: str
    thread_ts: str


def _is_slack_channel_id(channel_id: str) -> bool:
    return re.match(r"^[CGD]", channel_id) is not None


def resolve_slack_reply_target(client, ev: SlackEventInner) -> SlackReplyTarget | None:
    channel = (ev.assistant_thread.channel_id if ev.assistant_thread else None) or ev.channel or ""
    thread_ts = ev.thread_ts or (ev.assistant_thread.thread_ts if ev.assistant_thread else None) or ev.ts or ""

    if not _is_slack_channel_id(channel) and ev.user:
        opened = client.open_conversation(ev.user)
        if opened:
            channel = opened

    if not _is_slack_channel_id(channel) or not thread_ts:
        log.warning("[slack] unable to resolve reply target channel=%s thread=%s user=%s", ev.channel, ev.thread_ts or ev.ts, ev.user)
        return None
    return SlackReplyTarget(channel=channel, thread_ts=thread_ts)
