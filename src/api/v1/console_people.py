"""The console's JSON API for people: link a Jira or HubSpot account to the person it is.

    GET  /console/api/people/unlinked   Jira/HubSpot accounts linked to no person, with suggestions (admins)
    POST /console/api/people/link       {external_user_id, person_id}: link them (admins, audited)

graph.py links an account to a person when their emails match. Jira often hides emails, and a
HubSpot owner may use another address, so many accounts stay unlinked and their tickets have
no person. An admin links them here: ``(:ExternalUser)-[:IS {via: 'admin', by, at}]->(:Person)``,
an admin-asserted mapping that replaces ``CONNECTOR_*_PEOPLE`` for console orgs. The link
counts like an email link everywhere: the person's OWNS and WORKED_ON edges move onto them,
and in a DM the person may read the restricted Jira projects the account can read.

A suggestion is a person with the same name, or with an email at the same domain (public mail
domains excluded). Emails themselves are never returned. Everything runs in the session's org.
"""

import logging
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from src.api.v1.console_connectors import _actor, _audit, _enabled, _json, _org
from src.connectors import graph as connector_graph
from src.core import tenancy
from src.db.falkordb_client import connect
from src.domain.console.session import console_admin

log = logging.getLogger(__name__)

router = APIRouter(prefix="/console/api", tags=["console"], dependencies=[Depends(_enabled)])

SOURCES = ("jira", "hubspot")
MAX_UNLINKED = 500
MAX_SUGGESTIONS = 5
MAX_ID = 300
# A shared address domain says nothing about who someone is.
PUBLIC_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com", "icloud.com",
        "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com", "gmx.com", "gmx.de", "yandex.com", "mail.com", "zoho.com",
    }
)
# Apps and bots are not people. Jira gives Atlassian's own app accounts (Automation for Jira and
# the like) accountIds under 557058:, and an account_type or is_bot property, when a connector
# stores one, says so outright; the names are a last resort for app accounts outside that prefix.
JIRA_APP_PREFIX = "557058:"
APP_NAMES = frozenset({"automation for jira", "jira automation", "jira service management widget", "atlassian assist", "system"})
_NOT_A_BOT = f"""
    NOT coalesce(u.is_bot, false) AND NOT toLower(coalesce(u.account_type, '')) IN ['app', 'bot', 'customer']
    AND NOT (u.source = 'jira' AND u.external_id STARTS WITH '{JIRA_APP_PREFIX}')
    AND NOT toLower(coalesce(u.name, '')) IN $app_names
"""


def _name_key(name) -> str:
    return " ".join(str(name or "").lower().split())


def _domain(email) -> str:
    local, at, domain = str(email or "").strip().lower().rpartition("@")
    return domain if at and local and domain not in PUBLIC_DOMAINS else ""


def unlinked() -> dict:
    """The org's Jira/HubSpot accounts linked to no person, most active first, each with suggested
    people; and every person, for a manual pick. Runs in the org's context."""
    _, graph = connect()
    accounts = graph.ro_query(
        f"""
        MATCH (u:ExternalUser)
        WHERE u.source IN $sources AND NOT (u)-[:IS]->(:Person) AND {_NOT_A_BOT}
        OPTIONAL MATCH (u)-[r]->(n)
        WHERE type(r) <> 'CAN_READ'
        RETURN u.id, u.name, u.source, u.email, count(DISTINCT n) AS items
        ORDER BY items DESC, u.name, u.id
        LIMIT $limit
        """,
        {"sources": list(SOURCES), "limit": MAX_UNLINKED, "app_names": sorted(APP_NAMES)},
    ).result_set
    people = graph.ro_query("MATCH (p:Person) WHERE p.name IS NOT NULL RETURN p.id, p.name, p.email ORDER BY p.name, p.id").result_set
    by_name: dict[str, list] = {}
    by_domain: dict[str, list] = {}
    for person_id, name, email in people:
        by_name.setdefault(_name_key(name), []).append((person_id, name))
        if domain := _domain(email):
            by_domain.setdefault(domain, []).append((person_id, name))
    rows = []
    for user_id, name, source, email, items in accounts:
        suggestions, seen = [], set()
        for reason, matches in (("same_name", by_name.get(_name_key(name), [])), ("same_email_domain", by_domain.get(_domain(email), []))):
            for person_id, person_name in matches:
                if person_id not in seen and len(suggestions) < MAX_SUGGESTIONS:
                    seen.add(person_id)
                    suggestions.append({"person_id": person_id, "name": person_name, "reason": reason})
        rows.append({"id": user_id, "name": name, "source": source, "items": int(items), "suggestions": suggestions})
    return {"unlinked": rows, "people": [{"id": person_id, "name": name} for person_id, name, _ in people]}


class LinkRefused(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def link(user_id: str, person_id: str, actor: str) -> dict:
    """Link the account to the person, as an admin-asserted mapping. Runs in the org's context."""
    _, graph = connect()
    rows = graph.query(
        f"MATCH (u:ExternalUser {{id: $id}}) WHERE u.source IN $sources AND {_NOT_A_BOT} OPTIONAL MATCH (u)-[r:IS]->(p:Person) RETURN r.via, p.id",
        {"id": user_id, "sources": list(SOURCES), "app_names": sorted(APP_NAMES)},
    ).result_set
    if not rows:
        raise LinkRefused(404, "no such Jira or HubSpot account (apps and bots are not linked)")
    person = graph.query("MATCH (p:Person {id: $id}) RETURN p.name", {"id": person_id}).result_set
    if not person:
        raise LinkRefused(404, "no such person")
    via, linked = rows[0]
    if linked == person_id:
        return {"ok": True, "external_user_id": user_id, "person": {"id": person_id, "name": person[0][0]}, "changed": False}
    if linked is not None and via != "admin":
        raise LinkRefused(409, "this account is already linked to a person by email")
    graph.query("MATCH (:ExternalUser {id: $id})-[r:IS]->(:Person) DELETE r", {"id": user_id})
    graph.query(
        "MATCH (u:ExternalUser {id: $id}), (p:Person {id: $person}) MERGE (u)-[r:IS]->(p) SET r.via = 'admin', r.by = $by, r.at = $at",
        {"id": user_id, "person": person_id, "by": actor, "at": int(time.time() * 1000)},
    )
    connector_graph._move_person_edges(graph, user_id, person_id)
    log.info("[console] account linked org=%s user=%s person=%s", tenancy.current_org(), user_id, person_id)
    return {"ok": True, "external_user_id": user_id, "person": {"id": person_id, "name": person[0][0]}, "changed": True}


@router.get("/people/unlinked")
def list_unlinked(session=Depends(console_admin)):
    org = _org(session)
    with tenancy.org_context(org):
        return unlinked()


@router.post("/people/link")
async def link_person(request: Request, session=Depends(console_admin)):
    body = await _json(request)
    user_id, person_id = body.get("external_user_id"), body.get("person_id")
    if not all(isinstance(value, str) and 0 < len(value) <= MAX_ID for value in (user_id, person_id)):
        return JSONResponse({"error": "send external_user_id and person_id"}, status_code=400)
    org = _org(session)
    try:
        answer = await run_in_threadpool(tenancy.in_org, org, link, user_id, person_id, _actor(session))
    except LinkRefused as refused:
        return JSONResponse({"error": str(refused)}, status_code=refused.status)
    if answer["changed"]:
        _audit(org.id, session, "people.link", f"{user_id} -> {person_id}")
    return answer
