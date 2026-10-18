"""Connect Jira or HubSpot from the console over the vendor's OAuth.

``POST /console/api/connectors/{source}/connect`` (an owner or admin, CSRF-checked) answers
``{authorize_url}``. Its ``state`` is 32 random bytes; kv keeps ``console:oauth:<sha256(state)>``
for ten minutes, bound to the session (``Session.id_hash``), the org and the source.

``GET /console/oauth/{source}/callback`` takes the state once (it is deleted as it is read), and
goes on only when the browser still carries the same session, the session's org is the one the
state was minted for, and the signed-in person is still an owner or admin. It then trades the
code for tokens, finds the vendor account (a Jira site from ``accessible-resources``; a HubSpot
portal from the token's metadata, checked against the CRM's own account details), claims the
``ExternalAccount`` for the org, and stores the install (status ``connecting``) with its tokens
encrypted. A reconnect of an account the org already has replaces that install's tokens instead.

Every outcome is a redirect to a fixed console page. Nothing from the vendor (an error
description, a site or portal name) is echoed into a URL or a page, and the code, the state and
the tokens never reach a log line.
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

from src.connectors import oauth, registry
from src.connectors.base import ConnectorError
from src.core.config import settings
from src.core.tenancy import org_context
from src.db.falkordb_client import kv

log = logging.getLogger(__name__)
router = APIRouter(tags=["console-oauth"])

SOURCES = ("jira", "hubspot")
STATE_TTL_SECONDS = 600
SESSION_COOKIE = "__Host-brain_session"
# The only error codes a callback redirect carries; the console words each one itself.
#   oauth_denied     the person declined on the vendor's consent page
#   oauth_state      the state is unknown, expired, spent, for another tool, or the session changed
#   account_claimed  another org already reads this Jira site or HubSpot portal
#   oauth_failed     anything else (the exchange, no Jira site, the app not configured); the log has the type
ERRORS = frozenset({"oauth_denied", "oauth_state", "account_claimed", "oauth_failed"})


def _state_key(state: str) -> str:
    return f"console:oauth:{hashlib.sha256(state.encode()).hexdigest()}"


async def _admin_session(request: Request):
    """Package A's ``console_admin``: the signed-in owner or admin, with the org set in context.
    Raises ``HTTPException`` (401 signed out, 403 not an admin, 404 console off); it also checks
    CSRF and Origin on a mutation."""
    from src.domain.console.session import console_admin

    return await console_admin(request)


async def _admin(request: Request):
    return await _admin_session(request)


def _connector(source: str):
    """The registered connector of ``source``, importing its module if no env install did."""
    if source not in SOURCES:
        return None
    if registry.get(source) is None:
        import importlib

        importlib.import_module(registry.KNOWN_MODULES[source])
    return registry.get(source)


def _csrf_ok(request: Request, session) -> bool:
    token = request.headers.get("x-csrf-token") or ""
    origin = request.headers.get("origin") or ""
    expected = getattr(session, "csrf", "") or ""
    return bool(expected) and _same(token, expected) and bool(settings.public_url) and origin == settings.public_url


def _same(a: str, b: str) -> bool:
    """Constant time, on bytes: ``compare_digest`` raises on a non-ASCII ``str`` from a header."""
    return secrets.compare_digest(a.encode(), b.encode())


@router.post("/console/api/connectors/{source}/connect")
def connect(source: str, request: Request, session=Depends(_admin)):
    if not settings.console_enabled:
        return JSONResponse({"error": "not_found"}, status_code=404)
    if not _csrf_ok(request, session):
        return JSONResponse({"error": "csrf"}, status_code=403)
    if not getattr(session, "id_hash", ""):
        # An operator (BRAIN_ADMIN_TOKEN) has no browser session to bind the state to.
        return JSONResponse({"error": "operator_session"}, status_code=403)
    connector = _connector(source)
    spec = getattr(connector, "oauth", None)
    if connector is None or spec is None:
        return JSONResponse({"error": "unknown_source"}, status_code=404)
    if not oauth.configured(spec) or not settings.public_url:
        return JSONResponse({"error": "not_configured"}, status_code=409)
    state = secrets.token_urlsafe(32)
    record = {"org": session.org, "session": session.id_hash, "source": source, "person": getattr(session, "person", None), "at": int(time.time())}
    kv().set(_state_key(state), json.dumps(record), ex=STATE_TTL_SECONDS)
    return {"authorize_url": oauth.authorize_url(spec, oauth.callback_url(source), state)}


def _go(path: str) -> RedirectResponse:
    response = RedirectResponse(path, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _fail(code: str) -> RedirectResponse:
    return _go(f"/console?error={code if code in ERRORS else 'oauth_failed'}")


def _connected(source: str) -> RedirectResponse:
    """Success: the console lists the new connection and offers its next step (Jira: pick projects)."""
    return _go(f"/console?connected={source}")


def _take_state(state: str) -> dict | None:
    """The state's record, deleted in the same round trip so it is used once."""
    pipe = kv().pipeline()
    pipe.get(_state_key(state))
    pipe.delete(_state_key(state))
    raw, _ = pipe.execute()
    if not raw:
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _session_hash(request: Request) -> str:
    cookie = request.cookies.get(SESSION_COOKIE) or ""
    return hashlib.sha256(cookie.encode()).hexdigest() if cookie else ""


