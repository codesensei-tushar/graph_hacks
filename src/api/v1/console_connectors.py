"""The console's JSON API for connections, and its ask. The console UI uses only this.

    GET    /console/api/connections                    every connection of the org, with sync status
    GET    /console/api/connections/{id}/sites         Jira sites to choose from, when more than one (admins)
    POST   /console/api/connections/{id}/site          {cloud_id}: the chosen site (admins)
    GET    /console/api/connections/{id}/available     [{external_id, key, name}]: containers to select (admins)
    PUT    /console/api/connections/{id}/containers    {selected: [external_id]} (admins)
    POST   /console/api/connections/{id}/sync          fetch now (admins)
    DELETE /console/api/connections/{id}               disconnect: removes everything it wrote (admins)
    POST   /console/api/ask                            {q}: the /api/ask shape, with the asker's own scopes

Connecting a source (``POST /console/api/connectors/{source}/connect``) and the vendor callbacks
are package D's (``console_oauth.py``); the Jira site routes here call D's
``jira.oauth.sites_for`` and ``choose_site``.

Everything is 404 unless ``CONSOLE_ENABLED``. Sessions come from ``console_user`` /
``console_admin`` (members view and ask; owners and admins change things), which also check
CSRF (``Origin``, ``X-CSRF-Token``, JSON) on every cookie mutation. Every route runs in the
session's org (``tenancy.org_context``), so it only ever sees that org's graph. Nothing secret
is returned, and vendor error text is never echoed.

``{id}`` is the install key, ``<source>:<install>``.
"""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from src.connectors import registry, store, sync
from src.connectors.base import Install
from src.core import tenancy
from src.core.config import settings
from src.db.falkordb_client import kv
from src.domain.console.session import console_admin, console_user

log = logging.getLogger(__name__)

MODEL_QUESTIONS_PER_ORG_PER_DAY = 200
MAX_SELECTED = 200
_ADMIN_ROLES = ("owner", "admin")

def _enabled() -> None:
    if not settings.console_enabled:
        raise HTTPException(status_code=404, detail="Not Found")


router = APIRouter(prefix="/console/api", tags=["console"], dependencies=[Depends(_enabled)])


def _get(session, name: str):
    return session.get(name) if isinstance(session, dict) else getattr(session, name, None)


def _org(session) -> tenancy.OrgRef:
    org_id = _get(session, "org")
    org_id = org_id.get("id") if isinstance(org_id, dict) else getattr(org_id, "id", org_id)
    try:
        return tenancy.resolve(org_id)
    except (LookupError, TypeError) as error:
        raise HTTPException(status_code=403, detail="no org") from error


def _actor(session) -> str:
    if isinstance(getattr(session, "actor", None), str):
        return session.actor
    person = _get(session, "person")
    person = person.get("id") if isinstance(person, dict) else getattr(person, "id", person)
    return str(person or _get(session, "slack_user") or "unknown")


async def _json(request: Request) -> dict:
    if not (request.headers.get("content-type") or "").startswith("application/json"):
        raise HTTPException(status_code=415, detail="send JSON")
    try:
        body = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="invalid JSON") from error
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="send a JSON object")
    return body


def _stored(install_key: str) -> store.ConnectorInstall:
    stored = store.get(install_key)
    if stored is None:
        raise HTTPException(status_code=404, detail="no such connection")
    return stored


def _connector(source: str):
    connector = registry.get(source)
    if connector is None and source in registry.KNOWN_MODULES:
        import importlib

        importlib.import_module(registry.KNOWN_MODULES[source])
        connector = registry.get(source)
    if connector is None:
        raise HTTPException(status_code=404, detail="no such connector")
    return connector


# --- connections ------------------------------------------------------------------------


def _row(install: Install, status: str, *, account=None, site=None, created_by=None, created_at=None, error=None) -> dict:
    connector = registry.get(install.source)
    sync_status = sync.install_status(connector, install) if connector else {"containers": []}
    containers = [
        {key: container.get(key) for key in ("id", "name", "items", "backfilled", "capped", "last_fetch_at", "last_error")}
        for container in sync_status["containers"]
    ]
    return {
        "id": install.key,
        "source": install.source,
        "status": status,
        "account": sync_status.get("account") or account,
        "site": sync_status.get("site") or site,
        "auth": install.auth,
        "created_by": created_by,
        "created_at": created_at,
        "items": sum(int(container["items"] or 0) for container in containers),
        "last_sync_at": sync_status.get("last_sync_at"),
        "last_error": error or sync_status.get("last_error"),
        "containers": containers,
    }


