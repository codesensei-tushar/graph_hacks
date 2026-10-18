"""`link github`: tie a Slack user's Person to their GitHub account, proven by GitHub sign-in.

Typing a login is not enough: anyone could claim a collaborator's login and read a
private repo's data. The bot DMs a GitHub sign-in link; GitHub sends back a code; the
code is traded for the user's identity and the user token is dropped at once.

    (:SlackUser)-[:IS]->(:Person)<-[:IS]-(:GitHubUser)

The link token is one-time and expires after 15 minutes. It remembers the org the DM came from,
and the callback (``/github/oauth/callback``, a legacy-only path) writes that org's graph, so a
`link github` in any org's workspace links the person in that org.
"""

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass

from src.core import tenancy
from src.core.config import settings
from src.db.falkordb_client import connect, kv
from src.integrations.github.client import authorize_url, exchange_code_for_identity

log = logging.getLogger(__name__)

LINK_TTL_SECONDS = 15 * 60


@dataclass(frozen=True)
class GitHubLinkResult:
    ok: bool
    message: str
    login: str | None = None


def callback_url() -> str:
    return f"{settings.public_url or 'http://localhost:8000'}/github/oauth/callback"


def _state_key(token: str) -> str:
    return f"github:link:{hashlib.sha256(token.encode()).hexdigest()}"


def create_github_link_url(slack_user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    org = tenancy.current_org_or_none()
    record = {"slack_user_id": slack_user_id, "org": org.id if org is not None else tenancy.LEGACY_ORG_ID}
    kv().set(_state_key(token), json.dumps(record), ex=LINK_TTL_SECONDS)
    return authorize_url(token, callback_url())


def _person_for_slack_user(slack_user_id: str) -> dict | None:
    _, graph = connect()
    rows = graph.query("MATCH (:SlackUser {id: $id})-[:IS]->(p:Person) RETURN p.id, p.name LIMIT 1", {"id": slack_user_id}).result_set
    return {"id": rows[0][0], "name": rows[0][1]} if rows else None


def complete_github_link(state: str, code: str, exchange=exchange_code_for_identity) -> GitHubLinkResult:
    key = _state_key(state or "")
    raw = kv().getdel(key) if state else None  # one-time: a replayed callback finds nothing
    if not raw:
        return GitHubLinkResult(False, "This link expired or was already used. Send `link github` to the bot again.")
    record = json.loads(raw)
    try:
        org = tenancy.resolve(record.get("org") or tenancy.LEGACY_ORG_ID)
    except LookupError:
        return GitHubLinkResult(False, "This link expired or was already used. Send `link github` to the bot again.")
    with tenancy.org_context(org):
        return _complete(record["slack_user_id"], code, exchange)


def _complete(slack_user_id: str, code: str, exchange) -> GitHubLinkResult:
    person = _person_for_slack_user(slack_user_id)
    if person is None:
        return GitHubLinkResult(False, "Your Slack account isn't linked to anyone in the company graph yet. Message the bot once, then try again.")
    try:
        identity = exchange(code, callback_url())
    except Exception:
        log.exception("[github] link code exchange failed slack_user=%s", slack_user_id)
        return GitHubLinkResult(False, "GitHub didn't confirm the sign-in. Send `link github` to the bot and try again.")

    _, graph = connect()
    graph.query(
        """
        MERGE (u:GitHubUser {id: $id})
        SET u.login = $login, u.name = $login
        WITH u
        OPTIONAL MATCH (u)-[old:IS]->(:Person)
        DELETE old
        """,
        {"id": str(identity.id), "login": identity.login},
    )
    graph.query(
        "MATCH (u:GitHubUser {id: $id}), (p:Person {id: $person}) MERGE (u)-[:IS]->(p)",
        {"id": str(identity.id), "person": person["id"]},
    )
    log.info("[github] linked login=%s person=%s slack_user=%s", identity.login, person["id"], slack_user_id)
    return GitHubLinkResult(True, f"Linked GitHub @{identity.login} to {person['name']}. You can close this tab.", identity.login)
