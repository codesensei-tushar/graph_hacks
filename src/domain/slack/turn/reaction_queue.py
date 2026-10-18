"""Port of company-brain `brain/slack/reaction-queue.ts`.

Passive reactions (triage ACKs and answer fallbacks) go through a durable outbox and
are delivered one at a time, 2-3 seconds apart, with an hourly per-channel breaker on
model-selected ACK volume. The DO alarm becomes a ``src.core.scheduler`` job.

    brain:reaction-outbox            hash id -> row (brain_reaction_outbox)
    brain:reaction-outbox:unique     hash "team|channel|ts|emoji" -> id
    brain:reaction-outbox:seq        id counter
    brain:reaction-breaker           hash channel -> breaker state
    brain:reaction-queue:schedule    pending scheduler job id
"""

import logging
import random
import uuid
from dataclasses import dataclass

from src.core import tenancy
from src.core import scheduler
from src.db.falkordb_client import kv
from src.domain.slack.turn._state import LOCK, hdel, hget_json, hgetall_json, hset_json, now_ms
from src.domain.slack.turn.event_store import SlackEventIdentity, record_stored_action_outcome, record_stored_suppression
from src.domain.slack.turn.reaction_policy import AckBreakerDecision, AckBreakerState, evaluate_ack_breaker

log = logging.getLogger(__name__)

REACTION_EXPIRY_MS = 10 * 60 * 1_000
# Spacing above one second even after timestamp flooring.
REACTION_MIN_DELAY_SECONDS = 2
REACTION_MAX_DELAY_SECONDS = 3
REACTION_MAX_ATTEMPTS = 3
REACTION_RETENTION_MS = 7 * 24 * 60 * 60 * 1_000

QUEUE_JOB = "runPassiveReactionQueue"

_OUTBOX = "brain:reaction-outbox"
_UNIQUE = "brain:reaction-outbox:unique"
_SEQ = "brain:reaction-outbox:seq"
_BREAKER = "brain:reaction-breaker"
_SCHEDULE = "brain:reaction-queue:schedule"

# ReactionQueueKind: ack | answer_fallback


@dataclass(frozen=True)
class EnqueueReactionResult:
    enqueued: bool
    # breaker | dedup
    suppression: str | None = None


def _unique_key(team_id: str, channel: str, message_ts: str, emoji: str) -> str:
    return f"{team_id}|{channel}|{message_ts}|{emoji}"


def _reserve_ack_reaction(channel_id: str, now: int) -> AckBreakerDecision:
    row = hget_json(_BREAKER, channel_id)
    decision = evaluate_ack_breaker(
        AckBreakerState(bucket=row["hour_bucket"], count=row["ack_count"], open_until=row["open_until"]) if row else None,
        now,
    )
    hset_json(
        _BREAKER,
        channel_id,
        {"hour_bucket": decision.next.bucket, "ack_count": decision.next.count, "open_until": decision.next.open_until},
    )
    return decision


def _random_queue_delay_seconds() -> float:
    return REACTION_MIN_DELAY_SECONDS + random.random() * (REACTION_MAX_DELAY_SECONDS - REACTION_MIN_DELAY_SECONDS)


def _arm_reaction_queue(delay_seconds: float | None = None) -> None:
    with LOCK:
        if kv().get(_SCHEDULE) is not None:
            return
        job_id = uuid.uuid4().hex
        kv().set(_SCHEDULE, job_id)
        # One queue for every org's teams: its job is the legacy org's, and each row runs in its team's org.
        with tenancy.org_context(tenancy.legacy_org()):
            scheduler.schedule(
                _random_queue_delay_seconds() if delay_seconds is None else delay_seconds,
                QUEUE_JOB,
                {"queue": "passive_reaction", "schedule_id": job_id},
                job_id=job_id,
            )


def _pending_rows() -> list[dict]:
    rows = [row for row in hgetall_json(_OUTBOX).values() if row["status"] == "pending"]
    return sorted(rows, key=lambda row: (row["available_at"], row["id"]))


def recover_passive_reaction_queue() -> None:
    """Re-arm durable pending work after a restart or a prior schedule failure."""
    job_id = kv().get(_SCHEDULE)
    if job_id is not None:
        job_id = job_id.decode() if isinstance(job_id, bytes) else job_id
        if kv().zscore("brain:schedule", job_id) is None:
            kv().delete(_SCHEDULE)
    if _pending_rows():
        _arm_reaction_queue()


