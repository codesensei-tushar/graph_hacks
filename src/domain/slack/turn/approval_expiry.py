"""Port of company-brain `brain/turn/approval-expiry.ts`.

Collapse a still-live Approve/Deny card once nobody decided within 15 minutes.
The DO ``schedule()`` becomes a ``src.core.scheduler`` job, keyed per approval so
re-arming replaces the pending job.
"""

import logging
import math

from src.core import scheduler
from src.domain.access import approvals
from src.domain.access.approvals import PendingApproval
from src.domain.slack.turn._state import now_ms
from src.integrations.slack.client import SlackApprovalCard, resolved_approval_blocks

log = logging.getLogger(__name__)

EXPIRY_JOB = "runApprovalExpiry"
RETRY_DELAY_SECONDS = 60
MAX_ATTEMPTS = 3
EXPIRED_TEXT = "This approval expired after 15 minutes."


def _job_id(approval_id: str) -> str:
    return f"approval-expiry:{approval_id}"


def approval_card(approval: PendingApproval) -> SlackApprovalCard:
    return SlackApprovalCard(
        approval_id=approval.approval_id,
        summary=approval.summary,
        tool_name=approval.tool_name,
        asker_user=approval.asker_user,
        expires_at=approval.expires_at / 1000,
        slug=(approval.extra or {}).get("slug"),
    )


def arm_approval_expiry(approval: PendingApproval) -> None:
    delay_seconds = max(1, math.ceil((approval.expires_at - now_ms()) / 1000))
    scheduler.schedule(delay_seconds, EXPIRY_JOB, {"approval_id": approval.approval_id}, job_id=_job_id(approval.approval_id))


def _update_card(approval: PendingApproval) -> bool:
    from src.domain.slack.workspace import get_workspace_by_team_id

    workspace = get_workspace_by_team_id(approval.team_id)
    if workspace is None:
        return True
    client = workspace.client
    blocks = resolved_approval_blocks(approval_card(approval), "Expired")
    if hasattr(client, "update_slack_message"):
        return bool(client.update_slack_message(approval.channel, approval.card_ts, EXPIRED_TEXT, blocks))
    return bool(client.update_message(approval.channel, approval.card_ts, EXPIRED_TEXT, blocks))


def run_approval_expiry(payload: dict) -> None:
    attempt = payload.get("attempt") or 0
    approval = approvals.load_approval(payload["approval_id"])
    if approval is None:
        return
    if approval.status == "pending":
        if not approvals.approval_is_expired(approval):
            arm_approval_expiry(approval)
            return
        approvals.mark_approval_terminal(approval.approval_id, "expired")
    elif approval.status != "expired" or attempt == 0:
        # Retries may still collapse an expired card; anything else is decided.
        return
    if not approval.card_ts:
        return
    updated = _update_card(approval)
    if not updated and attempt + 1 < MAX_ATTEMPTS:
        scheduler.schedule(
            RETRY_DELAY_SECONDS,
            EXPIRY_JOB,
            {"approval_id": approval.approval_id, "attempt": attempt + 1},
            job_id=_job_id(approval.approval_id),
        )


scheduler.register(EXPIRY_JOB, run_approval_expiry)
