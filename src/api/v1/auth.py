"""Sign in with Slack, the console's own JSON routes, and invite codes.

- ``GET /auth/slack/start`` and ``GET /auth/slack/callback``: OpenID Connect against
  Slack (``src/domain/console/oidc.py``). The callback maps team and user to an org
  and a Person, refuses guests and Slack Connect users, and starts a session. A team
  with no org lands on ``/console/start``.
- ``POST /auth/logout``.
- ``GET /console/api/me``, ``POST /console/api/orgs`` (name plus invite code) and
  ``GET /console/api/audit`` (admins).
- ``POST /ops/invites``: the operator (BRAIN_ADMIN_TOKEN) mints an invite code.

Every route here answers 404 while CONSOLE_ENABLED is off. Failures redirect to
``/signin?error=<code>`` with a fixed code; nothing Slack says is echoed.
"""

import logging
import unicodedata

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from src.core.config import settings
from src.domain.access import roles
from src.core import tenancy
from src.domain import orgs
from src.domain.console import audit, csrf, invites, oidc
from src.domain.console import session as sessions
from src.domain.console.session import COOKIE, Session, console_admin, console_session, require_console
from src.domain.slack.workspace import (
    get_org_actor_by_slack_identity,
    get_workspace_by_team_id,
    list_connected_workspaces,
)

log = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_console)], tags=["console-auth"])

SIGNIN_PATH = "/signin"
START_PATH = "/console/start"
CONSOLE_PATH = "/console"
ORG_NAME_MAX = 80


def _origin(request: Request) -> str:
    return csrf.expected_origin(request)


def _signin_error(code: str) -> RedirectResponse:
    response = RedirectResponse(f"{SIGNIN_PATH}?error={code}", status_code=303)
    _clear_binding(response)
    return response


def _clear_binding(response) -> None:
    response.delete_cookie(oidc.BINDING_COOKIE, path="/", secure=True, httponly=True, samesite="lax")


# --- sign in -----------------------------------------------------------------------------


@router.get("/auth/slack/start")
def slack_start(request: Request):
    if not settings.console_signin_enabled:
        return _signin_error("unavailable")
    url, binding = oidc.begin(_origin(request))
    response = RedirectResponse(url, status_code=302)
    # Lax: the callback is a top-level GET navigation back from Slack, which carries it.
    response.set_cookie(
        oidc.BINDING_COOKIE, binding, max_age=oidc.STATE_TTL_SECONDS, path="/", secure=True, httponly=True, samesite="lax"
    )
    return response


def _unrouted_org_created_by(created_by: str) -> orgs.Org | None:
    """An org this Slack identity created that no workspace routes to yet (its bot isn't installed)."""
    rows = tenancy.control_graph().query(
        "MATCH (o:Org {created_by: $by}) WHERE NOT ()-[:OF]->(o) RETURN o.id ORDER BY o.created_at LIMIT 1",
        {"by": created_by},
    ).result_set
    return orgs.get_org(rows[0][0]) if rows else None


class _Refused(Exception):
    def __init__(self, code: str, org: str | None = None) -> None:
        super().__init__(code)
        self.code, self.org = code, org


def _resolve(identity: oidc.SlackIdentity) -> tuple[dict, str]:
    """The new session's fields and where to send the browser. Raises ``_Refused``."""
    team, user = identity.team_id, identity.user_id
    base = {"team": team, "slack_user": user, "person": None, "person_name": identity.name}

    org = orgs.org_for_team(team)
    if org is None:
        # No workspace routes here yet. Either this person already created an org whose
        # bot isn't installed, or they start one (an invite code is needed for that).
        created = _unrouted_org_created_by(f"{team}:{user}")
        if created is not None:
            return {**base, "org": created.id, "role": sessions.ROLE_PENDING}, START_PATH
        return {**base, "org": None, "role": roles.ROLE_MEMBER}, START_PATH

    with tenancy.org_context(org.ref()):
        # Guests, bots and Slack Connect users from another workspace: not members.
        if refusal := sessions.slack_membership_refusal(team, user):
            raise _Refused(refusal, org.id)
        email = identity.email if identity.email_verified else None
        resolution = get_org_actor_by_slack_identity(team, user, email)
        if resolution.lookup == "mapped_not_in_org":
            raise _Refused("not_a_member", org.id)
        if org.created_by and org.created_by == f"{team}:{user}":
            role = roles.ROLE_OWNER
        else:
            role = roles.role_of(user, env_roles=org.legacy)
    person = resolution.actor.person_id if resolution.actor else None
    return {**base, "org": org.id, "person": person, "role": role}, CONSOLE_PATH


