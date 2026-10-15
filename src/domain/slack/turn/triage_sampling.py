"""Port of company-brain `brain/slack/triage-sampling.ts`.

Upstream this gates how many content-bearing triage traces go to analytics. There is
no analytics sink here, but the counter and decision are kept so a trace sink can use them.
"""

from dataclasses import dataclass

from src.db.falkordb_client import kv
from src.domain.slack.turn._state import now_ms

DAY_MS = 24 * 60 * 60 * 1_000
SAMPLING_COUNTER_RETENTION_DAYS = 8

TRIAGE_TRACE_FULL_CAPTURE_PER_ORG_DAY = 2_000
TRIAGE_TRACE_DOWNSHIFT_RATE = 0.2


@dataclass(frozen=True)
class TriageTraceSamplingDecision:
    sampled: bool
    sample_rate: float
    daily_ordinal: int


def _stable_trace_hash(trace_id: str) -> int:
    """Stable FNV-1a hash so retrying the same trace cannot change its sample."""
    value = 0x811C9DC5
    encoded = trace_id.encode("utf-16-le")
    # JS charCodeAt walks UTF-16 code units.
    for unit in (int.from_bytes(encoded[i : i + 2], "little") for i in range(0, len(encoded), 2)):
        value ^= unit
        value = (value * 0x01000193) & 0xFFFFFFFF
    return value


def evaluate_triage_trace_sampling(daily_ordinal: int, trace_id: str) -> TriageTraceSamplingDecision:
    if daily_ordinal <= TRIAGE_TRACE_FULL_CAPTURE_PER_ORG_DAY:
        return TriageTraceSamplingDecision(sampled=True, sample_rate=1, daily_ordinal=daily_ordinal)
    return TriageTraceSamplingDecision(
        sampled=_stable_trace_hash(trace_id) % 5 == 0,
        sample_rate=TRIAGE_TRACE_DOWNSHIFT_RATE,
        daily_ordinal=daily_ordinal,
    )


def reserve_triage_trace_sample(*, org_id: str, trace_id: str, now: int | None = None) -> TriageTraceSamplingDecision:
    day_bucket = (now or now_ms()) // DAY_MS
    key = f"brain:triage-trace-volume:{org_id}:{day_bucket}"
    daily_ordinal = int(kv().incr(key))
    if daily_ordinal == 1:
        kv().expire(key, SAMPLING_COUNTER_RETENTION_DAYS * DAY_MS // 1000)
    return evaluate_triage_trace_sampling(daily_ordinal, trace_id)
