"""Deploy plumbing, not bot behavior: ``/healthz``, a seed refresh and a team import for
production, and a clock for the last verified Slack request. ``/mcp`` is served by
``src/api/mcp.py``.

``deploy/deploy.sh`` polls ``/healthz`` until ``git_sha`` matches the commit it shipped.
``slack_last_event_at`` moves when ``/slack/events`` or ``/slack/interactions`` answers
200, which those routes only do after the signature checks out (unsigned requests get
401). The clock lives in this process, so it resets on restart.

``GET /ops/metrics`` (admin token) returns the graph query metrics of
``src/core/query_metrics.py``: per tool and per org, counts, p50/p95, timeouts and queue
refusals. ``install`` registers its query hook. Also per process, also reset on restart.
"""

import dataclasses
import logging
import os
from datetime import UTC, datetime

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.api.v1.slack_install import _admin_denial
from src.core import query_metrics
from src.core.config import settings
from src.db.falkordb_client import connect
from src.domain.onboarding.team_import import MAX_FILE_BYTES, TeamFileError, check_team, contents, describe, import_team
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


@router.get("/ops/metrics")
def metrics(request: Request):
    """Graph query metrics since this process started. Names tools and org ids, never query text."""
    if denied := _admin_denial(request):
        return denied
    return query_metrics.snapshot()


@router.post("/ops/seed")
def seed(request: Request):
    """Refresh the configured company's seed in place. Merges by id and never deletes, so
    memories, workspaces and GitHub history survive; the counts say what changed."""
    if denied := _admin_denial(request):
        return denied
    report = seed_company(company_module(settings.seed_company))
    log.info("[ops] seed merged company=%s created nodes=%d edges=%d set=%d", report.company, report.nodes_created, report.edges_created, report.properties_set)
    return {"company": settings.seed_company, "mode": "merge", **dataclasses.asdict(report)}


@router.post("/ops/import")
async def import_team_file(request: Request, check: bool = False):
    """Merge one team file (TOML, see docs/ONBOARDING-TEAMS.md) into the configured company.

    The body is the file. The whole file is checked, against the graph too, before anything
    is written: a refused file writes nothing and answers 400 with one message per problem.
    ``?check=true`` runs the same checks and writes nothing.
    """
    if denied := _admin_denial(request):
        return denied
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FILE_BYTES:
            return JSONResponse({"error": "too_large", "limit_bytes": MAX_FILE_BYTES}, status_code=413)
    company_id = company_module(settings.seed_company).COMPANY["id"]
    try:
        if check:
            team = await run_in_threadpool(check_team, bytes(body), company_id)
            return {"company": settings.seed_company, "mode": "check", "team": team.TEAM_ID, "contents": describe(team), "counts": contents(team)}
        result = await run_in_threadpool(import_team, bytes(body), company_id)
    except TeamFileError as error:
        return JSONResponse({"error": "refused", "problems": error.problems}, status_code=400)
    report = result.report
    log.info("[ops] team imported team=%s created nodes=%d edges=%d set=%d", result.team.TEAM_ID, report.nodes_created, report.edges_created, report.properties_set)
    return {"company": settings.seed_company, "mode": "merge", **result.to_dict()}


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
    query_metrics.install()
    app.add_middleware(SlackEventClock)
