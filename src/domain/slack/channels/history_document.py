"""Port of company-brain ``brain/slack/history-document.ts``.

Packs a public channel's history window into deterministic daily documents for
``org_shared``. The rollout writes them with ``upsert_brain_document``, which
upserts by ``custom_id`` (company-brain's full-replace write).
"""

import hashlib
import re
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from typing import Any

from src.domain.memory.containers import SHARED_TEAM_BRAIN_CONTAINER_TAG
from src.domain.memory.tags import BRAIN_TAG_LABELS_METADATA_KEY, BRAIN_TAGS_METADATA_KEY, normalize_brain_tag_key
from src.domain.slack.channels._compat import as_dict, normalize_slack_message_content

SLACK_HISTORY_DOCUMENT_VERSION = 1
SLACK_HISTORY_DOCUMENT_MAX_CHARS = 30_000


@dataclass
class SlackHistoryDocument:
    custom_id: str
    content: str
    container_tag: str
    metadata: dict[str, Any]
    date: str
    part: int


@dataclass
class SlackHistoryDocumentInput:
    team_id: str
    channel_id: str
    channel_name: str
    window_start_ms: int
    window_end_ms: int
    messages: list[Any] = dc_field(default_factory=list)
    topic: str | None = None
    purpose: str | None = None
    bot_user_id: str | None = None


