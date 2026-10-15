"""Port of company-brain `brain/slack/event-store.ts`.

A bounded local copy of recent Slack messages per channel, used as triage and turn
context, plus the per-message triage audit (decision, claim lease, suppression,
outcome) and the per-scope history-warmup state.

The DO SQLite tables become Redis hashes:

    brain:slack-event:{team}:{channel}    field event_ts  -> row (brain_slack_event)
    brain:slack-context:{team}:{channel}  field scope_key -> row (brain_slack_context_state)
    brain:slack-event:channels            set of "team|channel" with stored events
    brain:slack-event:maintenance         last retention sweep (brain_slack_event_maintenance)

Each channel holds at most ``SLACK_EVENT_RING_MAX_PER_CHANNEL`` rows, so the SQL
filters run in Python over one small hash. ``event_ts`` ordering is string order,
exactly as the SQLite TEXT column sorted.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from src.db.falkordb_client import kv
from src.domain.slack.turn._state import LOCK, hdel, hgetall_json, hget_json, hset_json, now_ms, smembers

log = logging.getLogger(__name__)

EVENT_TEXT_MAX_CHARS = 2_000
SLACK_EVENT_RING_MAX_PER_CHANNEL = 50
TRIAGE_CLAIM_LEASE_MS = 2 * 60 * 1_000
CONTEXT_COMPLETENESS_VERSION = 1
EVENT_RETENTION_MS = 7 * 24 * 60 * 60 * 1_000
CLEANUP_INTERVAL_MS = 60 * 60 * 1_000
CONTEXT_BACKFILL_RETRY_MS = 60 * 60 * 1_000
CONTEXT_GAP_BACKFILL_MS = 6 * 60 * 60 * 1_000

_CHANNELS_KEY = "brain:slack-event:channels"
_MAINTENANCE_KEY = "brain:slack-event:maintenance"

# StoredTriageDecision: filtered | explicit | pass | ack | answer | investigate
# StoredTriagePriority: summons | urgent | normal | low


@dataclass(frozen=True)
class SlackEventIdentity:
    team_id: str
    channel: str
    message_ts: str
    thread_ts: str | None = None


@dataclass(frozen=True)
class StoredTriageClaim:
    id: str
    claimed_at: int


@dataclass
class StoredSlackEventAudit:
    fallback_used: bool
    decision: str | None = None
    priority: str | None = None
    reason: str | None = None
    emoji: str | None = None
    fallback_emoji: str | None = None
    suppression: str | None = None
    action_outcome: str | None = None
    trace_id: str | None = None


@dataclass
class LocalThreadContextSnapshot:
    messages: list[dict]
    truncated: bool
    retained_complete: bool


def _events_key(team_id: str, channel: str) -> str:
    return f"brain:slack-event:{team_id}:{channel}"


def _context_key(team_id: str, channel: str) -> str:
    return f"brain:slack-context:{team_id}:{channel}"


def _field(ev: Any, name: str) -> Any:
    if isinstance(ev, dict):
        return ev.get(name)
    value = getattr(ev, name, None)
    if value is None and getattr(ev, "model_extra", None):
        value = ev.model_extra.get(name)
    return value


def _as_dict(value: Any) -> dict | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return dict(value)


def _bounded_text(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    return text[:EVENT_TEXT_MAX_CHARS]


def _nested_payload(ev: Any) -> dict:
    subtype = _field(ev, "subtype")
    message = _as_dict(_field(ev, "message"))
    previous = _as_dict(_field(ev, "previous_message"))
    if subtype == "message_changed" and message:
        return message
    if subtype == "message_deleted" and previous:
        return previous
    return _as_dict(ev) or {}


def _normalized_thread_ts(message_ts: str, thread_ts: str | None) -> str | None:
    """A thread parent may carry thread_ts == ts; store it as channel-level context."""
    return thread_ts if thread_ts and thread_ts != message_ts else None


def slack_event_identity(team_id: str, ev: Any) -> SlackEventIdentity | None:
    payload = _nested_payload(ev)
    channel = _field(ev, "channel")
    message_ts = _field(ev, "deleted_ts") or payload.get("ts") or _field(ev, "ts")
    if not channel or not message_ts:
        return None
    return SlackEventIdentity(
        team_id=team_id,
        channel=channel,
        message_ts=message_ts,
        thread_ts=_normalized_thread_ts(message_ts, payload.get("thread_ts")),
    )


def _rows(team_id: str, channel: str) -> dict[str, dict]:
    return hgetall_json(_events_key(team_id, channel))


def _update_context_state(team_id: str, channel: str, scope_keys: set[str]) -> None:
    """UPDATE ... SET history_complete = 0 for existing scope rows only."""
    key = _context_key(team_id, channel)
    for scope_key in scope_keys:
        row = hget_json(key, scope_key)
        if row is None:
            continue
        row["history_complete"] = 0
        row["history_complete_version"] = CONTEXT_COMPLETENESS_VERSION
        hset_json(key, scope_key, row)


def _thread_scope_keys(rows: dict[str, dict], dropped: list[dict]) -> set[str]:
    reply_parents = {row.get("thread_ts") for row in rows.values() if row.get("thread_ts")}
    keys = set()
    for row in dropped:
        if row.get("thread_ts"):
            keys.add(f"thread:{row['thread_ts']}")
        elif row["event_ts"] in reply_parents:
            keys.add(f"thread:{row['event_ts']}")
    return keys


def _maybe_prune_event_store(team_id: str, channel: str, now: int) -> None:
    """Keep the store physically bounded: a ring per channel plus a 7-day retention sweep."""
    rows = _rows(team_id, channel)
    ordered = sorted(rows, reverse=True)
    pruned = [rows[ts] for ts in ordered[SLACK_EVENT_RING_MAX_PER_CHANNEL:]]
    if pruned:
        _update_context_state(team_id, channel, _thread_scope_keys(rows, pruned))
        hdel(_events_key(team_id, channel), *(row["event_ts"] for row in pruned))

    last = kv().get(_MAINTENANCE_KEY)
    if last is not None and now - int(last) < CLEANUP_INTERVAL_MS:
        return
    cutoff = now - EVENT_RETENTION_MS
    for member in smembers(_CHANNELS_KEY):
        member_team, member_channel = member.split("|", 1)
        channel_rows = _rows(member_team, member_channel)
        stale = [row for row in channel_rows.values() if row["received_at"] < cutoff]
        if stale:
            _update_context_state(member_team, member_channel, _thread_scope_keys(channel_rows, stale))
            hdel(_events_key(member_team, member_channel), *(row["event_ts"] for row in stale))
        if len(stale) == len(channel_rows):
            kv().srem(_CHANNELS_KEY, member)
    kv().set(_MAINTENANCE_KEY, now)


def record_slack_event(team_id: str, ev: Any, now: int | None = None) -> SlackEventIdentity | None:
    """Persist or refresh the local representation of a Slack message event."""
    now = now or now_ms()
    identity = slack_event_identity(team_id, ev)
    if identity is None:
        return None
    payload = _nested_payload(ev)
    subtype = _field(ev, "subtype")
    is_deleted = subtype == "message_deleted"
    is_bot = bool(
        payload.get("bot_id")
        or payload.get("app_id")
        or _field(ev, "bot_id")
        or _field(ev, "app_id")
        or payload.get("subtype") == "bot_message"
    )
    text = None if is_deleted else _bounded_text(payload.get("text"))
    incoming = {
        "event_ts": identity.message_ts,
        "thread_ts": identity.thread_ts,
        "user_id": payload.get("user") or _field(ev, "user"),
        "bot_id": payload.get("bot_id") or _field(ev, "bot_id"),
        "app_id": payload.get("app_id") or _field(ev, "app_id"),
        "is_bot": 1 if is_bot else 0,
        "text": text,
        "subtype": subtype or payload.get("subtype"),
        "is_deleted": 1 if is_deleted else 0,
    }
    key = _events_key(identity.team_id, identity.channel)
    with LOCK:
        existing = hget_json(key, identity.message_ts)
        if existing is None:
            row = {
                **incoming,
                "received_at": now,
                "updated_at": now,
                **_empty_triage(),
            }
        else:
            row = dict(existing)
            for column in ("thread_ts", "user_id", "bot_id", "app_id", "subtype"):
                row[column] = incoming[column] if incoming[column] is not None else existing.get(column)
            row["is_bot"] = max(incoming["is_bot"], existing.get("is_bot", 0))
            if incoming["is_deleted"] or existing.get("is_deleted"):
                row["text"] = None
            else:
                row["text"] = incoming["text"] if incoming["text"] is not None else existing.get("text")
            row["is_deleted"] = max(incoming["is_deleted"], existing.get("is_deleted", 0))
            if existing.get("triage_decision") == "historical":
                row["triage_decision"] = None
                row["triage_source"] = None
            row["updated_at"] = now
        hset_json(key, identity.message_ts, row)
        kv().sadd(_CHANNELS_KEY, f"{identity.team_id}|{identity.channel}")
        _maybe_prune_event_store(identity.team_id, identity.channel, now)
    return identity


def _empty_triage() -> dict:
    return {
        "triage_decision": None,
        "triage_source": None,
        "triage_priority": None,
        "triage_reason": None,
        "triage_emoji": None,
        "triage_fallback_emoji": None,
        "suppression": None,
        "fallback_used": 0,
        "action_outcome": None,
        "trace_id": None,
        "triage_claim_id": None,
        "triage_claimed_at": None,
    }


def _update_row(identity: SlackEventIdentity, mutate) -> bool:
    key = _events_key(identity.team_id, identity.channel)
    with LOCK:
        row = hget_json(key, identity.message_ts)
        if row is None:
            return False
        if mutate(row) is False:
            return False
        hset_json(key, identity.message_ts, row)
        return True


def mark_stored_event_filtered(identity: SlackEventIdentity, reason: str, claim_id: str | None = None) -> bool:
    return record_stored_triage_decision(
        identity,
        decision="filtered",
        source="structural_filter",
        reason=reason,
        claim_id=claim_id,
    )


def claim_stored_event_for_triage(identity: SlackEventIdentity, now: int | None = None) -> StoredTriageClaim | None:
    """Atomically lease an unjudged message before starting its model call.

    A stale lease may be reclaimed, while the unique id prevents the former owner
    from publishing a decision after ownership changes.
    """
    now = now or now_ms()
    claim_id = str(uuid.uuid4())

    def mutate(row: dict) -> bool:
        decision = row.get("triage_decision")
        claimed_at = row.get("triage_claimed_at")
        claimable = decision is None or (
            decision == "judging" and (claimed_at is None or claimed_at <= now - TRIAGE_CLAIM_LEASE_MS)
        )
        if not claimable:
            return False
        row.update(_empty_triage())
        row["triage_decision"] = "judging"
        row["triage_claim_id"] = claim_id
        row["triage_claimed_at"] = now
        row["updated_at"] = now
        return True

    return StoredTriageClaim(id=claim_id, claimed_at=now) if _update_row(identity, mutate) else None


def record_stored_triage_decision(
    identity: SlackEventIdentity,
    *,
    decision: str,
    claim_id: str | None = None,
    source: str | None = None,
    priority: str | None = None,
    reason: str | None = None,
    emoji: str | None = None,
    fallback_emoji: str | None = None,
    trace_id: str | None = None,
) -> bool:
    def mutate(row: dict) -> bool:
        if claim_id is not None and not (row.get("triage_decision") == "judging" and row.get("triage_claim_id") == claim_id):
            return False
        row["triage_decision"] = decision
        row["triage_source"] = source
        row["triage_priority"] = priority
        row["triage_reason"] = reason
        row["triage_emoji"] = emoji
        row["triage_fallback_emoji"] = fallback_emoji
        row["trace_id"] = trace_id if trace_id is not None else row.get("trace_id")
        row["triage_claim_id"] = None
        row["triage_claimed_at"] = None
        row["updated_at"] = now_ms()
        return True

    return _update_row(identity, mutate)


def record_stored_suppression(
    identity: SlackEventIdentity,
    *,
    suppression: str,
    fallback_used: bool = False,
    outcome: str | None = None,
) -> None:
    def mutate(row: dict) -> None:
        row["suppression"] = suppression
        row["fallback_used"] = 1 if fallback_used else 0
        row["action_outcome"] = outcome or "suppressed"
        row["updated_at"] = now_ms()

    _update_row(identity, mutate)
    log.info(
        "[company-brain] proactivity suppressed reason=%s team=%s channel=%s message=%s fallback=%s",
        suppression,
        identity.team_id,
        identity.channel,
        identity.message_ts,
        fallback_used is True,
    )


def record_stored_action_outcome(identity: SlackEventIdentity, outcome: str) -> None:
    def mutate(row: dict) -> None:
        row["action_outcome"] = outcome
        row["updated_at"] = now_ms()

    _update_row(identity, mutate)


def _rows_to_slack_messages(rows: list[dict]) -> list[dict]:
    messages = []
    for row in rows:
        message: dict = {"ts": row["event_ts"]}
        for column, field in (
            ("user_id", "user"),
            ("text", "text"),
            ("thread_ts", "thread_ts"),
            ("bot_id", "bot_id"),
            ("app_id", "app_id"),
            ("subtype", "subtype"),
        ):
            if row.get(column):
                message[field] = row[column]
        if not row.get("bot_id") and not row.get("app_id") and row.get("is_bot"):
            message["bot_id"] = "local-context-bot"
        messages.append(message)
    return messages


def _select(team_id: str, channel: str, predicate, limit: int) -> list[dict]:
    """SELECT ... WHERE predicate ORDER BY event_ts DESC LIMIT n, returned oldest first."""
    rows = [row for row in _rows(team_id, channel).values() if predicate(row)]
    rows.sort(key=lambda row: row["event_ts"], reverse=True)
    return list(reversed(rows[:limit]))


def load_local_channel_context(*, team_id: str, channel: str, before_ts: str, limit: int = 20) -> list[dict]:
    rows = _select(
        team_id,
        channel,
        lambda row: (row.get("thread_ts") is None or row.get("thread_ts") == row["event_ts"])
        and not row.get("is_deleted")
        and row["event_ts"] < before_ts,
        limit,
    )
    return _rows_to_slack_messages(rows)


def load_local_thread_context(*, team_id: str, channel: str, thread_ts: str, before_ts: str, limit: int = 50) -> list[dict]:
    rows = _select(
        team_id,
        channel,
        lambda row: not row.get("is_deleted")
        and row["event_ts"] < before_ts
        and (row["event_ts"] == thread_ts or row.get("thread_ts") == thread_ts),
        limit,
    )
    return _rows_to_slack_messages(rows)


def get_previous_human_channel_activity_at(identity: SlackEventIdentity) -> int | None:
    """Most recent prior human activity in the channel, excluding this event."""
    rows = _select(
        identity.team_id,
        identity.channel,
        lambda row: row["event_ts"] < identity.message_ts and not row.get("is_bot") and not row.get("is_deleted"),
        1,
    )
    return rows[0]["received_at"] if rows else None


def _context_scope_key(thread_ts: str | None) -> str:
    return f"thread:{thread_ts}" if thread_ts else "channel"


def _slack_timestamp_ms(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value) * 1_000
    except ValueError:
        return None


def has_local_context_gap(current_message_ts: str | None, previous_message_ts: str | None) -> bool:
    """A visible six-hour discontinuity is the signal that event delivery may have had a gap."""
    current = _slack_timestamp_ms(current_message_ts)
    previous = _slack_timestamp_ms(previous_message_ts)
    return current is not None and previous is not None and current - previous > CONTEXT_GAP_BACKFILL_MS


def _latest_context_event_before(*, team_id: str, channel: str, thread_ts: str | None, current_message_ts: str) -> str | None:
    if thread_ts:
        rows = _select(
            team_id,
            channel,
            lambda row: not row.get("is_deleted")
            and row["event_ts"] < current_message_ts
            and (row["event_ts"] == thread_ts or row.get("thread_ts") == thread_ts),
            1,
        )
    else:
        rows = _select(
            team_id,
            channel,
            lambda row: (row.get("thread_ts") is None or row.get("thread_ts") == row["event_ts"])
            and not row.get("is_deleted")
            and row["event_ts"] < current_message_ts,
            1,
        )
    return rows[0]["event_ts"] if rows else None


def is_local_context_history_complete(*, team_id: str, channel: str, thread_ts: str | None = None) -> bool:
    row = hget_json(_context_key(team_id, channel), _context_scope_key(thread_ts))
    return bool(row) and row.get("history_complete") == 1 and row.get("history_complete_version") == CONTEXT_COMPLETENESS_VERSION


def claim_local_context_warmup(
    *,
    team_id: str,
    channel: str,
    thread_ts: str | None = None,
    current_message_ts: str | None = None,
    now: int | None = None,
) -> bool:
    """Claim an initial or visible-gap Slack history warmup for a local context scope."""
    now = now or now_ms()
    key = _context_key(team_id, channel)
    scope_key = _context_scope_key(thread_ts)
    with LOCK:
        row = hget_json(key, scope_key)
        previous_message_ts = (
            _latest_context_event_before(team_id=team_id, channel=channel, thread_ts=thread_ts, current_message_ts=current_message_ts)
            if current_message_ts
            else None
        )
        warm = bool(row) and row.get("warm") == 1
        needs_gap_backfill = warm and has_local_context_gap(current_message_ts, previous_message_ts)
        # A thread can stay marked warm after the channel ring evicts its older rows.
        # A current reply with no prior local thread event is a cache miss, not an empty thread.
        needs_missing_thread_backfill = bool(warm and thread_ts and current_message_ts and not previous_message_ts)
        if row and row.get("warm") and not needs_gap_backfill and not needs_missing_thread_backfill:
            return False
        if row and not needs_missing_thread_backfill and now - row.get("last_backfill_at", 0) < CONTEXT_BACKFILL_RETRY_MS:
            return False
        hset_json(
            key,
            scope_key,
            {
                "warm": 0,
                "history_complete": 0,
                "history_complete_version": CONTEXT_COMPLETENESS_VERSION,
                "last_backfill_at": now,
            },
        )
        return True


def mark_local_context_warm(*, team_id: str, channel: str, thread_ts: str | None = None, history_complete: bool | None = None) -> None:
    hset_json(
        _context_key(team_id, channel),
        _context_scope_key(thread_ts),
        {
            "warm": 1,
            "history_complete": 0 if history_complete is False else 1,
            "history_complete_version": CONTEXT_COMPLETENESS_VERSION,
            "last_backfill_at": now_ms(),
        },
    )


def _merge_slack_history_rows_into_local_context(*, team_id: str, channel: str, messages: list[dict]) -> None:
    key = _events_key(team_id, channel)
    now = now_ms()
    for message in messages:
        ts = message.get("ts")
        if not ts:
            continue
        received_at = _slack_timestamp_ms(ts)
        existing = hget_json(key, ts)
        is_bot = 1 if (message.get("bot_id") or message.get("app_id") or message.get("subtype") == "bot_message") else 0
        incoming = {
            "thread_ts": _normalized_thread_ts(ts, message.get("thread_ts")),
            "user_id": message.get("user"),
            "bot_id": message.get("bot_id"),
            "app_id": message.get("app_id"),
            "text": _bounded_text(message.get("text")),
            "subtype": message.get("subtype"),
        }
        if existing is None:
            row = {
                "event_ts": ts,
                **incoming,
                "is_bot": is_bot,
                "is_deleted": 0,
                "received_at": int(received_at) if received_at is not None else now,
                "updated_at": now,
                **_empty_triage(),
                "triage_decision": "historical",
                "triage_source": "slack_backfill",
            }
        elif existing.get("is_deleted"):
            continue
        else:
            # Live rows win; history only fills gaps.
            row = dict(existing)
            for column, value in incoming.items():
                if row.get(column) is None:
                    row[column] = value
            row["is_bot"] = max(row.get("is_bot", 0), is_bot)
        hset_json(key, ts, row)
    kv().sadd(_CHANNELS_KEY, f"{team_id}|{channel}")


def merge_slack_history_into_local_context(*, team_id: str, channel: str, messages: list[dict]) -> None:
    with LOCK:
        _merge_slack_history_rows_into_local_context(team_id=team_id, channel=channel, messages=messages)
        _maybe_prune_event_store(team_id, channel, now_ms())


def merge_slack_thread_history_into_local_context(
    *,
    team_id: str,
    channel: str,
    thread_ts: str,
    before_ts: str,
    messages: list[dict],
    limit: int,
) -> LocalThreadContextSnapshot:
    """Merge a thread read and capture the bounded prompt window before the ring prunes it.

    ``retained_complete`` says whether that same complete window survived pruning and
    is therefore safe to reuse on a later event without another Slack read.
    """
    limit = max(1, int(limit))
    with LOCK:
        _merge_slack_history_rows_into_local_context(team_id=team_id, channel=channel, messages=messages)
        before_prune = load_local_thread_context(team_id=team_id, channel=channel, thread_ts=thread_ts, before_ts=before_ts, limit=limit + 1)
        truncated = len(before_prune) > limit
        window = before_prune[-limit:] if truncated else before_prune
        _maybe_prune_event_store(team_id, channel, now_ms())
        retained = load_local_thread_context(team_id=team_id, channel=channel, thread_ts=thread_ts, before_ts=before_ts, limit=limit + 1)
    retained_complete = (
        not truncated
        and len(retained) == len(window)
        and all(message["ts"] == retained[index]["ts"] for index, message in enumerate(window))
    )
    return LocalThreadContextSnapshot(messages=window, truncated=truncated, retained_complete=retained_complete)


def get_stored_slack_event_audit(identity: SlackEventIdentity) -> StoredSlackEventAudit | None:
    row = hget_json(_events_key(identity.team_id, identity.channel), identity.message_ts)
    if row is None:
        return None
    return StoredSlackEventAudit(
        decision=row.get("triage_decision"),
        priority=row.get("triage_priority"),
        reason=row.get("triage_reason"),
        emoji=row.get("triage_emoji"),
        fallback_emoji=row.get("triage_fallback_emoji"),
        suppression=row.get("suppression"),
        fallback_used=row.get("fallback_used") == 1,
        action_outcome=row.get("action_outcome"),
        trace_id=row.get("trace_id"),
    )
