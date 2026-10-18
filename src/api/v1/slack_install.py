"""Install, status, disconnect and account-link routes.

Port of the matching routes in company-brain `routes/slack/index.ts` and
`setup/routes.ts` (manifest.json). company-brain gates install/status/disconnect on
a signed-in org admin. There is no web login here, so those routes take the
``BRAIN_ADMIN_TOKEN`` bearer token (or ``?token=`` for the browser install link).
The installing Slack user becomes that workspace's installer of record.

Orgs: an install started with the admin token goes to the legacy org, or to ``?org=<id>``;
one started from the console (``CONSOLE_ENABLED``) goes to the signed-in admin's org. The
org travels in the OAuth state, and the callback stores the bot token in that org's graph
and routes the team to it (a ``WorkspaceRoute``; the legacy org's teams need none). A team
already routed to another org is refused: one Slack workspace feeds one org.
"""

import inspect

import html
import json
import logging
import secrets
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from src.core import tenancy
from src.core.config import settings
from src.db.falkordb_client import kv
from src.domain import orgs
from src.domain.slack import account_link
from src.domain.slack.bootstrap import bootstrap_slack_workspace
from src.domain.slack.install_greeting import greet_slack_installer
from src.domain.slack.lifecycle_api import exchange_slack_oauth
from src.domain.slack.manifest import SLACK_SCOPES, oauth_callback_url, slack_app_manifest
from src.domain.slack.token_crypto import encrypt_token
from src.domain.slack.workspace import (
    disconnect_slack_workspace,
    get_workspace_by_team_id,
    get_workspace_status,
    list_connected_slack_installs,
    upsert_workspace,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/slack", tags=["slack-install"])

OAUTH_STATE_TTL_SECONDS = 600


def _origin(request: Request) -> str:
    return settings.public_url or str(request.base_url).rstrip("/")


def _admin_denial(request: Request, query_token: str | None = None) -> JSONResponse | None:
    if not settings.brain_admin_token:
        return JSONResponse({"error": "admin routes are disabled: set BRAIN_ADMIN_TOKEN"}, status_code=503)
    header = request.headers.get("authorization", "")
    presented = header[7:] if header.lower().startswith("bearer ") else (query_token or "")
    if not presented or not secrets.compare_digest(presented, settings.brain_admin_token):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return None


def _admin_org(org: str | None) -> tenancy.OrgRef | None:
    """The org an admin-token call is for: ``?org=`` or the legacy org. None for an unknown org."""
    if not org:
        return tenancy.legacy_org()
    try:
        return tenancy.resolve(org)
    except (LookupError, TypeError):
        return None


async def _console_admin_org(request: Request) -> tenancy.OrgRef | None:
    """The signed-in console admin's org, or None (console off, no session, not an admin)."""
    if not settings.console_enabled:
        return None
    try:
        from src.domain.console.session import console_admin  # package A
    except ImportError:
        return None
    try:
        session = console_admin(request)
        if inspect.isawaitable(session):
            session = await session
    except Exception:
        return None
    org_id = session.get("org") if isinstance(session, dict) else getattr(session, "org", None)
    try:
        return tenancy.resolve(org_id) if org_id else None
    except (LookupError, TypeError):
        return None


@router.get("/manifest.json")
def manifest(request: Request):
    return slack_app_manifest(_origin(request))


@router.get("/status")
def status(request: Request, org: str | None = None):
    if denied := _admin_denial(request):
        return denied
    if (ref := _admin_org(org)) is None:
        return JSONResponse({"error": "no such org"}, status_code=404)
    with tenancy.org_context(ref):
        return {**get_workspace_status(), "installs": list_connected_slack_installs()}


@router.delete("/workspace")
def disconnect(request: Request, team_id: str | None = None, org: str | None = None):
    if denied := _admin_denial(request):
        return denied
    if (ref := _admin_org(org)) is None:
        return JSONResponse({"error": "no such org"}, status_code=404)
    with tenancy.org_context(ref):
        result = disconnect_slack_workspace(team_id)
    if not result.disconnected:
        if result.reason == "cleanup_failed":
            return JSONResponse({"error": "cleanup_failed", "retryable": True}, status_code=503)
        return JSONResponse({"error": "not_connected"}, status_code=404)
    return {"ok": True, "revoked": result.revoked}


@router.get("/oauth/install")
async def oauth_install(request: Request, token: str | None = None, org: str | None = None):
    ref = await _console_admin_org(request) if not token and not request.headers.get("authorization") else None
    if ref is None:
        if denied := _admin_denial(request, token):
            return denied
        if (ref := _admin_org(org)) is None:
            return JSONResponse({"error": "no such org"}, status_code=404)
    if not settings.slack_oauth_enabled:
        return HTMLResponse(
            "Slack is not configured: set SLACK_CLIENT_ID, SLACK_CLIENT_SECRET and SLACK_SIGNING_SECRET.",
            status_code=409,
        )
    state = secrets.token_urlsafe(24)
    kv().set(f"slack:oauth:{state}", json.dumps({"return_to": _origin(request), "org": ref.id}), ex=OAUTH_STATE_TTL_SECONDS)
    query = urlencode(
        {
            "client_id": settings.slack_client_id,
            "scope": SLACK_SCOPES,
            "redirect_uri": oauth_callback_url(_origin(request)),
            "state": state,
        }
    )
    return RedirectResponse(f"https://slack.com/oauth/v2/authorize?{query}", status_code=302)


@router.get("/oauth/callback")
def oauth_callback(request: Request, background: BackgroundTasks, code: str | None = None, state: str | None = None):
    if not code or not state:
        return JSONResponse({"error": "missing code/state"}, status_code=400)
    key = f"slack:oauth:{state}"
    raw = kv().get(key)
    if not raw:
        return JSONResponse({"error": "invalid or expired state"}, status_code=400)
    kv().delete(key)
    try:
        ref = tenancy.resolve(json.loads(raw).get("org") or tenancy.LEGACY_ORG_ID)
    except (LookupError, TypeError, ValueError):
        return JSONResponse({"error": "the org this install was started for no longer exists"}, status_code=400)

    oauth = exchange_slack_oauth(code, oauth_callback_url(_origin(request)))
    routed = orgs.routed_org_id(oauth.team_id)
    if routed is not None and routed != ref.id:
        log.warning("[slack] install of team=%s into org=%s refused: routed to org=%s", oauth.team_id, ref.id, routed)
        return HTMLResponse("<!doctype html><title>Company Brain</title><p>This Slack workspace is already connected to another Company Brain org.</p>", status_code=409)
    with tenancy.org_context(ref):
        upsert_workspace(
            team_id=oauth.team_id,
            bot_token_enc=encrypt_token(oauth.bot_token),
            bot_user_id=oauth.bot_user_id,
            team_name=oauth.team_name,
            installed_by=oauth.authed_user_id,
            scopes=oauth.scopes,
            app_id=oauth.app_id,
            source="oauth",
        )
    if not tenancy.is_legacy(ref):
        orgs.route_team(oauth.team_id, ref.id)

    def after_install() -> None:
        try:
            workspace = get_workspace_by_team_id(oauth.team_id)
            if workspace is None:
                return
            bootstrap_slack_workspace(workspace=workspace, slack_user_id=oauth.authed_user_id)
            # Reload so the greeting sees the home channel bootstrap just set.
            workspace = get_workspace_by_team_id(oauth.team_id) or workspace
            greet_slack_installer(workspace=workspace, slack_user_id=oauth.authed_user_id, team_name=oauth.team_name)
        except Exception:
            log.warning("[slack] post-oauth bootstrap failed", exc_info=True)

    background.add_task(tenancy.in_org, ref, after_install)
    team = html.escape(oauth.team_name or "your workspace")
    return HTMLResponse(f"<!doctype html><title>Company Brain</title><p>Company Brain is connected to <b>{team}</b>. You can close this tab and DM the bot in Slack.</p>")


_STATUS_CODES = {"invalid": 404, "expired": 410, "used": 410, "not_in_org": 403, "claimed": 409}


def _page(body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>Link your Slack account</title><body style='font-family:sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem'>{body}</body>",
        status_code=status_code,
    )


def _link_org(token: str) -> tenancy.OrgRef:
    """The org of the team an account-link token was made for (its state is in kv); legacy when unknown."""
    try:
        loaded = account_link._load(token)
    except Exception:
        loaded = None
    return orgs.org_ref_for_team((loaded[1] if loaded else {}).get("team_id"))


@router.get("/account-link/{token}")
def account_link_preview(token: str):
    with tenancy.org_context(_link_org(token)):
        return _account_link_preview(token)


def _account_link_preview(token: str):
    preview = account_link.get_slack_account_link_preview(token)
    if preview.status != "ready":
        return _page(f"<p>This link is {html.escape(preview.status)}. Ask Company Brain for a new one.</p>", _STATUS_CODES[preview.status])
    who = html.escape(preview.slack_display_name or preview.slack_email or "your Slack account")
    team = html.escape(preview.team_name or "this workspace")
    return _page(
        f"<h1>Link {who}</h1><p>Enter the email the team knows you by in <b>{team}</b>.</p>"
        f"<form method=post><input type=email name=email required style='width:100%;padding:.5rem'>"
        f"<p><button type=submit>Link account</button></p></form>"
    )


@router.post("/account-link/{token}")
async def account_link_complete(token: str, request: Request, background: BackgroundTasks):
    # Parsed by hand: FastAPI's Form() needs python-multipart, which isn't installed.
    email = (parse_qs((await request.body()).decode()).get("email") or [""])[0]
    ref = _link_org(token)
    with tenancy.org_context(ref):
        return _account_link_complete(token, email, background, ref)


def _account_link_complete(token: str, email: str, background: BackgroundTasks, ref: tenancy.OrgRef):
    result = account_link.complete_slack_account_link(token, email)
    if not result.ok:
        messages = {
            "not_in_org": "No team member has that email. Ask an admin to add you.",
            "claimed": "That person is already linked to another Slack account.",
        }
        message = messages.get(result.reason, f"This link is {result.reason}. Ask Company Brain for a new one.")
        return _page(f"<p>{html.escape(message)}</p>", _STATUS_CODES[result.reason])

    workspace = get_workspace_by_team_id(result.team_id)
    if workspace is not None:
        def notify() -> None:
            org = workspace.team_name or "the workspace"
            account_link.notify_slack_account_linked(workspace.client.web, slack_user_id=result.slack_user_id, org_name=org)
            account_link.notify_installer_of_link(
                workspace.client.web,
                installer_slack_user_id=workspace.installed_by,
                slack_user_id=result.slack_user_id,
                person_name=result.person_name,
            )

        background.add_task(tenancy.in_org, ref, notify)
    return _page(f"<p>Linked to {html.escape(result.person_name or 'your account')}. You can go back to Slack.</p>")
