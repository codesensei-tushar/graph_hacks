"""Connector webhooks, and the admin routes to see and nudge the sync.

``POST /connectors/{source}/webhook`` takes a delivery for a configured connector that
accepts webhooks (404 otherwise), refuses one its connector cannot verify (401), answers a
redelivery of the same bytes as a duplicate, and does the work in a background task, so
the sender gets its answer at once. A delivery is marked seen only after it ran.

``GET /ops/connectors`` (status: accounts, containers, cursors, errors; never a config
value) and ``POST /ops/connectors/{source}/sync`` (``?full=true`` starts every container
over) need ``BRAIN_ADMIN_TOKEN``.
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from src.api.v1.slack_install import _admin_denial
from src.connectors import registry, sync
from src.connectors.base import WebhookRequest
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


@router.get("/ops/connectors")
def connectors_status(request: Request):
    if denied := _admin_denial(request):
        return denied
    return {"connectors": sync.status()}


@router.post("/ops/connectors/{source}/sync")
def connector_sync_now(source: str, request: Request, full: bool = False):
    if denied := _admin_denial(request):
        return denied
    if not sync.sync_now(source, full=full):
        return JSONResponse({"error": "not configured", "source": source}, status_code=404)
    return {"ok": True, "source": source, "full": full}
