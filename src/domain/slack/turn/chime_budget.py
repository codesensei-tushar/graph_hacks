"""Port of company-brain `brain/slack/chime-budget.ts`.

How often Company Brain may speak unsolicited per channel, by triage priority, and
how much silent investigation may run at once.

    brain:chime-budget           hash channel -> budget state (brain_chime_budget)
    brain:passive-investigation  hash trace_id -> claim row (brain_passive_investigation)
"""

from dataclasses import dataclass, replace

from src.domain.slack.turn._state import LOCK, hdel, hget_json, hgetall_json, hset_json, now_ms

HOUR_MS = 60 * 60 * 1_000

CHIME_ABSOLUTE_MAX_PER_HOUR = 12
CHIME_SUMMONS_MAX_PER_HOUR = 4
CHIME_GENERAL_MAX_PER_HOUR = 6
CHIME_LOW_MAX_PER_HOUR = 2
CHIME_NORMAL_MIN_INTERVAL_MS = 3 * 60 * 1_000
CHIME_LOW_QUIET_INTERVAL_MS = 15 * 60 * 1_000
PASSIVE_INVESTIGATION_MAX_CONCURRENT = 2
PASSIVE_INVESTIGATION_MAX_PER_HOUR = 6
PASSIVE_INVESTIGATION_LEASE_MS = 10 * 60 * 1_000
PASSIVE_INVESTIGATION_RETENTION_MS = 7 * 24 * HOUR_MS

_BUDGET = "brain:chime-budget"
_INVESTIGATIONS = "brain:passive-investigation"

# ChimeBudgetSuppression: budget_min_interval | budget_priority_allowance | budget_ceiling


@dataclass(frozen=True)
class ChimeBudgetState:
    bucket: int
    total: int
    summons: int
    general: int
    low: int
    last_reply_at: int
    last_normal_reply_at: int


@dataclass(frozen=True)
class ChimeBudgetDecision:
    allowed: bool
    next: ChimeBudgetState | None = None
    suppression: str | None = None


def can_use_answer_fallback(suppression: str, has_explicit_fallback: bool, priority: str) -> bool:
    return priority == "normal" and suppression == "budget_min_interval" and has_explicit_fallback


