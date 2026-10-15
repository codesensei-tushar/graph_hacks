"""Who can change what.

- owner   the person who installed the app (``SLACK_OWNER_ID``).
- admin   listed in ``SLACK_ADMIN_IDS`` or promoted by an owner/admin. Stored on the SlackUser node.
- member  everyone else in the workspace.

Owners and admins can promote and demote admins and wipe a channel's memory.
Members can ask, remember, and propose writes for their own approval.
"""

from enum import Enum

from src.core.config import settings
from src.db.falkordb_client import connect


class Role(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


def role_of(user_id: str) -> Role:
    if settings.slack_owner_id and user_id == settings.slack_owner_id:
        return Role.OWNER
    if user_id in settings.slack_admin_ids:
        return Role.ADMIN
    _, graph = connect()
    rows = graph.query(
        "MATCH (u:SlackUser {id: $id}) RETURN u.role",
        {"id": user_id},
    ).result_set
    if rows and rows[0][0] == Role.ADMIN.value:
        return Role.ADMIN
    return Role.MEMBER


def can_manage(role: Role) -> bool:
    return role in (Role.OWNER, Role.ADMIN)


def set_admin(user_id: str, is_admin: bool) -> None:
    _, graph = connect()
    graph.query(
        "MERGE (u:SlackUser {id: $id}) SET u.role = $role",
        {"id": user_id, "role": Role.ADMIN.value if is_admin else Role.MEMBER.value},
    )
