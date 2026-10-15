"""Who can change what. Ranks ported from company-brain ``repo-lib/permissions.ts``.

- owner   the person who installed the app (``SLACK_OWNER_ID``).
- admin   listed in ``SLACK_ADMIN_IDS`` or promoted by an owner/admin. Stored on the SlackUser node.
- member  everyone else in the workspace.

Owners and admins promote and demote admins and wipe a channel's memory.
"""

from typing import Literal

from src.core.config import settings
from src.db.falkordb_client import connect

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"

MemberRole = Literal["owner", "admin", "member"]

_RANK: dict[str, int] = {ROLE_OWNER: 3, ROLE_ADMIN: 2, ROLE_MEMBER: 1}


def role_at_least(role: str | None, minimum: MemberRole) -> bool:
    if not role:
        return False
    return _RANK.get(role, 0) >= _RANK[minimum]


def role_of(slack_user_id: str) -> MemberRole:
    if settings.slack_owner_id and slack_user_id == settings.slack_owner_id:
        return ROLE_OWNER
    if slack_user_id in settings.slack_admin_ids:
        return ROLE_ADMIN
    _, graph = connect()
    rows = graph.query("MATCH (u:SlackUser {id: $id}) RETURN u.role", {"id": slack_user_id}).result_set
    if rows and rows[0][0] == ROLE_ADMIN:
        return ROLE_ADMIN
    return ROLE_MEMBER


def set_role(slack_user_id: str, role: MemberRole) -> None:
    _, graph = connect()
    graph.query("MERGE (u:SlackUser {id: $id}) SET u.role = $role", {"id": slack_user_id, "role": role})
