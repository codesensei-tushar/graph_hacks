"""Connector webhooks, and the admin routes to see and nudge the sync.

``POST /connectors/{source}/webhook`` takes a delivery for a configured connector that
accepts webhooks (404 otherwise), refuses one its connector cannot verify (401), answers a
redelivery of the same bytes as a duplicate, and does the work in a background task, so
the sender gets its answer at once. A delivery is marked seen only after it ran.

``GET /ops/connectors`` (status: accounts, containers, cursors, errors; never a config
value; plus ``orphaned``: installs whose data is still in the graph though nothing configures
them any more), ``POST /ops/connectors/{source}/sync`` (``?full=true`` starts every container
over) and ``DELETE /ops/connectors/orphans/{install}`` (deletes an orphan's data, 500 nodes at a
time) need ``BRAIN_ADMIN_TOKEN``. ``?org=<id>`` reads another org than the legacy one.
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from src.api.v1.slack_install import _admin_denial
from src.connectors import registry, sync
from src.connectors.base import WebhookRequest
from src.core import tenancy
from src.core.config import settings

log = logging.getLogger(__name__)
router = APIRouter(tags=["connectors"])

MAX_DELIVERY_BYTES = 1024 * 1024


def _public_url(request: Request) -> str:
    """The URL the sender signed: the public origin (nginx terminates TLS in front of us), path and query."""
    origin = settings.public_url or str(request.base_url).rstrip("/")
    query = request.url.query
    return f"{origin}{request.url.path}" + (f"?{query}" if query else "")


@router.post("/connectors/{source}/webhook")
async def connector_webhook(source: str, request: Request, background: BackgroundTasks):
    connector, install = registry.get(source), registry.install_for(source)
    if connector is None or install is None or not connector.accepts_webhooks:
        return JSONResponse({"error": "no such connector"}, status_code=404)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_DELIVERY_BYTES:
            return JSONResponse({"error": "too_large", "limit_bytes": MAX_DELIVERY_BYTES}, status_code=413)
    delivery = WebhookRequest(
        method=request.method,
        url=_public_url(request),
        headers={key.lower(): value for key, value in request.headers.items()},
        body=bytes(body),
    )
    try:
        verified = await run_in_threadpool(connector.verify_webhook, install, delivery)
    except Exception:
        log.exception("[connectors] %s webhook verification failed", source)
        verified = False
    if not verified:
        return JSONResponse({"error": "invalid signature"}, status_code=401)
    key = sync.delivery_key(source, delivery.body)
    if await run_in_threadpool(sync.is_duplicate, key):
        return {"ok": True, "duplicate": True}
    background.add_task(sync.handle_delivery, source, delivery, key)
    return {"ok": True}


def _ops_org(org: str | None) -> tenancy.OrgRef | None:
    try:
        return tenancy.resolve(org or tenancy.LEGACY_ORG_ID)
    except (LookupError, TypeError):
        return None


@router.get("/ops/connectors")
def connectors_status(request: Request, org: str | None = None):
    if denied := _admin_denial(request):
        return denied
    if (ref := _ops_org(org)) is None:
        return JSONResponse({"error": "no such org"}, status_code=404)
    with tenancy.org_context(ref):
        rows = sync.status() if tenancy.is_legacy(ref) else [sync.install_status(c, i) for c, i in registry.installs_of(ref.id)]
        return {"connectors": rows, "orphaned": sync.orphans()}


@router.delete("/ops/connectors/orphans/{install_key}")
def purge_orphan(install_key: str, request: Request, org: str | None = None):
    if denied := _admin_denial(request):
        return denied
    if (ref := _ops_org(org)) is None:
        return JSONResponse({"error": "no such org"}, status_code=404)
    with tenancy.org_context(ref):
        if not sync.purge_orphan(install_key, actor="operator"):
            return JSONResponse({"error": "not an orphaned install: it is configured, or left nothing"}, status_code=409)
    return JSONResponse({"ok": True, "install": install_key, "status": "purging"}, status_code=202)


@router.post("/ops/connectors/{source}/sync")
def connector_sync_now(source: str, request: Request, full: bool = False):
    if denied := _admin_denial(request):
        return denied
    if not sync.sync_now(source, full=full):
        return JSONResponse({"error": "not configured", "source": source}, status_code=404)
    return {"ok": True, "source": source, "full": full}
