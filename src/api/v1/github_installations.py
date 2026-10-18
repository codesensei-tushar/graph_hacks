"""The operator's GitHub installations: connect or bind one to any org, the legacy org included.

All need ``BRAIN_ADMIN_TOKEN``; ``?org=<id>`` names the org (default the legacy org).

    POST   /ops/github/connect                          {install_url}: whoever installs from it must also
                                                        sign in with GitHub (proving they can reach the
                                                        installation) before it is bound to the org
    GET    /ops/github/installations                    every installation of the app, and the org it feeds
    POST   /ops/github/installations/{id}/bind          {backfill?}: bind it to the org and start its backfill.
                                                        The admin token is the trust; the app's JWT proves
                                                        the installation is this app's
    POST   /ops/github/installations/{id}/sync          {backfill?}: read every repo again ("all" or N)
    DELETE /ops/github/installations/{id}               disconnect: its data is deleted in batches
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from src.api.v1.slack_install import _admin_denial, _admin_org
from src.connectors import oauth as connector_oauth
from src.core import tenancy
from src.domain.github import backfill, connections, installations
from src.integrations.github import client

log = logging.getLogger(__name__)
router = APIRouter(tags=["github-installations"], include_in_schema=False)

BAD_LIMIT = {"error": "bad_limit", "message": 'backfill is "all" or a number of pull requests up to 10000.'}
NOT_CONNECTED = {"error": "not_found", "message": "That installation is not connected to an org."}


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _depth(body: dict):
    """None when absent; -1 when it is not "all" or 0..MAX_LIMIT."""
    value = body.get("backfill", body.get("backfill_limit"))
    if value is None:
        return None
    try:
        return backfill.clamp_limit(0 if value == "all" else value)
    except ValueError:
        return -1


def _org(request: Request):
    return _admin_org(request.query_params.get("org"))


@router.post("/ops/github/connect")
async def connect(request: Request):
    from src.api.v1 import console_github

    if denied := _admin_denial(request):
        return denied
    org = _org(request)
    if org is None:
        return JSONResponse({"error": "no_org", "message": "No such org."}, status_code=404)
    if not await run_in_threadpool(console_github.can_connect):
        return JSONResponse({"error": "not_configured", "message": "Register the app first (its slug, client id and secret), and set PUBLIC_URL."}, status_code=409)
    return {"install_url": await run_in_threadpool(console_github.mint_state, org.id, None, None, "operator"), "org": org.id}


def _list() -> list[dict]:
    found = []
    for item in client.list_app_installations():
        binding = installations.get(item["id"]) or {}
        account = item.get("account") or {}
        live = binding.get("status") in (installations.ACTIVE, installations.REMOVING)
        found.append({
            "id": str(item["id"]), "account": account.get("login") or account.get("slug"), "account_type": account.get("type"),
            "suspended": bool(item.get("suspended_at")), "org": binding.get("org") if live else None, "status": binding.get("status") or "unbound",
        })
    return found


@router.get("/ops/github/installations")
async def listing(request: Request):
    if denied := _admin_denial(request):
        return denied
    try:
        return {"installations": await run_in_threadpool(_list)}
    except Exception as error:
        log.warning("[github] listing installations failed: %s", type(error).__name__)
        return JSONResponse({"error": "unreachable", "message": "GitHub could not be asked for the app's installations. Is the app registered?"}, status_code=502)


def _bind(installation_id: int, org: tenancy.OrgRef, limit):
    try:
        found = client.get_installation(installation_id)
    except Exception as error:
        log.warning("[github] installation=%s lookup failed: %s", installation_id, type(error).__name__)
        return JSONResponse({"error": "not_found", "message": "This app has no such installation."}, status_code=404)
    account = found.get("account") or {}
    if not account.get("id"):
        return JSONResponse({"error": "not_found", "message": "This app has no such installation."}, status_code=404)
    with connector_oauth.account_lock(installations.account_key(account["id"])):
        try:
            installations.bind(installation_id, org.id, account, connected_by="operator", backfill_limit=limit)
        except installations.Claimed:
            return JSONResponse({"error": "account_claimed", "message": "That GitHub account already feeds another org."}, status_code=409)
    with tenancy.org_context(org):
        connections.start_backfill(installation_id)
        connections.ensure_collaborator_refresh()
        return connections.status(installations.get(installation_id))


def _audit(org_id: str, action: str, target: str) -> None:
    from src.domain.console import audit

    audit.record(org_id, "operator", action, target, "ok")


@router.post("/ops/github/installations/{installation_id}/bind")
async def bind(installation_id: int, request: Request):
    if denied := _admin_denial(request):
        return denied
    org = _org(request)
    if org is None:
        return JSONResponse({"error": "no_org", "message": "No such org."}, status_code=404)
    limit = _depth(await _body(request))
    if limit == -1:
        return JSONResponse(BAD_LIMIT, status_code=400)
    result = await run_in_threadpool(_bind, installation_id, org, limit)
    if isinstance(result, dict):
        _audit(org.id, "github.bind", str(installation_id))
    return result


def _bound(installation_id: int):
    current = installations.get(installation_id)
    if current is None or current["status"] not in (installations.ACTIVE, installations.REMOVING) or not current["org"]:
        return None, None
    return tenancy.resolve(current["org"]), current


@router.post("/ops/github/installations/{installation_id}/sync")
async def sync(installation_id: int, request: Request):
    if denied := _admin_denial(request):
        return denied
    limit = _depth(await _body(request))
    if limit == -1:
        return JSONResponse(BAD_LIMIT, status_code=400)

    def run():
        org, current = _bound(installation_id)
        if org is None or current["status"] != installations.ACTIVE:
            return JSONResponse(NOT_CONNECTED, status_code=404)
        with tenancy.org_context(org):
            connections.resync(installation_id, limit)
        _audit(org.id, "github.sync", str(installation_id))
        return {"ok": True, "org": org.id}

    return await run_in_threadpool(run)


@router.delete("/ops/github/installations/{installation_id}")
async def disconnect(installation_id: int, request: Request):
    if denied := _admin_denial(request):
        return denied

    def run():
        org, current = _bound(installation_id)
        if org is None:
            return JSONResponse(NOT_CONNECTED, status_code=404)
        with tenancy.org_context(org):
            if current["status"] == installations.ACTIVE:
                connections.disconnect(installation_id, forget=False)
        _audit(org.id, "github.disconnect", str(installation_id))
        return JSONResponse({"ok": True, "status": "removing", "org": org.id}, status_code=202)

    return await run_in_threadpool(run)