@router.get("/auth/slack/callback")
def slack_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if not settings.console_signin_enabled:
        return _signin_error("unavailable")
    try:
        # Slack sends ?error= when the person cancels; the state is still spent.
        identity = oidc.finish(None if error else code, state, _origin(request), request.cookies.get(oidc.BINDING_COOKIE))
    except oidc.SignInError as exc:
        audit.record(None, "unknown", "signin", result=f"refused:{exc.code}")
        return _signin_error(exc.code)

    actor = audit.slack_actor(identity.team_id, identity.user_id)
    try:
        fields, destination = _resolve(identity)
    except _Refused as refused:
        audit.record(refused.org, actor, "signin", result=f"refused:{refused.code}")
        return _signin_error(refused.code)

    sid, session = sessions.create(**fields, replacing=request.cookies.get(COOKIE))
    audit.record(session.org, actor, "signin", result="ok" if session.org else "ok:no_org")
    response = RedirectResponse(destination, status_code=303)
    _clear_binding(response)
    sessions.set_cookie(response, sid)
    return response


@router.post("/auth/logout")
def logout(request: Request):
    csrf.check_origin(request)
    sid = request.cookies.get(COOKIE)
    session = sessions.load(sid) if sid else None
    if sid:
        sessions.destroy(sid)
    if session is not None:
        audit.record(session.org, session.actor, "signout")
    response = RedirectResponse(f"{SIGNIN_PATH}?signed_out", status_code=303)
    sessions.clear_cookie(response)
    return response


# --- console JSON ------------------------------------------------------------------------


def _me(session: Session, org: orgs.Org | None = None) -> dict:
    org = org or (orgs.get_org(session.org) if session.org else None)
    connected = False
    if org is not None:
        with tenancy.org_context(org.ref()):
            connected = get_workspace_by_team_id(session.team) is not None if session.team else bool(list_connected_workspaces())
    return {
        "org": {"id": org.id, "name": org.name, "slug": org.slug} if org else None,
        "person": {"id": session.person, "name": session.person_name},
        "role": session.role if org else None,
        "csrf": session.csrf,
        "slack_connected": connected,
    }


@router.get("/console/api/me")
def me(session: Session = Depends(console_session)):
    return _me(session)


class OrgIn(BaseModel):
    name: str
    invite_code: str


def _clean_name(raw: str) -> str | None:
    name = " ".join(raw.split())
    if not 2 <= len(name) <= ORG_NAME_MAX or any(unicodedata.category(ch).startswith("C") for ch in name):
        return None
    return name


@router.post("/console/api/orgs")
def create_org(body: OrgIn, request: Request, session: Session = Depends(console_session)):
    if session.operator:
        raise HTTPException(status_code=403, detail="operator_cannot_own_org")
    name = _clean_name(body.name)
    if name is None:
        return JSONResponse({"error": "bad_name"}, status_code=400)
    if session.org or orgs.org_for_team(session.team) or _unrouted_org_created_by(session.creator_id):
        return JSONResponse({"error": "already_in_org"}, status_code=409)
    if not invites.consume(body.invite_code, session.actor):
        audit.record(None, session.actor, "org.create", result="refused:invalid_invite")
        return JSONResponse({"error": "invalid_invite"}, status_code=400)
    try:
        org = orgs.create_org(name, created_by=session.creator_id)
    except Exception:
        log.exception("[console] org creation failed")
        invites.release(body.invite_code, session.actor)
        audit.record(None, session.actor, "org.create", result="error")
        return JSONResponse({"error": "org_creation_failed"}, status_code=500)

    # Pending, not owner, until the Slack install confirms they're a full member of the team.
    sid, rotated = sessions.rotate(request.cookies.get(COOKIE), session, org=org.id, role=sessions.ROLE_PENDING)
    audit.record(org.id, session.actor, "org.create", target=org.id)
    response = JSONResponse(_me(rotated, org), status_code=201)
    sessions.set_cookie(response, sid)
    return response


@router.get("/console/api/audit")
def audit_log(session: Session = Depends(console_admin), limit: int = 100):
    return audit.recent(session.org, limit)


# --- operator ------------------------------------------------------------------------------


@router.post("/ops/invites")
def mint_invite(request: Request, ttl_days: int = invites.DEFAULT_TTL_DAYS):
    if not settings.brain_admin_token:
        return JSONResponse({"error": "admin routes are disabled: set BRAIN_ADMIN_TOKEN"}, status_code=503)
    if not sessions.is_operator(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    code, expires_at = invites.mint(ttl_days)
    audit.record(None, audit.OPERATOR, "invite.create")
    return {"code": code, "expires_at": expires_at}
