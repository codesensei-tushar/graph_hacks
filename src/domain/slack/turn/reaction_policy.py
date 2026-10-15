"""Port of company-brain `brain/slack/reaction-policy.ts`."""

from dataclasses import dataclass

HOUR_MS = 60 * 60 * 1_000

ACK_BREAKER_MAX_PER_HOUR = 30
ACK_BREAKER_OPEN_MS = 60 * 60 * 1_000


@dataclass(frozen=True)
class AckBreakerState:
    bucket: int
    count: int
    open_until: int


@dataclass(frozen=True)
class AckBreakerDecision:
    allowed: bool
    opened: bool
    next: AckBreakerState


def evaluate_ack_breaker(state: AckBreakerState | None, now_ms: int) -> AckBreakerDecision:
    bucket = now_ms // HOUR_MS
    if state and state.open_until > now_ms:
        return AckBreakerDecision(allowed=False, opened=False, next=state)
    count = state.count if state and state.bucket == bucket else 0
    if count >= ACK_BREAKER_MAX_PER_HOUR:
        return AckBreakerDecision(
            allowed=False,
            opened=True,
            next=AckBreakerState(bucket=bucket, count=count, open_until=now_ms + ACK_BREAKER_OPEN_MS),
        )
    return AckBreakerDecision(allowed=True, opened=False, next=AckBreakerState(bucket=bucket, count=count + 1, open_until=0))