def list_connections(org: tenancy.OrgRef) -> list[dict]:
    rows = []
    if tenancy.is_legacy(org):
        for _connector_, install in registry.installs():
            rows.append(_row(install, "active"))
    for stored in store.list_installs():
        install = Install(source=stored.source, id=stored.install, org=stored.org, auth=stored.auth)
        rows.append(
            _row(
                install, stored.status, account=stored.external_account, site=stored.site,
                created_by=stored.created_by, created_at=stored.created_at, error=stored.last_error,
            )
        )
    return rows


@router.get("/connections")
def connections(session=Depends(console_user)):
    org = _org(session)
    with tenancy.org_context(org):
        return list_connections(org)


def _jira_oauth():
    try:
        from src.connectors.jira import oauth  # package D
    except ImportError:
        return None
    return oauth


@router.get("/connections/{install_key}/sites")
async def sites(install_key: str, session=Depends(console_admin)):
    """The Jira sites the install's grant covers: ``[{cloud_id, name, url}]``."""
    oauth = _jira_oauth()
    if oauth is None:
        return JSONResponse({"error": "Jira sign-in is not available yet"}, status_code=503)
    org = _org(session)
    with tenancy.org_context(org):
        stored = _stored(install_key)
        if stored.source != "jira" or stored.auth != "oauth":
            return JSONResponse({"error": "only a Jira connection made by sign-in has sites"}, status_code=400)
        install = store.to_install(stored)
    try:
        found = await run_in_threadpool(tenancy.in_org, org, oauth.sites_for, install)
    except Exception as error:
        log.warning("[console] Jira sites failed install=%s: %s", install_key, type(error).__name__)
        return JSONResponse({"error": "Jira could not be reached; try again"}, status_code=502)
    return [{"cloud_id": site.cloud_id, "name": site.name, "url": site.url} for site in found]


@router.post("/connections/{install_key}/site")
async def choose_site(install_key: str, request: Request, session=Depends(console_admin)):
    """``{cloud_id}``: point the install at one site. Answers the install id to continue with
    (the org's existing install of that site, when it has one)."""
    oauth = _jira_oauth()
    if oauth is None:
        return JSONResponse({"error": "Jira sign-in is not available yet"}, status_code=503)
    body = await _json(request)
    cloud_id = body.get("cloud_id")
    if not isinstance(cloud_id, str) or not cloud_id:
        return JSONResponse({"error": "choose one of the listed sites"}, status_code=400)
    org = _org(session)
    try:
        chosen = await run_in_threadpool(oauth.choose_site, org.id, install_key, cloud_id)
    except LookupError:
        return JSONResponse({"error": "choose one of the listed sites"}, status_code=400)
    except PermissionError:
        return JSONResponse({"error": "that Jira site is already connected to another org"}, status_code=409)
    except Exception as error:
        log.warning("[console] Jira site choice failed install=%s: %s", install_key, type(error).__name__)
        return JSONResponse({"error": "Jira could not be reached; try again"}, status_code=502)
    _audit(org.id, session, "connector.site", chosen)
    return {"ok": True, "id": chosen}


@router.get("/connections/{install_key}/available")
async def available(install_key: str, session=Depends(console_admin)):
    org = _org(session)
    with tenancy.org_context(org):
        stored = _stored(install_key)
        connector = _connector(stored.source)
        install = store.to_install(stored)
    try:
        found = await run_in_threadpool(tenancy.in_org, org, connector.available_containers, install)
    except Exception as error:
        log.warning("[console] available containers failed install=%s: %s", install_key, type(error).__name__)
        return JSONResponse({"error": "the source could not be reached; try again"}, status_code=502)
    return [{"external_id": c.external_id, "key": c.key, "name": c.name} for c in found]


@router.put("/connections/{install_key}/containers")
async def select_containers(install_key: str, request: Request, session=Depends(console_admin)):
    body = await _json(request)
    selected = body.get("selected")
    if not isinstance(selected, list) or len(selected) > MAX_SELECTED or not all(isinstance(item, str) for item in selected):
        return JSONResponse({"error": f"selected is a list of at most {MAX_SELECTED} ids"}, status_code=400)
    org = _org(session)
    with tenancy.org_context(org):
        stored = _stored(install_key)
        if stored.status == "removing":
            return JSONResponse({"error": "this connection is being removed"}, status_code=409)
        connector = _connector(stored.source)
        if not connector.container_field:
            return JSONResponse({"error": "this connector syncs everything it can see"}, status_code=400)
        install = store.to_install(stored)
    try:
        found = await run_in_threadpool(tenancy.in_org, org, connector.available_containers, install)
    except Exception as error:
        log.warning("[console] available containers failed install=%s: %s", install_key, type(error).__name__)
        return JSONResponse({"error": "the source could not be reached; try again"}, status_code=502)
    by_id = {c.external_id: c for c in found}
    unknown = [item for item in selected if item not in by_id]
    if unknown:
        return JSONResponse({"error": "some selected ids are not available", "unknown": unknown[:20]}, status_code=400)
    with tenancy.org_context(org):
        updated = store.update_config(install_key, {connector.container_field: ",".join(by_id[item].key for item in selected)})
        if updated is None:
            return JSONResponse({"error": "no such connection"}, status_code=404)
        store.set_status(install_key, "active", only_from=("connecting",))
        registry.put(store.to_install(store.get(install_key) or updated))
        # A DELETE may have run since the check above: re-read after the put, and undo it if so.
        current = store.get(install_key)
        if current is None or current.status == "removing":
            registry.drop(org.id, updated.source, updated.install)
            sync.cancel(Install(source=updated.source, id=updated.install, org=org.id, auth=updated.auth))
            return JSONResponse({"error": "this connection is being removed"}, status_code=409)
        sync.sync_now(updated.source, full=False, install_id=updated.install)
        _audit(org.id, session, "connector.containers", install_key)
    return {"ok": True, "selected": len(selected)}


