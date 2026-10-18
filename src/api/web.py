"""The judge-facing page at ``/``, the team pages at ``/team/<id>``, the graph explorer at ``/explore``, and their files under ``/static``.

``install`` adds the ask routes, the page route and the static mount in one call, so
``main.py`` needs one line. The page route is a plain route and the mount sits under its
own prefix, so neither shadows ``/healthz``, ``/api/*``, ``/slack/*``, ``/github/*`` or
``/mcp``. An unknown path answers FastAPI's JSON 404, except to a browser asking for a page,
which gets ``404.html`` with links back. ``/team/<id>`` for a team the
graph does not have is the same page with a 404 status, so the page still says so and a
crawler or a link checker sees the miss.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.api import console_web
from src.api.v1 import ask, explorer, graph, teams
from src.domain.knowledge import team_page

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
INDEX = STATIC_DIR / "index.html"
TEAM = STATIC_DIR / "team.html"
EXPLORER = STATIC_DIR / "explorer.html"
NOT_FOUND = STATIC_DIR / "404.html"
# Paths machines call: their 404 stays JSON whatever the client accepts.
MACHINE_PREFIXES = ("/api/", "/mcp", "/static/", "/slack/", "/github/", "/ops/", "/healthz")

router = APIRouter(tags=["web"], include_in_schema=False)


@router.get("/")
def index():
    return console_web.public_page(INDEX)


@router.get("/team/{team_id}")
def team(team_id: str):
    # The page fetches /api/team/<id> itself and says so when the team is not in the graph;
    # the status says so too. The lookup is the API's own, from the same one-minute cache.
    # A lookup that fails serves the page as a 200: it fetches the team itself and shows the error.
    page = console_web.public_page(TEAM)
    try:
        known = len(team_id) <= team_page.MAX_TEAM_ID and team_page.public_team_page(team_id) is not None
    except Exception:
        log.exception("[web] team lookup failed for the page status team=%s", team_id[: team_page.MAX_TEAM_ID])
        known = True
    if not known:
        page.status_code = 404
    return page


@router.get("/explore")
def explore():
    # The page reads ?node=<id> itself and fetches /api/explore; an unknown node gets the nearest names there.
    return console_web.public_page(EXPLORER)


async def not_found(request: Request, exc: StarletteHTTPException):
    """A browser that asks for a page that is not here gets one with links back; everyone else, the JSON 404."""
    wants_page = request.method in ("GET", "HEAD") and "text/html" in request.headers.get("accept", "")
    if exc.status_code == 404 and wants_page and not request.url.path.startswith(MACHINE_PREFIXES):
        page = console_web.render(NOT_FOUND.read_text(), console_web.console_enabled())
        return HTMLResponse(page, status_code=404, headers={"Cache-Control": "no-cache"})
    return await http_exception_handler(request, exc)


def install(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, not_found)
    app.include_router(ask.router)
    app.include_router(teams.router)
    app.include_router(graph.router)
    app.include_router(explorer.router)
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