def enqueue_passive_reaction(
    *,
    identity: SlackEventIdentity,
    emoji: str,
    kind: str,
    reason: str,
    obs,
    now: int | None = None,
) -> EnqueueReactionResult:
    now = now or now_ms()
    with LOCK:
        for row in hgetall_json(_OUTBOX).values():
            if row["status"] != "pending" and row["created_at"] < now - REACTION_RETENTION_MS:
                hdel(_OUTBOX, str(row["id"]))
                hdel(_UNIQUE, _unique_key(row["team_id"], row["channel_id"], row["message_ts"], row["emoji"]))
        unique = _unique_key(identity.team_id, identity.channel, identity.message_ts, emoji)
        if kv().hexists(_UNIQUE, unique):
            record_stored_suppression(identity, suppression="dedup", outcome="reaction_deduplicated")
            return EnqueueReactionResult(enqueued=False, suppression="dedup")
        # The emergency threshold is for model-selected ACK volume. Answer fallbacks
        # are already bounded by the speaking budget they replace.
        breaker = _reserve_ack_reaction(identity.channel, now) if kind == "ack" else None
        if breaker is not None and not breaker.allowed:
            record_stored_suppression(identity, suppression="breaker", outcome="reaction_breaker_open")
            if breaker.opened:
                log.error("[company-brain] passive reaction breaker tripped channel=%s trace=%s", identity.channel, obs.trace_id)
            else:
                log.warning("[company-brain] passive reaction breaker open channel=%s trace=%s", identity.channel, obs.trace_id)
            return EnqueueReactionResult(enqueued=False, suppression="breaker")
        row_id = int(kv().incr(_SEQ))
        hset_json(
            _OUTBOX,
            str(row_id),
            {
                "id": row_id,
                "team_id": identity.team_id,
                "channel_id": identity.channel,
                "message_ts": identity.message_ts,
                "thread_ts": identity.thread_ts,
                "emoji": emoji,
                "kind": kind,
                "reason": reason,
                "trace_id": obs.trace_id,
                "distinct_id": obs.distinct_id,
                "session_id": obs.session_id,
                "status": "pending",
                "created_at": now,
                "available_at": now,
                "expires_at": now + REACTION_EXPIRY_MS,
                "attempts": 0,
                "last_error": None,
            },
        )
        kv().hset(_UNIQUE, unique, row_id)
    record_stored_action_outcome(identity, "reaction_queued")
    log.info(
        "[company-brain] passive reaction queued kind=%s emoji=%s trace=%s channel=%s message=%s",
        kind,
        emoji,
        obs.trace_id,
        identity.channel,
        identity.message_ts,
    )
    _arm_reaction_queue()
    return EnqueueReactionResult(enqueued=True)


def _row_identity(row: dict) -> SlackEventIdentity:
    return SlackEventIdentity(team_id=row["team_id"], channel=row["channel_id"], message_ts=row["message_ts"], thread_ts=row["thread_ts"])


def _retry_delay_seconds(result, attempts: int) -> int:
    if not result.ok and getattr(result, "retry_after_seconds", None):
        return result.retry_after_seconds
    return min(30, 2 ** max(1, attempts))


def _save(row: dict) -> None:
    hset_json(_OUTBOX, str(row["id"]), row)


@dataclass
class _ReactionResult:
    ok: bool
    outcome: str | None = None
    error: str | None = None
    retry_after_seconds: int | None = None


def _add_reaction(client, channel: str, ts: str, emoji: str):
    if hasattr(client, "add_slack_reaction_detailed"):
        return client.add_slack_reaction_detailed(channel, ts, emoji)
    added = client.add_reaction(channel, ts, emoji)
    return _ReactionResult(ok=True, outcome="added") if added else _ReactionResult(ok=False, error="request_failed")


def run_passive_reaction_queue(payload: dict) -> None:
    with LOCK:
        scheduled = kv().get(_SCHEDULE)
        scheduled = scheduled.decode() if isinstance(scheduled, bytes) else scheduled
        if scheduled is None or scheduled != payload.get("schedule_id"):
            return
        kv().delete(_SCHEDULE)

    now = now_ms()
    pending = _pending_rows()
    if not pending:
        return
    row = pending[0]
    from src.domain import orgs

    with tenancy.org_context(orgs.org_ref_for_team(row.get("team_id"))):
        _run_row(row, now)


def _run_row(row: dict, now: int) -> None:
    if row["expires_at"] <= now:
        row["status"] = "expired"
        row["last_error"] = "stale"
        _save(row)
        record_stored_action_outcome(_row_identity(row), "reaction_expired")
        log.info(
            "[company-brain] passive reaction expired kind=%s emoji=%s trace=%s channel=%s message=%s",
            row["kind"],
            row["emoji"],
            row["trace_id"],
            row["channel_id"],
            row["message_ts"],
        )
        _arm_reaction_queue()
        return
    if row["available_at"] > now:
        _arm_reaction_queue(max(1, -(-(row["available_at"] - now) // 1_000)))
        return

    from src.domain.slack.workspace import get_workspace_by_team_id

    result = _ReactionResult(ok=False, error="request_failed")
    try:
        workspace = get_workspace_by_team_id(row["team_id"])
    except Exception:
        workspace = None
    if workspace is not None:
        try:
            result = _add_reaction(workspace.client, row["channel_id"], row["message_ts"], row["emoji"])
        except Exception:
            result = _ReactionResult(ok=False, error="request_failed")

    attempts = row["attempts"] + 1
    if result.ok:
        row.update(status="delivered", attempts=attempts, last_error=None)
        _save(row)
        record_stored_action_outcome(_row_identity(row), result.outcome or "added")
        log.info(
            "[company-brain] passive reaction delivered kind=%s emoji=%s outcome=%s trace=%s channel=%s message=%s",
            row["kind"],
            row["emoji"],
            result.outcome,
            row["trace_id"],
            row["channel_id"],
            row["message_ts"],
        )
    elif attempts < REACTION_MAX_ATTEMPTS and result.error in ("ratelimited", "request_failed"):
        row.update(attempts=attempts, available_at=now + _retry_delay_seconds(result, attempts) * 1_000, last_error=result.error)
        _save(row)
    else:
        row.update(status="failed", attempts=attempts, last_error=result.error)
        _save(row)
        record_stored_action_outcome(_row_identity(row), "reaction_failed")
        log.warning(
            "[company-brain] passive reaction failed kind=%s emoji=%s error=%s trace=%s channel=%s message=%s",
            row["kind"],
            row["emoji"],
            result.error,
            row["trace_id"],
            row["channel_id"],
            row["message_ts"],
        )
    _arm_reaction_queue()


scheduler.register(QUEUE_JOB, run_passive_reaction_queue)
