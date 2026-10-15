"""Port of company-brain `brain/slack/turn-inbox.ts`.

Follow-up messages that arrive while a thread turn is running. Each is reserved as
``classifying``, resolved by the active-turn gate to ``pending`` or ``ignored``, and
drained into the running turn as ``applied``.

    brain:thread-turn-update:{thread_key}  field message_ts -> row (brain_thread_turn_update)
"""

import re
import time
from dataclasses import asdict, dataclass

from src.db.falkordb_client import kv
from src.domain.slack.turn._state import LOCK, hget_json, hgetall_json, hset_json, now_ms

# Matches turn_control's retention; updates are meaningless once their turn is gone.
INBOX_RETENTION_SECONDS = 30 * 24 * 60 * 60
_RECENT_DUPLICATE_WINDOW_MS = 30_000


@dataclass
class TurnInboxUpdate:
    thread_key: str
    turn_id: str
    revision: int
    message_ts: str
    author_user: str
    author_name: str | None
    instruction: str
    outcome: str  # ActiveTurnGateResult: ignore | append | replace
    status: str  # classifying | pending | applied | ignored
    created_at: int
    applied_at: int | None


def _key(thread_key: str) -> str:
    return f"brain:thread-turn-update:{thread_key}"


def _normalized_instruction(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower()).strip()


def _save(update: dict) -> None:
    key = _key(update["thread_key"])
    hset_json(key, update["message_ts"], update)
    kv().expire(key, INBOX_RETENTION_SECONDS)


def _all(thread_key: str) -> list[dict]:
    return list(hgetall_json(_key(thread_key)).values())


def _ts_order(update: dict) -> tuple[int, int]:
    seconds, _, fraction = update["message_ts"].partition(".")
    return int(seconds or 0), int(fraction or 0)


def _turn_is_active(thread_key: str, turn_id: str, revision: int) -> bool:
    from src.domain.slack.turn.turn_control import get_thread_turn

    row = get_thread_turn(thread_key)
    return bool(row) and row.turn_id == turn_id and row.revision == revision and row.status in ("running", "waiting_approval")


def reserve_turn_update(
    *,
    thread_key: str,
    turn_id: str,
    revision: int,
    message_ts: str,
    author_user: str,
    instruction: str,
    author_name: str | None = None,
) -> str:
    """Returns ``reserved``, ``duplicate`` or ``inactive``."""
    with LOCK:
        if not _turn_is_active(thread_key, turn_id, revision):
            return "inactive"
        if hget_json(_key(thread_key), message_ts) is not None:
            return "duplicate"
        cutoff = now_ms() - _RECENT_DUPLICATE_WINDOW_MS
        recent = [
            row
            for row in _all(thread_key)
            if row["turn_id"] == turn_id
            and row["revision"] == revision
            and row["status"] in ("classifying", "pending", "applied")
            and row["created_at"] >= cutoff
        ]
        duplicate = any(
            row["author_user"] == author_user and _normalized_instruction(row["instruction"]) == _normalized_instruction(instruction)
            for row in recent
        )
        now = now_ms()
        _save(
            asdict(
                TurnInboxUpdate(
                    thread_key=thread_key,
                    turn_id=turn_id,
                    revision=revision,
                    message_ts=message_ts,
                    author_user=author_user,
                    author_name=author_name,
                    instruction=instruction,
                    outcome="ignore" if duplicate else "append",
                    status="ignored" if duplicate else "classifying",
                    created_at=now,
                    applied_at=now if duplicate else None,
                )
            )
        )
        return "duplicate" if duplicate else "reserved"


def discard_turn_update_reservation(*, thread_key: str, turn_id: str, revision: int, message_ts: str) -> None:
    with LOCK:
        row = hget_json(_key(thread_key), message_ts)
        if row and row["turn_id"] == turn_id and row["revision"] == revision and row["status"] == "classifying":
            kv().hdel(_key(thread_key), message_ts)


def resolve_turn_update(*, thread_key: str, message_ts: str, outcome: str, status: str) -> bool:
    """``status`` is ``pending`` or ``ignored``. True when the row now has that status."""
    with LOCK:
        row = hget_json(_key(thread_key), message_ts)
        if row and row["status"] == "classifying":
            row["outcome"] = outcome
            row["status"] = status
            row["applied_at"] = now_ms() if status == "ignored" else None
            _save(row)
        return bool(row) and row["status"] == status


def list_current_turn_updates(*, thread_key: str, turn_id: str, revision: int) -> list[TurnInboxUpdate]:
    rows = [
        row
        for row in _all(thread_key)
        if row["turn_id"] == turn_id and row["revision"] == revision and row["status"] in ("pending", "applied")
    ]
    return [TurnInboxUpdate(**row) for row in sorted(rows, key=_ts_order)]


def consume_pending_turn_updates(control) -> list[TurnInboxUpdate]:
    with LOCK:
        updates = sorted(
            (
                row
                for row in _all(control.thread_key)
                if row["turn_id"] == control.turn_id and row["revision"] == control.revision and row["status"] == "pending"
            ),
            key=_ts_order,
        )
        if not updates:
            return []
        applied_at = now_ms()
        for row in updates:
            row["status"] = "applied"
            row["applied_at"] = applied_at
            _save(row)
        return [TurnInboxUpdate(**row) for row in updates]


def has_classifying_turn_updates(control) -> bool:
    return any(
        row["turn_id"] == control.turn_id and row["revision"] == control.revision and row["status"] == "classifying"
        for row in _all(control.thread_key)
    )


def wait_for_turn_update_classification(control, abort_signal=None, timeout_ms: int = 10_000) -> None:
    deadline = now_ms() + timeout_ms
    while has_classifying_turn_updates(control) and now_ms() < deadline:
        if abort_signal is not None and abort_signal.is_set():
            return
        time.sleep(0.025)
    if not has_classifying_turn_updates(control):
        return
    # A stuck classifier must not strand the turn. Preserve the fail-open policy by
    # converting unresolved reservations into pending APPEND updates.
    with LOCK:
        for row in _all(control.thread_key):
            if row["turn_id"] == control.turn_id and row["revision"] == control.revision and row["status"] == "classifying":
                row["outcome"] = "append"
                row["status"] = "pending"
                _save(row)


def format_turn_updates(updates: list[TurnInboxUpdate]) -> str:
    lines = ["<live_thread_updates>"]
    for update in updates:
        author = update.author_name or update.author_user
        if update.outcome == "replace":
            lines.append(
                f"{author} proposed a conflicting follow-up. Do not replace the original request; "
                f"finish it, then address this separately: {update.instruction}"
            )
        else:
            lines.append(f"{author}: {update.instruction}")
    lines.append("</live_thread_updates>")
    return "\n".join(lines)
