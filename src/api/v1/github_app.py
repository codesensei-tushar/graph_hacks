"""The operator's GitHub App page and routes: register the app without a terminal.

    GET  /ops/github/app            the page (static; it asks for BRAIN_ADMIN_TOKEN and keeps it in memory only)
    GET  /ops/github/app/status     {configured, app: {source, app_id, slug, owner, install_url, ...}} (admin token)
    POST /ops/github/app/manifest   {owner_org?, name?} -> {post_url, manifest} (admin token)
    GET  /ops/github/app/created    GitHub's redirect after "Create GitHub App": ?code&state

The page posts ``manifest`` to ``post_url`` (github.com) as a plain form, so it is served with its
own CSP whose ``form-action`` names https://github.com; it sits under ``/ops`` so nginx's console
CSP (``form-action 'self' https://slack.com``) does not apply to it. ``created`` is trusted only
through its single-use state; it renders a fixed page, and never echoes GitHub's text or a secret.
"""

import html
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from src.api.v1.slack_install import _admin_denial
from src.domain.github import app_registration
from src.integrations.github import app_config

log = logging.getLogger(__name__)
router = APIRouter(tags=["github-app"], include_in_schema=False)

PAGE = Path(__file__).resolve().parents[2] / "static" / "ops" / "github-app.html"
CSP = "default-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action https://github.com"
HEADERS = {"Cache-Control": "no-store", "Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}

# What a refusal says, by RegistrationError.code. Fixed text only.
MESSAGES = {
    "bad_owner": "That is not a GitHub organization name.",
    "bad_name": "An app name is at most 34 letters, digits, spaces, dots, dashes or underscores.",
    "no_public_url": "Set PUBLIC_URL first: GitHub sends webhooks and people back to it.",
    "already_registered": "A GitHub App is already registered for this deployment.",
    "env_configured": "This deployment's GitHub App is set by its environment (GITHUB_APP_ID and friends), which wins over a registered one.",
    "no_encryption_key": "Set TOKEN_ENCRYPTION_KEYS first: the app's private key and secrets are stored encrypted, never in plain text.",
    "state": "That registration link expired or was already used. Start again from the operator page.",
    "conversion_failed": "GitHub did not hand over the new app's credentials. Start again from the operator page; GitHub's code lasts an hour.",
}


@router.get("/ops/github/app")
def page():
    return HTMLResponse(PAGE.read_text(), headers=HEADERS)


def _status() -> dict:
    creds = app_config.current()
    return {"configured": creds is not None, "app": creds.describe() if creds else None, "manifest": app_registration.manifest()}


@router.get("/ops/github/app/status")
async def status(request: Request):
    if denied := _admin_denial(request):
        return denied
    return await run_in_threadpool(_status)


@router.post("/ops/github/app/manifest")
async def start(request: Request):
    if denied := _admin_denial(request):
        return denied
    try:
        body = await request.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    owner, name = body.get("owner_org"), body.get("name")
    if not isinstance(owner, (str, type(None))) or not isinstance(name, (str, type(None))):
        return JSONResponse({"error": "bad_owner", "message": MESSAGES["bad_owner"]}, status_code=400)
    try:
        return await run_in_threadpool(app_registration.start, owner, name)
    except app_registration.RegistrationError as error:
        code = 409 if error.code in ("already_registered", "env_configured", "no_public_url", "no_encryption_key") else 400
        return JSONResponse({"error": error.code, "message": MESSAGES[error.code]}, status_code=code)


def _result(title: str, lines: list[str], status_code: int = 200) -> HTMLResponse:
    body = "".join(f"<p>{line}</p>" for line in lines)
    return HTMLResponse(
        "<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<meta name='robots' content='noindex'><title>{html.escape(title)} · Company Brain</title>"
        "<link rel='stylesheet' href='/static/style.css?v=15'><link rel='stylesheet' href='/static/console.css?v=1'>"
        f"<body class='console'><main class='console-main'><div class='wrap'><section class='page-head'><p class='eyebrow'>Operator</p>"
        f"<h1>{html.escape(title)}</h1>{body}<p><a class='btn btn-ghost' href='/ops/github/app'>Back to the GitHub App page</a></p></section></div></main></body></html>",
        status_code=status_code, headers=HEADERS,
    )


@router.get("/ops/github/app/created")
async def created(code: str = "", state: str = ""):
    try:
        creds = await run_in_threadpool(app_registration.complete, code, state)
    except app_registration.RegistrationError as error:
        return _result("The app was not registered", [html.escape(MESSAGES[error.code])], 400)
    install = app_config.install_url(creds)
    lines = [f"GitHub App <b>{html.escape(creds.name or creds.slug)}</b> is registered, and its credentials are stored encrypted."]
    if install:
        lines.append(
            f"Companies connect it from the console (Connections → Connect GitHub). To install it on an account yourself: "
            f"<a href='{html.escape(install)}'>{html.escape(install)}</a>"
        )
    return _result("GitHub App registered", lines)
