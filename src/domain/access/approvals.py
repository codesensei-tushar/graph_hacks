"""Consequential writes wait for an explicit Approve from the person who asked.

An approval lives in Redis with a TTL, so an unanswered card expires on its own.
Only the asker can decide, and a decision is claimed exactly once.
"""

import json
import uuid
from dataclasses import asdict, dataclass
from enum import Enum

from src.core.config import settings
from src.db.falkordb_client import kv


class Decision(str, Enum):
    OK = "ok"
    EXPIRED = "expired"
    NOT_ASKER = "not_asker"


@dataclass
class Approval:
    id: str
    asker_id: str
    action: str
    payload: dict
    scope: str
    channel_id: str
    thread_ts: str


def _key(approval_id: str) -> str:
    return f"brain:approval:{approval_id}"


def create(asker_id: str, action: str, payload: dict, scope: str, channel_id: str, thread_ts: str) -> Approval:
    approval = Approval(
        id=uuid.uuid4().hex,
        asker_id=asker_id,
        action=action,
        payload=payload,
        scope=scope,
        channel_id=channel_id,
        thread_ts=thread_ts,
    )
    kv().set(_key(approval.id), json.dumps(asdict(approval)), ex=settings.approval_ttl_seconds)
    return approval


def claim(approval_id: str, user_id: str) -> tuple[Decision, Approval | None]:
    raw = kv().get(_key(approval_id))
    if raw is None:
        return Decision.EXPIRED, None
    approval = Approval(**json.loads(raw))
    if approval.asker_id != user_id:
        return Decision.NOT_ASKER, approval
    # delete() returns 0 when a concurrent click already claimed it.
    if not kv().delete(_key(approval_id)):
        return Decision.EXPIRED, None
    return Decision.OK, approval
