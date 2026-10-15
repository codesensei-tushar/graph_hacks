"""Port of company-brain `brain/slack/member-provisioning.ts`.

People arrive through Slack, so this is where a person first becomes a Person in the
company graph. Their user + org-membership rows become one Person node: provisioning
creates it (or reuses the one with that email) and marks it an org member; the
mapping records which Person Slack provisioned (their ``provisionedMemberId``) so a
later revoke only undoes membership Slack itself granted.
"""

import logging
import time
import uuid
from dataclasses import dataclass

from src.db.falkordb_client import connect
from src.domain.access.roles import ROLE_OWNER, role_at_least, role_of
from src.domain.slack import identity_lock
from src.domain.slack.membership_provenance import SlackMembershipMapping, provisioned_membership_to_revoke
from src.domain.slack.workspace import _write_member, get_workspace_member

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProvisionedSlackMember:
    person_id: str
    created_person: bool
    created_membership: bool


def _normalized_email(value: str) -> str:
    return value.strip().lower()


def _normalized_name(value: str | None, email: str) -> str:
    return " ".join((value or "").split()) or email


def new_person_id() -> str:
    return f"person-{uuid.uuid4().hex[:12]}"


def _find_person_by_email(email: str) -> dict | None:
    _, graph = connect()
    rows = graph.query(
        "MATCH (p:Person) WHERE toLower(p.email) = $email RETURN p.id, coalesce(p.org_member, true) LIMIT 1",
        {"email": email},
    ).result_set
    return {"id": rows[0][0], "org_member": bool(rows[0][1])} if rows else None


def provision_slack_workspace_member(
    *, team_id: str, slack_user_id: str, email: str, name: str | None = None
) -> ProvisionedSlackMember:
    email = _normalized_email(email)
    if not email or "@" not in email:
        raise ValueError("Slack member has no usable email")

    _, graph = connect()
    mapped = get_workspace_member(team_id, slack_user_id)
    person_id = mapped["person_id"] if mapped else None
    created_person = False
    if not person_id:
        existing = _find_person_by_email(email)
        person_id = existing["id"] if existing else None
        if not person_id:
            person_id = new_person_id()
            graph.query(
                """
                MERGE (p:Person {email: $email})
                ON CREATE SET p.id = $id, p.name = $name, p.provisioned_by = 'slack', p.created_at = $now, p.org_member = false
                """,
                {"id": person_id, "email": email, "name": _normalized_name(name, email), "now": int(time.time() * 1000)},
            )
            # A concurrent team_join may have created the same email first.
            person_id = _find_person_by_email(email)["id"]
            created_person = True

    identity_lock.slack_identity_advisory_lock()
    current = get_workspace_member(team_id, slack_user_id)
    persisted_person_id = (current or {}).get("person_id") or person_id

    # The org membership: grant it only when the Person isn't a member yet, and
    # remember that Slack did, so revoke only undoes what Slack granted.
    rows = graph.query(
        """
        MATCH (p:Person {id: $id})
        WITH p, coalesce(p.org_member, false) AS was_member, p.org_member IS NULL AS seeded
        SET p.org_member = true
        RETURN was_member OR seeded
        """,
        {"id": persisted_person_id},
    ).result_set
    already_member = bool(rows and rows[0][0])
    created_membership = not already_member

    link_source = "web_confirmed" if (current or {}).get("link_source") == "web_confirmed" else "email_match"
    provisioned = (current or {}).get("provisioned_person_id") or (persisted_person_id if created_membership else None)
    _write_member(team_id, slack_user_id, persisted_person_id, email, "active", link_source, provisioned)
    return ProvisionedSlackMember(
        person_id=persisted_person_id, created_person=created_person, created_membership=created_membership
    )


def revoke_slack_workspace_member(*, team_id: str, slack_user_id: str) -> bool:
    identity_lock.slack_identity_advisory_lock()
    mapped = get_workspace_member(team_id, slack_user_id)
    if not mapped:
        return False
    _write_member(
        team_id,
        slack_user_id,
        mapped["person_id"],
        mapped["email"],
        "revoked",
        mapped["link_source"] or "email_match",
        mapped["provisioned_person_id"],
    )

    _, graph = connect()
    rows = graph.query(
        "MATCH (m:SlackWorkspaceMember {person_id: $person}) RETURN m.status, m.provisioned_person_id",
        {"person": mapped["person_id"]},
    ).result_set
    mappings = [SlackMembershipMapping(status=status, provisioned_member_id=provisioned) for status, provisioned in rows]
    provisioned_id = provisioned_membership_to_revoke(mappings)
    if not provisioned_id or provisioned_id != mapped["person_id"]:
        return True
    # Never strip an owner or admin; their access isn't Slack's to take away.
    if role_at_least(role_of(slack_user_id), "admin") or role_of(slack_user_id) == ROLE_OWNER:
        return True
    graph.query("MATCH (p:Person {id: $id}) SET p.org_member = false", {"id": provisioned_id})
    return True
