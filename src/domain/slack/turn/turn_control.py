"""Port of company-brain `brain/slack/turn-control.ts`.

One live turn per Slack thread, with a revision counter so a newer instruction can
cancel or supersede an older run. The row (brain_thread_turn) lives in Redis; the
abort controller is in-process, like the DO kept it in memory.

    brain:thread-turn:{thread_key} -> row
"""

import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from threading import Event

from src.db.falkordb_client import kv
from src.domain.access.approvals import APPROVAL_EXPIRY_MS
from src.domain.slack.turn._state import LOCK, get_json, now_ms

# ThreadTurnStatus: running | waiting_approval | finalizing | completed | cancelled | superseded
STEERABLE_STATUSES = {"running", "waiting_approval"}
MAX_WAITING_APPROVAL_MS = APPROVAL_EXPIRY_MS
# The DO kept rows forever per org; bound them here so idle threads expire.
THREAD_TURN_RETENTION_SECONDS = 30 * 24 * 60 * 60


@dataclass
class ThreadTurnRow:
    thread_key: str
    turn_id: str
    revision: int
    status: str
    asker_user: str
    original_question: str
    latest_instruction: str | None
    updated_at: int


@dataclass(frozen=True)
class TurnControlSnapshot:
    thread_key: str
    turn_id: str
    revision: int


@dataclass(frozen=True)
class RunningTurnControl(TurnControlSnapshot):
    # Set when the turn is aborted (AbortSignal).
    signal: Event = field(default_factory=Event, compare=False)


@dataclass(frozen=True)
class ThreadTurnStartExpectation:
    turn_id: str
    revision: int
    status: str


@dataclass
class _ActiveTurn:
    turn_id: str
    revision: int
    controller: Event
    notify_cancelled: Callable[[str], None] | None = None
    notified: bool = False


_active_turns: dict[str, _ActiveTurn] = {}


def _key(thread_key: str) -> str:
    return f"brain:thread-turn:{thread_key}"


def _save(row: ThreadTurnRow) -> None:
    kv().set(_key(row.thread_key), json.dumps(asdict(row)), ex=THREAD_TURN_RETENTION_SECONDS)


def _set_active_turn(thread_key: str, active: _ActiveTurn) -> None:
    prior = _active_turns.get(thread_key)
    if prior and prior.controller is not active.controller:
        # A previous turn on this thread was replaced without being aborted (racy
        # back-to-back follow-ups); abort it so it stops instead of running on as a zombie.
        prior.controller.set()
    _active_turns[thread_key] = active


def _new_turn_id() -> str:
    return f"turn_{_base36(now_ms())}_{uuid.uuid4()}"