def empty_chime_budget_state(now: int) -> ChimeBudgetState:
    return ChimeBudgetState(bucket=now // HOUR_MS, total=0, summons=0, general=0, low=0, last_reply_at=0, last_normal_reply_at=0)


def _deny(suppression: str) -> ChimeBudgetDecision:
    return ChimeBudgetDecision(allowed=False, suppression=suppression)


def evaluate_chime_budget(
    *,
    state: ChimeBudgetState,
    priority: str,
    now: int,
    last_channel_activity_at: int | None = None,
    urgent_investigation_finding: bool = False,
) -> ChimeBudgetDecision:
    """Pure priority matrix, exported so the policy can be exhaustively unit-tested."""
    if state.bucket != now // HOUR_MS:
        state = replace(empty_chime_budget_state(now), last_reply_at=state.last_reply_at, last_normal_reply_at=state.last_normal_reply_at)

    if state.total >= CHIME_ABSOLUTE_MAX_PER_HOUR:
        return _deny("budget_ceiling")

    if priority == "summons":
        if state.summons >= CHIME_SUMMONS_MAX_PER_HOUR:
            return _deny("budget_priority_allowance")
        return ChimeBudgetDecision(allowed=True, next=replace(state, total=state.total + 1, summons=state.summons + 1, last_reply_at=now))

    if priority == "urgent" and urgent_investigation_finding is True:
        # Urgent findings pay only the absolute speech ceiling after silent work.
        return ChimeBudgetDecision(allowed=True, next=replace(state, total=state.total + 1, last_reply_at=now))

    if priority == "urgent":
        if state.general >= CHIME_GENERAL_MAX_PER_HOUR:
            return _deny("budget_priority_allowance")
        return ChimeBudgetDecision(allowed=True, next=replace(state, total=state.total + 1, general=state.general + 1, last_reply_at=now))

    if priority == "normal":
        if state.last_normal_reply_at > 0 and now - state.last_normal_reply_at < CHIME_NORMAL_MIN_INTERVAL_MS:
            return _deny("budget_min_interval")
        if state.general >= CHIME_GENERAL_MAX_PER_HOUR:
            return _deny("budget_priority_allowance")
        return ChimeBudgetDecision(
            allowed=True,
            next=replace(state, total=state.total + 1, general=state.general + 1, last_reply_at=now, last_normal_reply_at=now),
        )

    if last_channel_activity_at is not None and now - last_channel_activity_at < CHIME_LOW_QUIET_INTERVAL_MS:
        return _deny("budget_min_interval")
    if state.low >= CHIME_LOW_MAX_PER_HOUR:
        return _deny("budget_priority_allowance")
    return ChimeBudgetDecision(allowed=True, next=replace(state, total=state.total + 1, low=state.low + 1, last_reply_at=now))


def _read_budget_state(channel_id: str, now: int) -> ChimeBudgetState:
    row = hget_json(_BUDGET, channel_id)
    return ChimeBudgetState(**row) if row else empty_chime_budget_state(now)


def reserve_chime_answer(
    *,
    channel_id: str,
    priority: str,
    now: int | None = None,
    last_channel_activity_at: int | None = None,
    urgent_investigation_finding: bool = False,
) -> ChimeBudgetDecision:
    """Reserve an unsolicited Slack reply slot after triage has already judged it."""
    now = now or now_ms()
    with LOCK:
        result = evaluate_chime_budget(
            state=_read_budget_state(channel_id, now),
            priority=priority,
            now=now,
            last_channel_activity_at=last_channel_activity_at,
            urgent_investigation_finding=urgent_investigation_finding,
        )
        if result.allowed and result.next is not None:
            hset_json(_BUDGET, channel_id, result.next.__dict__)
        return result


@dataclass(frozen=True)
class PassiveInvestigationClaim:
    trace_id: str
    started_at: int


@dataclass(frozen=True)
class PassiveInvestigationDecision:
    allowed: bool
    claim: PassiveInvestigationClaim | None = None
    # concurrency | investigation_hourly
    suppression: str | None = None


def reserve_passive_investigation(*, trace_id: str, channel_id: str, priority: str, now: int | None = None) -> PassiveInvestigationDecision:
    """Reserve silent work after triage; overflow is dropped rather than queued stale."""
    now = now or now_ms()
    bucket = now // HOUR_MS
    with LOCK:
        rows = hgetall_json(_INVESTIGATIONS)
        for key, row in list(rows.items()):
            if row["status"] != "running" and row["started_at"] < now - PASSIVE_INVESTIGATION_RETENTION_MS:
                hdel(_INVESTIGATIONS, key)
                rows.pop(key)
            elif row["status"] == "running" and row["expires_at"] <= now:
                row["status"] = "expired"
                hset_json(_INVESTIGATIONS, key, row)
        concurrent = sum(1 for row in rows.values() if row["status"] == "running")
        if concurrent >= PASSIVE_INVESTIGATION_MAX_CONCURRENT:
            return PassiveInvestigationDecision(allowed=False, suppression="concurrency")
        hourly = sum(1 for row in rows.values() if row["hour_bucket"] == bucket)
        if hourly >= PASSIVE_INVESTIGATION_MAX_PER_HOUR:
            return PassiveInvestigationDecision(allowed=False, suppression="investigation_hourly")
        if trace_id not in rows:
            hset_json(
                _INVESTIGATIONS,
                trace_id,
                {
                    "trace_id": trace_id,
                    "channel_id": channel_id,
                    "priority": priority,
                    "status": "running",
                    "started_at": now,
                    "expires_at": now + PASSIVE_INVESTIGATION_LEASE_MS,
                    "hour_bucket": bucket,
                },
            )
        return PassiveInvestigationDecision(allowed=True, claim=PassiveInvestigationClaim(trace_id=trace_id, started_at=now))


def release_passive_investigation(claim: PassiveInvestigationClaim, outcome: str) -> None:
    """``outcome`` is completed | silent | failed | suppressed."""
    with LOCK:
        row = hget_json(_INVESTIGATIONS, claim.trace_id)
        if row and row["started_at"] == claim.started_at and row["status"] == "running":
            row["status"] = outcome
            row["expires_at"] = now_ms()
            hset_json(_INVESTIGATIONS, claim.trace_id, row)