@router.get("/console/oauth/{source}/callback")
async def callback(source: str, request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if not settings.console_enabled or source not in SOURCES:
        return JSONResponse({"error": "not_found"}, status_code=404)
    record = await run_in_threadpool(_take_state, state) if state else None
    if record is None or record.get("source") != source:
        return _fail("oauth_state")
    browser = _session_hash(request)
    if not browser or not _same(browser, str(record.get("session") or "")):
        return _fail("oauth_state")
    try:
        session = await _admin_session(request)
    except HTTPException:
        return _fail("oauth_state")
    if session.org != record.get("org") or getattr(session, "id_hash", "") != browser:
        return _fail("oauth_state")
    if error or not code:
        return _fail("oauth_denied" if error == "access_denied" else "oauth_failed")
    return await run_in_threadpool(_complete, source, session, code)


def _complete(source: str, session, code: str) -> RedirectResponse:
    """Exchange the code and store the install: blocking calls, so on a worker thread."""
    connector = _connector(source)
    spec = getattr(connector, "oauth", None)
    if spec is None or not oauth.configured(spec):
        return _fail("oauth_failed")
    try:
        tokens = oauth.exchange_code(spec, code, oauth.callback_url(source))
        with org_context(session.org):
            finish = _finish_jira if source == "jira" else _finish_hubspot
            return finish(session, tokens)
    except Exception as failure:
        # Any failure is the same fixed page, and the log gets its type only: a vendor message could carry anything.
        log.warning("[console-oauth] %s connect for org %s failed: %s", source, session.org, type(failure).__name__)
        return _fail("oauth_failed")


# --- per vendor ----------------------------------------------------------------------------------


def _existing(source: str, external_account: str):
    """The org's install of this vendor account, if it has one already (a reconnect)."""
    from src.connectors import store

    return next((row for row in store.list_installs(source) if row.external_account == external_account), None)


def _store_install(session, source: str, external_account: str | None, site: str | None, config: dict, secret: dict) -> str:
    """Claim the account, then create the install, or replace the tokens of the org's existing one.
    Returns the install id, or raises ``_Claimed`` (another org holds the account) or ``_Removing``
    (the org's install of it is being disconnected)."""
    from src.connectors import store

    if external_account is None:
        # Several Jira sites: no account yet. jira_oauth.choose_site claims one later, under the same lock.
        created = store.create_install(session.org, source, None, "oauth", status="connecting", config=config, secret=secret, created_by=getattr(session, "person", None))
        return created.key
    with oauth.account_lock(external_account):
        return _claim_and_store(session, source, external_account, site, config, secret)


def _claim_and_store(session, source: str, external_account: str, site: str | None, config: dict, secret: dict) -> str:
    """``_store_install`` under the account's lock, so a second callback for the account finds the first's install."""
    from src.connectors import store
    from src.domain import orgs

    newly_claimed = orgs.external_account_org(external_account) is None
    if not store.claim_external_account(external_account, session.org):
        raise _Claimed()
    current = _existing(source, external_account)
    if current is not None and current.status == "removing":
        # Its data is being deleted and its claim released: reconnect once that is done.
        raise _Removing()
    if current is not None:
        store.update_config(current.id, config, site=site)
        store.set_secret(current.id, secret)
        store.set_status(current.id, "active" if current.status in ("active", "error") else "connecting")
        installed = store.to_install(store.get(current.id))
        oauth.forget(installed)
        if current.status in ("active", "error"):
            from src.connectors import sync

            registry.put(installed)
            sync.rearm(installed)  # a credentials back-off must not outlast the reconnect
        return current.id
    try:
        created = store.create_install(
            session.org, source, None, "oauth", status="connecting", external_account=external_account, site=site,
            config=config, secret=secret, created_by=getattr(session, "person", None),
        )
    except Exception:
        if newly_claimed:
            orgs.release_external_account(external_account, session.org)
        raise
    return created.key


class _Claimed(Exception):
    pass


class _Removing(Exception):
    pass


def _finish_jira(session, tokens: oauth.Tokens) -> RedirectResponse:
    from src.connectors.jira import oauth as jira_oauth

    sites = jira_oauth.accessible_resources(tokens.access_token)
    if not sites:
        return _fail("oauth_failed")
    secret = tokens.as_secret()
    if len(sites) > 1:
        # The admin picks one (POST /console/api/connections/{id}/site, jira_oauth.choose_site); until then
        # the install has no cloud_id, so nothing is read and no account is claimed.
        install_id = _store_install(session, "jira", None, None, {}, secret)
        return _go(f"/console/connect/jira/site?id={urllib.parse.quote(install_id, safe='')}")
    site = sites[0]
    try:
        _store_install(session, "jira", site.external_account, site.url, {"cloud_id": site.cloud_id, "site_url": site.url}, secret)
    except _Claimed:
        return _fail("account_claimed")
    return _connected("jira")


def _finish_hubspot(session, tokens: oauth.Tokens) -> RedirectResponse:
    from src.connectors.hubspot import oauth as hubspot_oauth
    from src.connectors.hubspot.client import HubSpotClient

    meta = hubspot_oauth.token_metadata(tokens.access_token)
    hub_id = meta["hub_id"]
    issued = str(tokens.extra.get("hub_id") or hub_id)
    details = HubSpotClient(tokens.access_token).account_details()
    if issued != hub_id or str(details.get("portalId") or "") != hub_id:
        raise ConnectorError("HubSpot named different portals for one token")
    ui_domain = str(details.get("uiDomain") or "app.hubspot.com")
    site = f"https://{ui_domain}" if hubspot_oauth.UI_DOMAIN.fullmatch(ui_domain) else "https://app.hubspot.com"
    try:
        _store_install(session, "hubspot", f"hubspot:{hub_id}", site, {"hub_id": hub_id}, tokens.as_secret())
    except _Claimed:
        return _fail("account_claimed")
    return _connected("hubspot")
