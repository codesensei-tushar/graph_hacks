"""The org console's pages (``/signin``, ``/console/...``) and the ``/privacy`` page.

Every console page is a static file under ``src/static/console/`` that reads the console JSON API
(``/console/api/*``) itself, so this module only serves files. With ``CONSOLE_ENABLED`` off (the
default) a console page answers FastAPI's plain 404, as any unknown path does, and the public
header's "Sign in" link stays hidden: production is unchanged until the console is turned on.

``/privacy`` is served either way: the Jira and HubSpot apps published as Refactron link to it.

The pages are sent with the console's CSP (no inline script or style, same-origin calls only),
the same policy nginx sets on ``/console`` and ``/auth``, so a local run breaks where production would.


``CONSOLE_FIXTURES=1`` (dev and screenshots only) also mounts ``console_fake``, the console API
answered from ``tests/fixtures/console/``, so the pages can be built before the real API exists.
"""

import os
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response

from src.core.config import settings

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
CONSOLE_DIR = STATIC_DIR / "console"
PRIVACY = STATIC_DIR / "privacy.html"

CSP = "default-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self' https://slack.com"
HEADERS = {
    "Cache-Control": "no-cache",
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}

# Each console route and the file it serves. A route with a path parameter serves the same file
# for every id; the page reads the id from its own address.
PAGES = {
    "/signin": "signin.html",
    "/console": "index.html",
    "/console/start": "start.html",
    "/console/connect/jira/site": "site.html",
    "/console/connectors/{connection_id}/projects": "projects.html",
    "/console/connectors/{connection_id}": "connection.html",
    "/console/ask": "ask.html",
}

# The public header's link, hidden in the files; shown when the console runs.
SIGNIN_HIDDEN = " hidden data-console-link>"
SIGNIN_SHOWN = " data-console-link>"

router = APIRouter(tags=["console-web"], include_in_schema=False)


def console_enabled() -> bool:
    return settings.console_enabled


def public_page(path: Path) -> Response:
    """A public page (``/``, ``/team/<id>``, ``/explore``), with "Sign in" shown when the console runs."""
    if not console_enabled():
        return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-cache"})
    html = path.read_text().replace(SIGNIN_HIDDEN, SIGNIN_SHOWN, 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


def _serve(name: str) -> Response:
    if not console_enabled():
        raise HTTPException(status_code=404)
    return FileResponse(CONSOLE_DIR / name, media_type="text/html", headers=HEADERS)


def _page(route: str, name: str):
    if "{connection_id}" in route:

        def serve_one(connection_id: str):
            return _serve(name)

        return serve_one

    def serve():
        return _serve(name)

    return serve


for _route, _name in PAGES.items():
    router.add_api_route(_route, _page(_route, _name), methods=["GET"], name=f"console:{_name}")


@router.get("/privacy")
def privacy():
    return FileResponse(PRIVACY, media_type="text/html", headers=HEADERS)


def fixtures_enabled() -> bool:
    """Dev only: the fake console API (console_fake) answers /console/api from JSON fixtures."""
    return console_enabled() and os.getenv("CONSOLE_FIXTURES", "").lower() in ("1", "true", "yes")


def install(app: FastAPI) -> None:
    if fixtures_enabled():
        from src.api import console_fake

        # First in line, ahead of the real /auth and /console/api routes main.py mounted earlier.
        before = len(app.router.routes)
        console_fake.install(app)
        fake = app.router.routes[before:]
        del app.router.routes[before:]
        app.router.routes[0:0] = fake
    app.include_router(router)
