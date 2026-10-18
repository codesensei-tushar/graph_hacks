"""Connect GitHub: a company installs the app on its own GitHub account, from the console.

    GET    /console/api/github                          {app: {available, connect}, installations: [...]}
    POST   /console/api/github/connect                  {install_url}: github.com/apps/<slug>/installations/new?state=…
    POST   /console/api/github/installations/{id}/sync  {backfill?}: read every repo again ("all" or N)
    DELETE /console/api/github/installations/{id}       disconnect: its data is deleted; {uninstall_url}
    GET    /console/oauth/github/setup                  GitHub's setup URL: ?installation_id&setup_action&state
    GET    /console/oauth/github/verify                 the GitHub sign-in that proves the installer: ?code&state

The flow, and why each check is there:

1. **Connect** (an owner or admin with a browser session) mints a state: 32 random bytes, kept in kv
   as ``console:github:<sha256>`` for 15 minutes, bound to the session (``Session.id_hash``) and
   the org, and used once. GitHub carries ``state`` from the install URL to the setup URL
   (https://docs.github.com/en/apps/sharing-github-apps/sharing-your-github-app).
2. **Setup**: GitHub sends ``installation_id``, which anyone can type
   (https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/about-the-setup-url
   warns about exactly that). So the state is taken once, the browser must still carry the same
   session, still an owner or admin of the same org, and then the person signs in with GitHub.
3. **Verify**: the sign-in code is traded for a user token, and ``GET /user/installations`` must list
   the installation: the person in the browser can reach it on GitHub. The token is dropped at once.
   Then ``GET /app/installations/{id}`` (the app's JWT) names its account, the account is claimed
   (``ExternalAccount github:<account id>``: one org per GitHub account), the installation is bound
   to the org, and its backfill is queued in the org.

An operator can mint the same state for any org with the admin token (``POST /ops/github/connect``,
in ``github_app``); it has no browser session, so steps 2 and 3 skip the session checks, and the
GitHub sign-in check still applies. Every outcome is a redirect to a fixed console address; nothing
GitHub says is echoed, and codes, states and tokens never reach a log line.
"""

import hashlib
import json
import logging
import secrets
import time
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from src.connectors import oauth as connector_oauth
from src.core import tenancy
from src.core.config import settings
from src.db.falkordb_client import kv
from src.domain.console.session import console_admin, console_user
from src.domain.github import backfill, connections, installations
from src.integrations.github import app_config, client

log = logging.getLogger(__name__)

STATE_TTL_SECONDS = 15 * 60
SESSION_COOKIE = "__Host-brain_session"
# The codes a callback redirect carries; the console words each one.
#   github_state     the state is unknown, expired, spent, or the session changed
#   github_denied    the GitHub sign-in was declined
#   github_not_yours the person who signed in cannot reach that installation on GitHub
#   account_claimed  another org already reads this GitHub account
#   github_failed    anything else (GitHub unreachable, the app not set up)
ERRORS = frozenset({"github_state", "github_denied", "github_not_yours", "account_claimed", "github_failed"})


def _enabled() -> None:
    if not settings.console_enabled:
        raise HTTPException(status_code=404, detail="Not Found")


api = APIRouter(prefix="/console/api/github", tags=["console"], dependencies=[Depends(_enabled)])
callbacks = APIRouter(tags=["console-github"], include_in_schema=False)


def _state_key(state: str) -> str:
    return f"console:github:{hashlib.sha256(state.encode()).hexdigest()}"


def _org(session) -> tenancy.OrgRef:
    try:
        return tenancy.resolve(session.org)
    except (LookupError, TypeError) as error:
        raise HTTPException(status_code=403, detail="no_org") from error


_slug_cache: tuple[float, str] | None = None


def app_slug() -> str:
    """The app's URL name: stored, ``GITHUB_APP_SLUG``, or (an env app without it) asked of GitHub once an hour."""
    global _slug_cache
    creds = app_config.current()
    if creds is None:
        return ""
    if creds.slug:
        return creds.slug
    if _slug_cache and time.monotonic() - _slug_cache[0] < 3600:
        return _slug_cache[1]
    try:
        slug = str(client.get_app().get("slug") or "")
    except Exception as error:
        log.warning("[github] app slug unavailable: %s", type(error).__name__)
        slug = ""
    _slug_cache = (time.monotonic(), slug)
    return slug


