"""Port of company-brain `brain/slack/message-trace.ts`.

Maps a bot message (channel + ts) to the trace id of the turn that produced it, so
a debug reaction can surface that trace. The DO SQL table becomes a Redis hash per
channel.

``build_thread_context`` renders the thread with trace ids attached. The renderer
itself (``formatThread``) belongs to the prompt layer, which arrives with the agent,
so it is passed in.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from src.db.falkordb_client import kv
from src.integrations.slack.client import SlackThreadMessage

# Matches generateId() output (22-char base58-ish nanoid).
BRAIN_TRACE_ID_RE = re.compile(r"[123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz]{22}")

# formatThread keeps at most 30 rendered messages. A wider raw-message window leaves
# room for blank/system messages without querying every entry of a long thread.
TRACE_LOOKUP_MESSAGE_LIMIT = 60


@dataclass(frozen=True)
class BrainMessageTrace:
    trace_id: str
    # Slack requires replies and ephemerals to target the parent message rather than
    # an arbitrary reply timestamp. None only for legacy records.
    thread_ts: str | None = None


def _key(channel: str) -> str:
    return f"brain:slack:message-trace:{channel}"


def record_message_trace(channel: str, message_ts: str, trace_id: str, thread_ts: str | None = None) -> None:
    if not channel or not message_ts or not trace_id:
        return
    previous = kv().hget(_key(channel), message_ts)
    previous_thread = json.loads(previous).get("thread_ts") if previous else None
    kv().hset(_key(channel), message_ts, json.dumps({"trace_id": trace_id, "thread_ts": thread_ts or previous_thread}))


def lookup_message_trace_records(channel: str, message_ts: list[str]) -> dict[str, BrainMessageTrace]:
    tss = list(dict.fromkeys(ts for ts in message_ts if ts))
    if not tss:
        return {}
    out: dict[str, BrainMessageTrace] = {}
    for ts, raw in zip(tss, kv().hmget(_key(channel), tss), strict=True):
        if not raw:
            continue
        row = json.loads(raw)
        if not row.get("trace_id"):
            continue
        out[ts] = BrainMessageTrace(trace_id=row["trace_id"], thread_ts=row.get("thread_ts") or None)
    return out


def lookup_message_traces(channel: str, message_ts: list[str]) -> dict[str, str]:
    return {ts: record.trace_id for ts, record in lookup_message_trace_records(channel, message_ts).items()}


def is_our_slack_bot_message(message: dict, bot_user_id: str | None, slack_bot_id: str | None = None) -> bool:
    if bot_user_id and message.get("user") == bot_user_id:
        return True
    return bool(slack_bot_id and message.get("bot_id") == slack_bot_id)


def build_thread_context(
    format_thread: Callable[..., str],
    channel: str,
    thread: list[SlackThreadMessage],
    bot_user_id: str | None,
    slack_bot_id: str | None,
    exclude_ts: str | None,
    user_names: dict[str, str] | None = None,
    tz_offset_seconds: int | None = None,
    group_handles: dict[str, str] | None = None,
    bot_user_ids: set[str] | None = None,
) -> str:
    bot_ts = [
        m["ts"]
        for m in thread[-TRACE_LOOKUP_MESSAGE_LIMIT:]
        if m.get("ts") and is_our_slack_bot_message(m, bot_user_id, slack_bot_id)
    ]
    trace_by_ts = lookup_message_traces(channel, bot_ts)
    return format_thread(
        thread,
        bot_user_id,
        exclude_ts,
        trace_by_ts,
        user_names,
        tz_offset_seconds,
        group_handles,
        slack_bot_id,
        bot_user_ids,
    )
