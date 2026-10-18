"""GitHub App webhooks and the `link github` sign-in callback.

GitHub waits 10 seconds for a webhook answer, so the route verifies the signature (with the
app's secret: env, or the registered app), dedupes on ``X-GitHub-Delivery``, finds the org the
installation is bound to (``installations.route``), returns, and does the work in a background
task inside that org's context, so it writes that org's graph only. A delivery for no org (an
installation nobody connected, one being removed) is acknowledged and dropped. A delivery is
marked seen only after it ran, so a redelivery can retry a failure. A route lookup that fails
answers 503, so the delivery shows as failed in GitHub and can be redelivered.
"""

import html
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from src.db.falkordb_client import kv
from src.core import scheduler, tenancy
from src.domain.github import connections, installations
from src.domain.github.account_link import complete_github_link
from src.domain.github.webhooks import handle_github_event
from src.integrations.github import app_config
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
    creds = await run_in_threadpool(app_config.current)
    if creds is None:
        return JSONResponse({"error": "GitHub is not configured"}, status_code=503)
    raw_body = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}
    if not verify_github_signature(raw_body, headers, creds.webhook_secret):
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
    try:
        org, reason = await run_in_threadpool(installations.route, event, payload, creds.source == "env")
    except Exception:
        log.exception("[github] routing event=%s failed", event)
        return JSONResponse({"error": "try again"}, status_code=503)
    if org is None:
        log.info("[github] event=%s action=%s installation=%s dropped: %s", event, payload.get("action") or "-", (payload.get("installation") or {}).get("id"), reason)
        if reason == "connect_in_flight" and event == "installation" and payload.get("action") == "created":
            await run_in_threadpool(_check_later, payload)
        return {"ok": True, "routed": False}
    background.add_task(tenancy.in_org, org, _run_and_mark, key, event, payload)
    return {"ok": True}


RECHECK_JOB = "runGitHubUnroutedInstallation"


def _check_later(payload: dict) -> None:
    """A new installation arrived while a console Connect was in flight (env app): look again once
    that Connect has had time to bind it. If nobody did, it is the legacy org's, as before."""
    installation = payload.get("installation") or {}
    small = {"action": "created", "installation": {"id": installation.get("id"), "account": installation.get("account") or {}}}
    with tenancy.org_context(tenancy.legacy_org()):
        scheduler.schedule(installations.INFLIGHT_SECONDS, RECHECK_JOB, small, job_id=f"github:unrouted:{installation.get('id')}")


def run_recheck(payload: dict) -> None:
    from src.integrations.github import app_config

    creds = app_config.current()
    if creds is None:
        return
    org, reason = installations.route("installation", payload, creds.source == "env")
    if org is None:
        if reason == "connect_in_flight":
            _check_later(payload)
        return
    with tenancy.org_context(org):
        connections.start_backfill(int(payload["installation"]["id"]))


scheduler.register(RECHECK_JOB, run_recheck)


def _page(message: str, status_code: int = 200) -> HTMLResponse:
    body = (
        "<!doctype html><meta charset='utf-8'><title>Company Brain</title>"
        "<body style='font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem'>"
        f"<p>{message}</p></body>"
    )
    return HTMLResponse(body, status_code=status_code)


@router.get("/oauth/callback")
def github_oauth_callback(code: str = "", state: str = ""):
    if not app_config.link_enabled():
        return _page("GitHub sign-in is not configured on this server.", 503)
    if not code or not state:
        return _page("GitHub didn't send a sign-in code. Send <code>link github</code> to the bot again.", 400)
    result = complete_github_link(state, code)
    return _page(html.escape(result.message).replace("`link github`", "<code>link github</code>"), 200 if result.ok else 400)
