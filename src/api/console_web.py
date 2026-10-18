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
import re
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response

from src.core.config import settings

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
CONSOLE_DIR = STATIC_DIR / "console"
PRIVACY = STATIC_DIR / "privacy.html"
# Where privacy questions and deletion requests go: the one place to change it. /privacy fills
# every {{contact}} in privacy.html with it.
PRIVACY_CONTACT = "privacy@refactron.dev"

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
    "/console/teams": "teams.html",
    "/console/people": "people.html",
}

# Whether the console runs, for the page's scripts; render() flips it on a page served while it does.
CONSOLE_OFF = '<meta name="brain-console" content="off">'
CONSOLE_ON = '<meta name="brain-console" content="on">'

# The one header. Every page's <header class="top" data-shell="site|console" data-here="<page>">
# holds the brand, a NAV_MARK and an ACTIONS_MARK; render() fills them from NAV, so every page
# gets the same links as plain HTML (no-JS visitors and crawlers included), the current page
# marked, "Sign in" when the console runs, and the console's company name and Sign out slots,
# which console.js fills. Everything rendered is a constant from this file.
NAV = {
    "site": (
        ("ask", "Ask", "/#ask"),
        ("graph", "Graph", "/#graph"),
        ("explore", "Explore", "/explore"),
        ("proof", "Proof", "/#proof"),
        ("teams", "Teams", "/#teams"),
        ("how", "How it works", "/#how"),
        ("builders", "Builders", "/#builders"),
    ),
    "console": (
        ("connections", "Connections", "/console"),
        ("teams", "Teams", "/console/teams"),
        ("people", "People", "/console/people"),
        ("ask", "Ask", "/console/ask"),
    ),
}
HERE_UNDER = {"team": "teams"}
NAV_MARK = "<!-- shell:nav -->"
ACTIONS_MARK = "<!-- shell:actions -->"
_HEADER = re.compile(r'<header class="top" data-shell="(site|console)" data-here="([a-z]+)">')


def render(html: str, console_on: bool) -> str:
    """A page with its header filled in, and the console flag its scripts read."""
    match = _HEADER.search(html)
    if not match:
        return html
    shell, here = match.group(1), match.group(2)
    current = HERE_UNDER.get(here, here)
    label = "Console" if shell == "console" else "Sections"
    links = ""
    for key, text, href in NAV[shell]:
        mark = ' aria-current="page"' if key == current else ""
        links += f'<a href="{href}" data-nav="{key}"{mark}>{text}</a>'
    if shell == "console":
        actions = '<span class="top-org" id="top-org" hidden></span><button class="top-signin" id="sign-out" type="button" hidden>Sign out</button>'
    elif console_on and here != "signin":
        actions = '<a class="top-signin" href="/signin">Sign in</a>'
    else:
        actions = ""
    html = html.replace(NAV_MARK, f'<nav aria-label="{label}">{links}</nav>', 1).replace(ACTIONS_MARK, actions, 1)
    return html.replace(CONSOLE_OFF, CONSOLE_ON, 1) if console_on else html


router = APIRouter(tags=["console-web"], include_in_schema=False)


def console_enabled() -> bool:
    return settings.console_enabled


def public_page(path: Path) -> Response:
    """A public page (``/``, ``/team/<id>``, ``/explore``), with "Sign in" shown when the console runs."""
    return HTMLResponse(render(path.read_text(), console_enabled()), headers={"Cache-Control": "no-cache"})


def _serve(name: str) -> Response:
    if not console_enabled():
        raise HTTPException(status_code=404)
    return HTMLResponse(render((CONSOLE_DIR / name).read_text(), True), headers=HEADERS)


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
    return HTMLResponse(render(PRIVACY.read_text().replace("{{contact}}", PRIVACY_CONTACT), console_enabled()), headers=HEADERS)


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