@router.post("/connections/{install_key}/sync")
def sync_connection(install_key: str, request: Request, session=Depends(console_admin)):
    org = _org(session)
    with tenancy.org_context(org):
        source, _, install_id = install_key.partition(":")
        if tenancy.is_legacy(org) and (env := registry.install_for(source)) is not None and env.key == install_key:
            sync.sync_now(source)
            return {"ok": True}
        stored = _stored(install_key)
        if stored.status == "removing":
            return JSONResponse({"error": "this connection is being removed"}, status_code=409)
        if not sync.sync_now(stored.source, install_id=stored.install):
            return JSONResponse({"error": "this connection is not set up yet"}, status_code=409)
        _audit(org.id, session, "connector.sync", install_key)
        return {"ok": True}


@router.delete("/connections/{install_key}")
def disconnect(install_key: str, request: Request, session=Depends(console_admin)):
    org = _org(session)
    with tenancy.org_context(org):
        stored = store.get(install_key)
        if stored is None:
            source = install_key.partition(":")[0]
            env = registry.install_for(source) if tenancy.is_legacy(org) else None
            if env is not None and env.key == install_key:
                return JSONResponse({"error": "this connection is set by the server's environment"}, status_code=409)
            return JSONResponse({"error": "no such connection"}, status_code=404)
        if stored.status == "removing":
            return JSONResponse({"ok": True, "status": "removing"}, status_code=202)
        sync.remove_install(install_key, actor=_actor(session))
        _audit(org.id, session, "connector.disconnect.start", install_key)
        return JSONResponse({"ok": True, "status": "removing"}, status_code=202)


def _audit(org_id: str, session, action: str, target: str) -> None:
    sync._audit(org_id, _actor(session), action, target, "ok")


# --- ask -------------------------------------------------------------------------------


def read_scopes(session) -> list[str]:
    """The asker's own read surface, as in their Slack DM: org_shared, org_internal (console users are
    full members), their private channels, private repos and restricted connector projects."""
    from src.domain.memory.read_scope import resolve_brain_read_container_tags
    from src.domain.memory.writeback import SlackMemoryScope

    person = _get(session, "person")
    person = person.get("id") if isinstance(person, dict) else getattr(person, "id", person)
    scope = SlackMemoryScope(kind="dm", user_id=person, slack_user_id=_get(session, "slack_user"))
    return resolve_brain_read_container_tags(scope)


def _model_allowed(org: tenancy.OrgRef) -> bool:
    """Admins' questions go to the model while the org has model questions left today."""
    if not settings.llm_enabled:
        return False
    key = f"console:ask:model:{org.id}:{time.strftime('%Y%m%d', time.gmtime())}"
    used = kv().incr(key)
    if used == 1:
        kv().expire(key, 2 * 24 * 60 * 60)
    return used <= MODEL_QUESTIONS_PER_ORG_PER_DAY


@router.post("/ask")
async def ask(request: Request, session=Depends(console_user)):
    body = await _json(request)
    question = body.get("q")
    if not isinstance(question, str) or not question.strip():
        return JSONResponse({"error": "ask a question"}, status_code=400)
    org = _org(session)
    return await run_in_threadpool(tenancy.in_org, org, _answer, org, session, question)


def _answer(org: tenancy.OrgRef, session, question: str):
    from src.domain.knowledge import ask_api

    use_model = _get(session, "role") in _ADMIN_ROLES and _model_allowed(org)
    try:
        answer = ask_api.answer_question(question, use_model=use_model, scopes=read_scopes(session))
    except (ask_api.ToolRefused, ask_api.QuestionTooLong) as refused:
        return JSONResponse({"error": str(refused)}, status_code=400)
    return answer.to_dict()