def _base36(value: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while value:
        value, rem = divmod(value, 36)
        out = digits[rem] + out
    return out or "0"


def slack_thread_turn_key(team_id: str, channel: str, thread_ts: str) -> str:
    return f"{team_id}:{channel}:{thread_ts}"


def get_thread_turn(thread_key: str) -> ThreadTurnRow | None:
    row = get_json(_key(thread_key))
    return ThreadTurnRow(**row) if row else None


def is_steerable_thread_turn(row: ThreadTurnRow | None) -> bool:
    """Eviction takes the controller with it, so a missing one means orphaned, not fresh."""
    if not row or row.status not in STEERABLE_STATUSES:
        return False
    if row.status == "waiting_approval":
        return now_ms() - row.updated_at < MAX_WAITING_APPROVAL_MS
    active = _active_turns.get(row.thread_key)
    return bool(active) and active.turn_id == row.turn_id and active.revision == row.revision and not active.controller.is_set()


def thread_turn_start_expectation(row: ThreadTurnRow | None) -> ThreadTurnStartExpectation | None:
    return ThreadTurnStartExpectation(turn_id=row.turn_id, revision=row.revision, status=row.status) if row else None


def _turn_matches_start_expectation(row: ThreadTurnRow | None, expected: ThreadTurnStartExpectation | None) -> bool:
    # Once a turn has claimed finalization, its answer must reach Slack before
    # another revision can replace it.
    if row and row.status == "finalizing":
        return False
    if expected is None:
        return row is None
    return bool(row) and row.turn_id == expected.turn_id and row.revision == expected.revision and row.status == expected.status


def wait_for_thread_turn_finalization(expected: TurnControlSnapshot) -> ThreadTurnRow | None:
    while True:
        row = get_thread_turn(expected.thread_key)
        if not row or row.turn_id != expected.turn_id or row.revision != expected.revision or row.status != "finalizing":
            return row
        active = _active_turns.get(expected.thread_key)
        if not active or active.turn_id != expected.turn_id or active.revision != expected.revision:
            # A finalizing row without its in-memory turn is an orphan left by a
            # restart. Recover it so the thread can move on.
            with LOCK:
                row = get_thread_turn(expected.thread_key)
                if row and row.turn_id == expected.turn_id and row.revision == expected.revision and row.status == "finalizing":
                    row.status = "completed"
                    row.updated_at = now_ms()
                    _save(row)
            return get_thread_turn(expected.thread_key)
        time.sleep(0.025)


def begin_thread_turn(
    *,
    thread_key: str,
    asker_user: str,
    original_question: str,
    expected: ThreadTurnStartExpectation | None,
    latest_instruction: str | None = None,
) -> RunningTurnControl | None:
    with LOCK:
        existing = get_thread_turn(thread_key)
        if not _turn_matches_start_expectation(existing, expected):
            return None
        revision = (existing.revision if existing else 0) + 1
        turn_id = _new_turn_id()
        _save(
            ThreadTurnRow(
                thread_key=thread_key,
                turn_id=turn_id,
                revision=revision,
                status="running",
                asker_user=asker_user,
                original_question=original_question,
                latest_instruction=latest_instruction,
                updated_at=now_ms(),
            )
        )
        controller = Event()
        _set_active_turn(thread_key, _ActiveTurn(turn_id=turn_id, revision=revision, controller=controller))
        return RunningTurnControl(thread_key=thread_key, turn_id=turn_id, revision=revision, signal=controller)


def resume_thread_turn(snapshot: TurnControlSnapshot | None) -> RunningTurnControl | None:
    if snapshot is None or not is_thread_turn_current(snapshot):
        return None
    with LOCK:
        row = get_thread_turn(snapshot.thread_key)
        if row and row.turn_id == snapshot.turn_id and row.revision == snapshot.revision:
            row.status = "running"
            row.updated_at = now_ms()
            _save(row)
        controller = Event()
        _set_active_turn(snapshot.thread_key, _ActiveTurn(turn_id=snapshot.turn_id, revision=snapshot.revision, controller=controller))
        return RunningTurnControl(thread_key=snapshot.thread_key, turn_id=snapshot.turn_id, revision=snapshot.revision, signal=controller)


def attach_turn_cancel_notifier(control: TurnControlSnapshot, notify_cancelled: Callable[[str], None]) -> None:
    active = _active_turns.get(control.thread_key)
    if not active or active.turn_id != control.turn_id or active.revision != control.revision:
        return
    active.notify_cancelled = notify_cancelled


def is_thread_turn_current(control: TurnControlSnapshot | None) -> bool:
    if control is None:
        return True
    row = get_thread_turn(control.thread_key)
    return (
        bool(row)
        and row.turn_id == control.turn_id
        and row.revision == control.revision
        and row.status not in ("cancelled", "superseded", "completed")
    )


def _matches(row: ThreadTurnRow | None, control: TurnControlSnapshot) -> bool:
    return bool(row) and row.turn_id == control.turn_id and row.revision == control.revision


def mark_thread_turn_waiting_for_approval(control: TurnControlSnapshot) -> bool:
    if not is_thread_turn_current(control):
        return False
    with LOCK:
        _active_turns.pop(control.thread_key, None)
        row = get_thread_turn(control.thread_key)
        if row and row.status == "waiting_approval":
            return True
        if _matches(row, control) and row.status in ("running", "finalizing"):
            row.status = "waiting_approval"
            row.updated_at = now_ms()
            _save(row)
        return True


def _has_open_updates(control: TurnControlSnapshot) -> bool:
    from src.domain.slack.turn.turn_inbox import _all

    return any(
        update["turn_id"] == control.turn_id and update["revision"] == control.revision and update["status"] in ("classifying", "pending")
        for update in _all(control.thread_key)
    )


def claim_thread_turn_approval_if_inbox_empty(control: TurnControlSnapshot) -> bool:
    with LOCK:
        row = get_thread_turn(control.thread_key)
        if not _matches(row, control) or row.status != "running":
            return False
        if not _has_open_updates(control):
            row.status = "waiting_approval"
            row.updated_at = now_ms()
            _save(row)
        current = get_thread_turn(control.thread_key)
        return bool(current) and current.status == "waiting_approval"


def mark_thread_turn_completed(control: TurnControlSnapshot | None) -> None:
    if control is None or not is_thread_turn_current(control):
        return
    with LOCK:
        _active_turns.pop(control.thread_key, None)
        row = get_thread_turn(control.thread_key)
        if _matches(row, control):
            row.status = "completed"
            row.updated_at = now_ms()
            _save(row)


def reap_unfinished_thread_turn(control: TurnControlSnapshot | None) -> None:
    """A turn can exit by raise or early return; only this drops the controller it left behind."""
    if control is None:
        return
    row = get_thread_turn(control.thread_key)
    if not _matches(row, control) or row.status != "running":
        return
    mark_thread_turn_completed(control)


def _settle_interrupted_thread_turn(control: TurnControlSnapshot | None, status: str) -> bool:
    if control is None:
        return False
    with LOCK:
        row = get_thread_turn(control.thread_key)
        if not _matches(row, control) or row.status not in ("running", "waiting_approval", "finalizing"):
            return False
        active = _active_turns.get(control.thread_key)
        if active and active.turn_id == control.turn_id and active.revision == control.revision:
            _active_turns.pop(control.thread_key, None)
            active.controller.set()
        row.status = status
        row.updated_at = now_ms()
        _save(row)
        current = get_thread_turn(control.thread_key)
        return bool(current) and current.status == status


def complete_interrupted_thread_turn(control: TurnControlSnapshot | None) -> bool:
    return _settle_interrupted_thread_turn(control, "completed")


def supersede_interrupted_thread_turn(control: TurnControlSnapshot | None) -> bool:
    return _settle_interrupted_thread_turn(control, "superseded")


def claim_thread_turn_finalization_if_inbox_empty(control: TurnControlSnapshot) -> str:
    """Returns ``claimed``, ``updates_pending`` or ``inactive``."""
    with LOCK:
        row = get_thread_turn(control.thread_key)
        if not _matches(row, control):
            return "inactive"
        # Idempotent for this exact turn: a delivery may resume after the update
        # committed but before its caller observed it.
        if row.status == "finalizing":
            return "claimed"
        if row.status != "running":
            return "inactive"
        if not _has_open_updates(control):
            row.status = "finalizing"
            row.updated_at = now_ms()
            _save(row)
        claimed = get_thread_turn(control.thread_key)
        if not _matches(claimed, control):
            return "inactive"
        if claimed.status == "finalizing":
            return "claimed"
        return "updates_pending" if claimed.status == "running" else "inactive"


@dataclass
class InterruptResult:
    row: ThreadTurnRow | None
    aborted: bool
    notified: bool


def interrupt_thread_turn(thread_key: str, status: str, latest_instruction: str | None = None) -> InterruptResult:
    """``status`` is ``cancelled`` or ``superseded``."""
    with LOCK:
        row = get_thread_turn(thread_key)
        if not is_steerable_thread_turn(row):
            return InterruptResult(row=row, aborted=False, notified=False)
        stored = get_thread_turn(thread_key)
        if _matches(stored, row) and stored.status in ("running", "waiting_approval"):
            stored.status = status
            stored.latest_instruction = latest_instruction if latest_instruction is not None else row.latest_instruction
            stored.updated_at = now_ms()
            _save(stored)
        active = _active_turns.get(thread_key)
        same = bool(active) and active.turn_id == row.turn_id and active.revision == row.revision
        should_notify = same and not active.notified
        notifier = None
        if same:
            _active_turns.pop(thread_key, None)
            active.controller.set()
            if should_notify and active.notify_cancelled:
                active.notified = True
                notifier = active.notify_cancelled
    if notifier:
        notifier(status)
    return InterruptResult(row=row, aborted=bool(active), notified=should_notify)


class _FencedProgress:
    def __init__(self, control: TurnControlSnapshot, progress):
        self._control = control
        self._progress = progress
        self.narrate = self._narrate if getattr(progress, "narrate", None) else None

    def card(self, *args, **kwargs):
        if not is_thread_turn_current(self._control):
            return None
        return self._progress.card(*args, **kwargs)

    def _narrate(self, text: str) -> bool:
        if not is_thread_turn_current(self._control):
            return False
        return bool(self._progress.narrate(text))


def fenced_progress(control: TurnControlSnapshot | None, progress):
    """Wrap a progress sink so a cancelled or superseded turn stops posting."""
    if control is None:
        return progress
    return _FencedProgress(control, progress)


_LOW_SIGNAL = {"k", "ok", "okay", "kk", "cool", "nice", "thanks", "thank you", "ty", "got it", "sounds good"}


def classify_turn_steering(text: str) -> str:
    """Returns ``stop``, ``revise`` or ``none``."""
    normalized = re.sub(r"[^\w\s']", " ", text.lower(), flags=re.UNICODE).replace("_", " ")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return "none"
    if normalized in _LOW_SIGNAL:
        return "none"

    # An explicit "don't stop / don't cancel" means keep the active turn running.
    if re.search(r"\b(don't|dont|do not|never)\s+(stop|cancel|abort|halt|pause)\b", normalized):
        return "none"

    says_stop = re.search(r"\b(stop|cancel|abort|halt|pause)\b", normalized)
    stop_phrase = re.search(r"\b(never\s?mind|nvm|no need|forget it|scratch that)\b", normalized)
    stop_verb = re.search(r"\b(don't|dont) (do|run|send|post|create|execute|continue)\b", normalized)
    if says_stop or stop_phrase or stop_verb:
        return "stop"

    if re.search(r"^(no|nah|wait|actually|instead|rather|also|but|use)\b", normalized) or re.search(
        r"\b(no actually|do this|do that|change it|make it|include|exclude|without|with)\b", normalized
    ):
        return "revise"

    # While a turn is active, a substantive requester follow-up is usually new
    # guidance for the active task rather than a separate conversation.
    return "revise" if len(normalized) >= 8 else "none"
