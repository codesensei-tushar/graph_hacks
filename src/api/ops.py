"""Deploy plumbing, not bot behavior: ``/healthz``, the reserved ``/mcp`` path, a seed
refresh for production, and a clock for the last verified Slack request.

``deploy/deploy.sh`` polls ``/healthz`` until ``git_sha`` matches the commit it shipped.
``slack_last_event_at`` moves when ``/slack/events`` or ``/slack/interactions`` answers
200, which those routes only do after the signature checks out (unsigned requests get
401). The clock lives in this process, so it resets on restart.
"""

import dataclasses
import logging
import os
from datetime import UTC, datetime

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.api.v1.slack_install import _admin_denial
from src.core.config import settings
from src.db.falkordb_client import connect
from src.seed.seed import company_module, seed_company

log = logging.getLogger(__name__)
router = APIRouter(tags=["ops"])

GIT_SHA = os.getenv("GIT_SHA", "unknown")
SLACK_PATHS = frozenset({"/slack/events", "/slack/interactions"})

_slack_clock: dict[str, datetime | None] = {"last_event_at": None}


def _falkordb_status() -> str:
    try:
        _, graph = connect()
        graph.ro_query("RETURN 1")
        return "ok"
    except Exception:
        log.warning("healthz: FalkorDB check failed", exc_info=True)
        return "error"


@router.get("/healthz")
def healthz():
    last = _slack_clock["last_event_at"]
    return {
        "status": "ok",
        "git_sha": GIT_SHA,
        "falkordb": _falkordb_status(),
        "slack_last_event_at": last.isoformat() if last else None,
    }


@router.post("/ops/seed")
def seed(request: Request):
    """Refresh the configured company's seed in place. Merges by id and never deletes, so
    memories, workspaces and GitHub history survive; the counts say what changed."""
    if denied := _admin_denial(request):
        return denied
    report = seed_company(company_module(settings.seed_company))
    log.info("[ops] seed merged company=%s created nodes=%d edges=%d set=%d", report.company, report.nodes_created, report.edges_created, report.properties_set)
    return {"company": settings.seed_company, "mode": "merge", **dataclasses.asdict(report)}


@router.api_route("/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
@router.api_route("/mcp/{_rest:path}", methods=["GET", "POST", "DELETE"], include_in_schema=False)
def mcp_reserved():
    # nginx already proxies /mcp unbuffered with a 1h read timeout for streamed responses.
    return JSONResponse({"error": "MCP is not implemented yet"}, status_code=501)


class SlackEventClock:
    """ASGI middleware that stamps ``slack_last_event_at`` when a Slack route answers 200.

    Pure ASGI rather than ``BaseHTTPMiddleware`` so it never holds the response back
    from Slack's 3-second deadline or from the routes' background tasks.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] not in SLACK_PATHS:
            await self.app(scope, receive, send)
            return

        async def send_and_stamp(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] == 200:
                _slack_clock["last_event_at"] = datetime.now(UTC)
            await send(message)

        await self.app(scope, receive, send_and_stamp)


def install(app: FastAPI) -> None:
    app.include_router(router)
    app.add_middleware(SlackEventClock)