def can_connect() -> bool:
    creds = app_config.current()
    return bool(creds and creds.can_link and settings.public_url and app_slug())


def mint_state(org_id: str, session_hash: str | None, person: str | None, actor: str) -> str:
    """The install URL for ``org_id``, its state bound to the browser session (None: the operator)."""
    state = secrets.token_urlsafe(32)
    inflight = hashlib.sha256(state.encode()).hexdigest()
    record = {"org": org_id, "session": session_hash, "person": person, "actor": actor, "at": int(time.time()), "inflight": inflight}
    kv().set(_state_key(state), json.dumps(record), ex=STATE_TTL_SECONDS)
    installations.connect_started(inflight)
    return f"https://github.com/apps/{app_slug()}/installations/new?{urllib.parse.urlencode({'state': state})}"


def _take(state: str) -> dict | None:
    pipe = kv().pipeline()
    pipe.get(_state_key(state))
    pipe.delete(_state_key(state))
    raw, _ = pipe.execute()
    try:
        record = json.loads(raw) if raw else None
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _go(path: str) -> RedirectResponse:
    response = RedirectResponse(path, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _fail(code: str, operator: bool = False) -> RedirectResponse:
    code = code if code in ERRORS else "github_failed"
    return _go(f"/ops/github/app?error={code}" if operator else f"/console/github?error={code}")


def _session_hash(request: Request) -> str:
    cookie = request.cookies.get(SESSION_COOKIE) or ""
    return hashlib.sha256(cookie.encode()).hexdigest() if cookie else ""


async def _same_session(request: Request, record: dict) -> bool:
    """The browser carries the session the state was minted for, still an owner or admin of that org."""
    if record.get("session") is None:
        return True  # the operator's state: no browser session to check
    browser = _session_hash(request)
    if not browser or not secrets.compare_digest(browser.encode(), str(record.get("session")).encode()):
        return False
    try:
        session = await console_admin(request)
    except HTTPException:
        return False
    return session.org == record.get("org") and getattr(session, "id_hash", "") == browser


def verify_url() -> str:
    return f"{settings.public_url}/console/oauth/github/verify"


# --- the console API -------------------------------------------------------------------------------


@api.get("")
def overview(session=Depends(console_user)):
    org = _org(session)
    with tenancy.org_context(org):
        return {
            "app": {"available": app_config.enabled(), "connect": can_connect()},
            "installations": connections.list_status(org.id),
        }


@api.post("/connect")
def connect(session=Depends(console_admin)):
    org = _org(session)
    if not getattr(session, "id_hash", ""):
        return JSONResponse({"error": "operator_session"}, status_code=403)
    if not can_connect():
        return JSONResponse({"error": "not_configured"}, status_code=409)
    return {"install_url": mint_state(org.id, session.id_hash, getattr(session, "person", None), session.actor)}


def _limit(body: dict):
    value = body.get("backfill", body.get("backfill_limit"))
    if value is None:
        return None
    value = 0 if value == "all" else value
    try:
        return backfill.clamp_limit(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad_limit") from None


def _bound(org: tenancy.OrgRef, installation_id: str) -> dict:
    current = installations.get(installation_id)
    if current is None or current["org"] != org.id or current["status"] not in (installations.ACTIVE, installations.REMOVING):
        raise HTTPException(status_code=404, detail="no such connection")
    return current


async def _body(request: Request) -> dict:
    if not (request.headers.get("content-length") or "0").strip("0"):
        return {}
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _audit(org: tenancy.OrgRef, actor: str, action: str, target: str) -> None:
    from src.domain.console import audit

    audit.record(org.id, actor, action, target, "ok")


@api.post("/installations/{installation_id}/sync")
async def sync(installation_id: str, request: Request, session=Depends(console_admin)):
    org, limit = _org(session), _limit(await _body(request))

    def run():
        with tenancy.org_context(org):
            current = _bound(org, installation_id)
            if current["status"] != installations.ACTIVE:
                return JSONResponse({"error": "this connection is being removed"}, status_code=409)
            connections.resync(installation_id, limit)
            _audit(org, session.actor, "github.sync", installation_id)
            return {"ok": True}

    return await run_in_threadpool(run)


@api.delete("/installations/{installation_id}")
def disconnect(installation_id: str, session=Depends(console_admin)):
    org = _org(session)
    with tenancy.org_context(org):
        current = _bound(org, installation_id)
        manage = connections.status(current)["manage_url"]
        if current["status"] == installations.ACTIVE:
            connections.disconnect(installation_id, forget=False)
            _audit(org, session.actor, "github.disconnect", installation_id)
        return JSONResponse({"ok": True, "status": "removing", "uninstall_url": manage}, status_code=202)


# --- GitHub's redirects ------------------------------------------------------------------------------


@callbacks.get("/console/oauth/github/setup")
async def setup(request: Request, installation_id: str = "", setup_action: str = "", state: str = ""):
    if not settings.console_enabled:
        return JSONResponse({"error": "not_found"}, status_code=404)
    if not state:
        # A repo selection changed on GitHub (setup_on_update) or an install started on GitHub itself:
        # nothing to bind here; the console shows what is connected.
        return _go("/console/github?updated=1" if setup_action == "update" else "/console/github")
    record = await run_in_threadpool(_take, state)
    operator = bool(record) and record.get("session") is None
    if record is None or not installation_id.isdigit():
        return _fail("github_state")
    if not await _same_session(request, record):
        return _fail("github_state", operator)
    # Step 3's state: the same binding, plus the installation GitHub named.
    second = secrets.token_urlsafe(32)
    kv().set(_state_key(second), json.dumps({**record, "installation": int(installation_id), "step": "verify"}), ex=STATE_TTL_SECONDS)
    return _go(client.authorize_url(second, verify_url()))


@callbacks.get("/console/oauth/github/verify")
async def verify(request: Request, code: str = "", state: str = "", error: str = ""):
    if not settings.console_enabled:
        return JSONResponse({"error": "not_found"}, status_code=404)
    record = await run_in_threadpool(_take, state) if state else None
    if record is None or record.get("step") != "verify":
        return _fail("github_state")
    operator = record.get("session") is None
    if not await _same_session(request, record):
        return _fail("github_state", operator)
    if error or not code:
        return _fail("github_denied" if error == "access_denied" else "github_failed", operator)
    return await run_in_threadpool(_complete, record, code)


def _complete(record: dict, code: str) -> RedirectResponse:
    operator = record.get("session") is None
    installation_id = int(record["installation"])
    try:
        user = client.user_can_access_installation(code, verify_url(), installation_id)
        if user is None:
            return _fail("github_not_yours", operator)
        found = client.get_installation(installation_id)
        account = found.get("account") or {}
        if not account.get("id"):
            return _fail("github_failed", operator)
        org = tenancy.resolve(record["org"])
        with connector_oauth.account_lock(installations.account_key(account["id"])):
            try:
                installations.bind(installation_id, org.id, account, connected_by=str(record.get("actor") or record.get("person") or "operator"))
            except installations.Claimed:
                return _fail("account_claimed", operator)
        with tenancy.org_context(org):
            connections.start_backfill(installation_id)
            connections.ensure_collaborator_refresh()
        _audit(org, str(record.get("actor") or "operator"), "github.connect", str(installation_id))
    except Exception as failure:
        log.warning("[github] connect for org %s failed: %s", record.get("org"), type(failure).__name__)
        return _fail("github_failed", operator)
    finally:
        installations.connect_finished(str(record.get("inflight") or ""))
    log.info("[github] installation=%s connected to org=%s by github user=%s", installation_id, record.get("org"), user.login)
    return _go("/ops/github/app?connected=1" if operator else "/console/github?connected=1")
