"""Audit events in the control graph: ``(:AuditEvent {at, org, actor, action, target, result})``.

``actor`` is ``slack:<team>:<user>`` for a console user and ``operator`` for the
BRAIN_ADMIN_TOKEN holder. Writing an event never fails the action it records.
"""

import logging
import time

from src.domain.console import contracts

log = logging.getLogger(__name__)

OPERATOR = "operator"


def slack_actor(team_id: str, slack_user_id: str) -> str:
    return f"slack:{team_id}:{slack_user_id}"


def record(org: str | None, actor: str, action: str, target: str | None = None, result: str = "ok") -> None:
    try:
        contracts.control_graph().query(
            "CREATE (:AuditEvent {at: $at, org: $org, actor: $actor, action: $action, target: $target, result: $result})",
            {"at": int(time.time() * 1000), "org": org, "actor": actor, "action": action, "target": target, "result": result},
        )
    except Exception:
        log.exception("[audit] could not record action=%s org=%s result=%s", action, org, result)


def recent(org: str, limit: int = 100) -> list[dict]:
    rows = contracts.control_graph().query(
        """
        MATCH (e:AuditEvent {org: $org})
        RETURN e.at, e.actor, e.action, e.target, e.result
        ORDER BY e.at DESC LIMIT $limit
        """,
        {"org": org, "limit": max(1, min(int(limit), 500))},
    ).result_set
    return [dict(zip(("at", "actor", "action", "target", "result"), row, strict=True)) for row in rows]
