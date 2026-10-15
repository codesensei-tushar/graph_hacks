"""Consequential writes suspend for an Approve/Deny card.

Port of company-brain ``brain/turn/approval.ts``. Their DO SQL rows become JSON
records in the Redis keyspace under FalkorDB. Only the asker can decide, a pending
approval expires after 15 minutes, and a decision is claimed exactly once.
"""

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Literal

from src.db.falkordb_client import kv

APPROVAL_EXPIRY_MS = 15 * 60 * 1000
# Decided records stay readable for a day so a late click gets "already approved".
_RECORD_TTL_SECONDS = 24 * 60 * 60

ApprovalStatus = Literal["pending", "approved", "denied", "expired", "executed", "cancelled", "error"]

# Compare-and-set on status, so two clicks can't both decide.
_TRANSITION = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local record = cjson.decode(raw)
if record['status'] ~= ARGV[1] then return 0 end
record['status'] = ARGV[2]
if ARGV[3] ~= '' then
  record['decided_at'] = tonumber(ARGV[4])
  record['decided_by'] = ARGV[3]
end
redis.call('SET', KEYS[1], cjson.encode(record), 'KEEPTTL')
return 1
"""


@dataclass
class PendingApproval:
    approval_id: str
    team_id: str
    channel: str
    thread_ts: str
    asker_user: str
    tool_name: str
    tool_input: dict
    summary: str
    # The container the action reads and writes in, fixed at request time.
    container_tag: str
    status: ApprovalStatus
    created_at: int
    expires_at: int
    card_ts: str | None = None
    decided_at: int | None = None
    decided_by: str | None = None
    extra: dict = field(default_factory=dict)


def _key(approval_id: str) -> str:
    return f"brain:approval:{approval_id}"


def _now_ms() -> int:
    return int(time.time() * 1000)


def approval_is_expired(approval: PendingApproval, now: int | None = None) -> bool:
    return approval.status == "pending" and approval.expires_at <= (now or _now_ms())


def insert_pending_approval(
    *,
    team_id: str,
    channel: str,
    thread_ts: str,
    asker_user: str,
    tool_name: str,
    tool_input: dict,
    summary: str,
    container_tag: str,
    now: int | None = None,
    extra: dict | None = None,
) -> PendingApproval:
    now = now or _now_ms()
    approval = PendingApproval(
        approval_id=uuid.uuid4().hex,
        team_id=team_id,
        channel=channel,
        thread_ts=thread_ts,
        asker_user=asker_user,
        tool_name=tool_name,
        tool_input=tool_input,
        summary=summary,
        container_tag=container_tag,
        status="pending",
        created_at=now,
        expires_at=now + APPROVAL_EXPIRY_MS,
        extra=dict(extra or {}),
    )
    kv().set(_key(approval.approval_id), json.dumps(asdict(approval)), ex=_RECORD_TTL_SECONDS)
    return approval


def load_approval(approval_id: str) -> PendingApproval | None:
    raw = kv().get(_key(approval_id))
    if raw is None:
        return None
    try:
        return PendingApproval(**json.loads(raw))
    except (TypeError, ValueError):
        return None


def set_approval_card_ts(approval_id: str, card_ts: str | None) -> None:
    approval = load_approval(approval_id)
    if approval is None or not card_ts:
        return
    approval.card_ts = card_ts
    kv().set(_key(approval_id), json.dumps(asdict(approval)), keepttl=True)


def mark_approval_decided(approval_id: str, status: Literal["approved", "denied"], user_id: str, now: int | None = None) -> bool:
    """True only for the one caller that moved it out of pending."""
    return bool(kv().eval(_TRANSITION, 1, _key(approval_id), "pending", status, user_id, now or _now_ms()))


def mark_approval_terminal(approval_id: str, status: Literal["expired", "executed", "cancelled", "error"], from_status: str = "pending") -> bool:
    return bool(kv().eval(_TRANSITION, 1, _key(approval_id), from_status, status, "", 0))
