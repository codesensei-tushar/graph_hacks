"""GitHub App webhooks and the `link github` sign-in callback.

GitHub waits 10 seconds for a webhook answer, so the route verifies the signature,
dedupes on ``X-GitHub-Delivery``, returns, and does the work in a background task.
A delivery is marked seen only after it ran, so a redelivery can retry a failure.
"""

import html
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse

from src.core.config import settings
from src.db.falkordb_client import kv
from src.domain.github.account_link import complete_github_link
from src.domain.github.webhooks import handle_github_event
from src.integrations.github.verify import verify_github_signature

log = logging.getLogger(__name__)
router = APIRouter(prefix="/github", tags=["github"])

DELIVERY_DEDUP_TTL_SECONDS = 60 * 60 * 24


def _delivery_key(delivery_id: str) -> str:
    return f"github:delivery:{delivery_id}"


def _run_and_mark(key: str, event: str, payload: dict) -> None:
    try:
        outcome = handle_github_event(event, payload)
        kv().set(key, "1", ex=DELIVERY_DEDUP_TTL_SECONDS)
        log.info("[github] event=%s action=%s outcome=%s", event, payload.get("action") or "-", outcome)
    except Exception:
        log.exception("[github] event=%s action=%s failed", event, payload.get("action") or "-")


@router.post("/webhook")
async def github_webhook(request: Request, background: BackgroundTasks):
    if not settings.github_enabled:
        return JSONResponse({"error": "GitHub is not configured"}, status_code=503)
    raw_body = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}
    if not verify_github_signature(raw_body, headers, settings.github_webhook_secret):
        return JSONResponse({"error": "invalid signature"}, status_code=401)
    try:
        payload = json.loads(raw_body)
    except ValueError:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    event = headers.get("x-github-event", "")
    if event == "ping":
        return {"ok": True, "pong": True}
    delivery = headers.get("x-github-delivery", "")
    key = _delivery_key(delivery)
    if delivery and kv().exists(key):
        return {"ok": True, "duplicate": True}
    background.add_task(_run_and_mark, key, event, payload)
    return {"ok": True}


def _page(message: str, status_code: int = 200) -> HTMLResponse:
    body = (
        "<!doctype html><meta charset='utf-8'><title>Company Brain</title>"
        "<body style='font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem'>"
        f"<p>{message}</p></body>"
    )
    return HTMLResponse(body, status_code=status_code)


@router.get("/oauth/callback")
def github_oauth_callback(code: str = "", state: str = ""):
    if not settings.github_link_enabled:
        return _page("GitHub sign-in is not configured on this server.", 503)
    if not code or not state:
        return _page("GitHub didn't send a sign-in code. Send <code>link github</code> to the bot again.", 400)
    result = complete_github_link(state, code)
    return _page(html.escape(result.message).replace("`link github`", "<code>link github</code>"), 200 if result.ok else 400)