def _timestamp_ms(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        return round(float(ts) * 1000)
    except ValueError:
        return None


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def _iso_timestamp(ts: str) -> str:
    ms = _timestamp_ms(ts)
    return ts if ms is None else _iso(ms)


def _utc_date(ts: str) -> str:
    ms = _timestamp_ms(ts)
    return "unknown-date" if ms is None else _iso(ms)[:10]


def _clean_text(text: str | None) -> str:
    return (text or "").replace("\r\n", "\n").replace("\x00", "").strip()


def _file_summary(message: dict) -> str:
    names = [f["name"].strip() for f in (message.get("files") or []) if (f.get("name") or "").strip()][:8]
    return f"[Files: {', '.join(names)}]" if names else ""


def _reaction_summary(message: dict) -> str:
    reactions = [
        f":{r['name'].strip()}:×{max(r.get('count') or 1, 1)}"
        for r in (message.get("reactions") or [])
        if (r.get("name") or "").strip()
    ][:12]
    return f" [Reactions: {' '.join(reactions)}]" if reactions else ""


def _is_substantive(message: dict, bot_user_id: str | None) -> bool:
    if not message.get("ts"):
        return False
    if bot_user_id and message.get("user") == bot_user_id:
        return False
    text = _clean_text(normalize_slack_message_content(message))
    if not text and not message.get("files"):
        return False
    subtype = message.get("subtype")
    if subtype and subtype not in ("bot_message", "file_share", "thread_broadcast"):
        return False
    return True


def _speaker(message: dict) -> str:
    if message.get("user"):
        return f"<@{message['user']}>"
    if message.get("app_id"):
        return f"Slack app {message['app_id']}"
    if message.get("bot_id"):
        return f"Slack bot {message['bot_id']}"
    return "Unknown speaker"


def _message_line(message: dict, is_reply: bool) -> str:
    text = _clean_text(normalize_slack_message_content(message))
    body = " ".join(part for part in (text, _file_summary(message)) if part)
    prefix = "  ↳ " if is_reply else "- "
    return f"{prefix}{_iso_timestamp(message.get('ts') or '')} · {_speaker(message)}: {body}{_reaction_summary(message)}"


@dataclass
class _ThreadUnit:
    date: str
    earliest_ts: str
    latest_ts: str
    text: str


def _build_thread_units(doc_input: SlackHistoryDocumentInput) -> list[_ThreadUnit]:
    by_timestamp: dict[str, dict] = {}
    for raw in doc_input.messages:
        message = as_dict(raw)
        if not _is_substantive(message, doc_input.bot_user_id):
            continue
        ms = _timestamp_ms(message.get("ts"))
        if ms is None or ms < doc_input.window_start_ms or ms > doc_input.window_end_ms:
            continue
        by_timestamp[message["ts"]] = message
    by_root: dict[str, list[dict]] = {}
    for message in by_timestamp.values():
        root = message.get("thread_ts") or message.get("ts")
        if root:
            by_root.setdefault(root, []).append(message)

    units = []
    for root_ts, messages in by_root.items():
        messages.sort(key=lambda m: m.get("ts") or "")
        root = next((m for m in messages if m.get("ts") == root_ts), None)
        ordered = [root] + [m for m in messages if m is not root] if root else messages
        earliest = ordered[0].get("ts") or root_ts
        latest = ordered[-1].get("ts") or earliest
        units.append(
            _ThreadUnit(
                date=_utc_date(earliest),
                earliest_ts=earliest,
                latest_ts=latest,
                text="\n".join(_message_line(m, i > 0) for i, m in enumerate(ordered)),
            )
        )
    units.sort(key=lambda u: u.earliest_ts)
    return units


def _header(doc_input: SlackHistoryDocumentInput, date: str) -> str:
    lines = [
        f"# Slack history: #{doc_input.channel_name}",
        f"CHANNEL_ID: {doc_input.channel_id}",
        f"DOCUMENT_DATE_UTC: {date}",
        f"CAPTURE_WINDOW_UTC: {_iso(doc_input.window_start_ms)} — {_iso(doc_input.window_end_ms)}",
        f"TOPIC: {doc_input.topic.strip()}" if (doc_input.topic or "").strip() else "",
        f"PURPOSE: {doc_input.purpose.strip()}" if (doc_input.purpose or "").strip() else "",
        "",
    ]
    return "\n".join(line for i, line in enumerate(lines) if line or i == len(lines) - 1)


def _split_oversized_unit(unit: _ThreadUnit, max_chars: int) -> list[_ThreadUnit]:
    if len(unit.text) <= max_chars:
        return [unit]
    chunks: list[_ThreadUnit] = []
    current = ""
    for line in unit.text.split("\n"):
        clipped = line[:max_chars]
        if current and len(current) + len(clipped) + 1 > max_chars:
            chunks.append(_ThreadUnit(unit.date, unit.earliest_ts, unit.latest_ts, current))
            current = ""
        current = f"{current}\n{clipped}" if current else clipped
    if current:
        chunks.append(_ThreadUnit(unit.date, unit.earliest_ts, unit.latest_ts, current))
    return chunks


def _content_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def build_slack_history_documents(
    doc_input: SlackHistoryDocumentInput,
    max_chars: int = SLACK_HISTORY_DOCUMENT_MAX_CHARS,
) -> list[SlackHistoryDocument]:
    """Pack threads oldest-to-newest into deterministic daily documents.

    Participant ids stay inline but are not applied as document-level person tags,
    which would over-scope every extracted fact.
    """
    by_date: dict[str, list[_ThreadUnit]] = {}
    for unit in _build_thread_units(doc_input):
        by_date.setdefault(unit.date, []).extend(_split_oversized_unit(unit, max(max_chars - 1_000, 1_000)))

    documents: list[SlackHistoryDocument] = []
    channel_tag = normalize_brain_tag_key(f"slack_channel_{doc_input.channel_id}")
    for date, units in sorted(by_date.items()):
        prefix = _header(doc_input, date)
        state = {"part": 1, "body": "", "earliest": "", "latest": ""}

        def flush() -> None:
            if not state["body"]:
                return
            # Frozen window bounds keep a later partial-day recapture from reusing
            # this custom id and overwriting a fuller prior document.
            identity = ":".join(
                str(x)
                for x in (
                    SLACK_HISTORY_DOCUMENT_VERSION,
                    doc_input.team_id,
                    doc_input.channel_id,
                    date,
                    state["part"],
                    doc_input.window_start_ms,
                    doc_input.window_end_ms,
                )
            )
            part = state["part"]
            documents.append(
                SlackHistoryDocument(
                    custom_id=f"slackhist_v{SLACK_HISTORY_DOCUMENT_VERSION}_{_content_hash(identity)[:32]}",
                    content=f"{prefix}{state['body']}",
                    container_tag=SHARED_TEAM_BRAIN_CONTAINER_TAG,
                    date=date,
                    part=part,
                    metadata={
                        "source_type": "slack_channel_history",
                        "slack_team_id": doc_input.team_id,
                        "slack_channel_id": doc_input.channel_id,
                        "slack_channel_name": doc_input.channel_name,
                        "document_date": date,
                        "window_start": _iso(doc_input.window_start_ms),
                        "window_end": _iso(doc_input.window_end_ms),
                        "earliest_message_ts": state["earliest"],
                        "latest_message_ts": state["latest"],
                        "title": f"Slack #{doc_input.channel_name} — {date} (part {part})",
                        "mime_type": "text/markdown",
                        BRAIN_TAGS_METADATA_KEY: ["source_slack", channel_tag],
                        BRAIN_TAG_LABELS_METADATA_KEY: ["Slack", f"Channel #{doc_input.channel_name}"],
                    },
                )
            )
            state.update(part=part + 1, body="", earliest="", latest="")

        for unit in units:
            addition = f"{chr(10) * 2 if state['body'] else ''}{unit.text}"
            if state["body"] and len(prefix) + len(state["body"]) + len(addition) > max_chars:
                flush()
            state["body"] += f"{chr(10) * 2 if state['body'] else ''}{unit.text}"
            state["earliest"] = state["earliest"] or unit.earliest_ts
            state["latest"] = unit.latest_ts
        flush()
    return documents


def recent_slack_channel_evidence(
    messages: list[Any],
    bot_user_id: str | None = None,
    limit: int = 40,
    window: tuple[int, int] | None = None,
) -> str:
    """Compact, channel-local evidence for theme extraction and introductions."""
    picked = []
    for raw in messages:
        message = as_dict(raw)
        if not _is_substantive(message, bot_user_id):
            continue
        if window:
            ms = _timestamp_ms(message.get("ts"))
            if ms is None or ms < window[0] or ms > window[1]:
                continue
        picked.append(message)
    picked.sort(key=lambda m: m.get("ts") or "")
    return "\n".join(_message_line(m, bool(m.get("thread_ts"))) for m in picked[-limit:])
