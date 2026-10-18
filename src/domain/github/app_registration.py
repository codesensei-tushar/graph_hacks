"""Register this deployment's GitHub App from a browser, with GitHub's App Manifest flow.

GitHub's docs ("Registering a GitHub App from a manifest",
https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest, and
"Create a GitHub App from a manifest", https://docs.github.com/en/rest/apps/apps#create-a-github-app-from-a-manifest):

1. The browser POSTs a form with one field, ``manifest`` (the JSON below, as a string), to
   ``https://github.com/settings/apps/new`` (a personal account) or
   ``https://github.com/organizations/<org>/settings/apps/new`` (an organization), with
   ``?state=<unguessable>`` on the URL against CSRF.
2. After "Create GitHub App", GitHub redirects to the manifest's ``redirect_url`` with
   ``?code=<temporary code>&state=<the same state>``.
3. ``POST /app-manifests/{code}/conversions`` (no authentication; the code is the credential, and
   all three steps must finish within an hour) answers 201 with the app: ``id``, ``slug``,
   ``client_id``, ``client_secret``, ``webhook_secret``, ``pem``, ``html_url``, ``owner``.

Here: ``start`` mints the state (kv ``github:manifest:<sha256(state)>``, an hour, single use) and
returns where to post and what; ``complete`` takes the state once, converts the code and stores the
app encrypted in the control graph (``app_config.store``, MultiFernet, ``allow_plaintext=False``).
Only an operator (``BRAIN_ADMIN_TOKEN``) can start it; the callback is trusted only through its state.

The app asks for read-only permissions and is public, so any company can install it on its own
GitHub account from the console ("Connect GitHub"). ``installation`` and
``installation_repositories`` events reach every GitHub App without subscribing (and cannot be
subscribed to); ``default_events`` lists the rest.
"""

import hashlib
import json
import logging
import re
import secrets
import time

from src.core.config import settings
from src.db.falkordb_client import kv
from src.domain.slack.token_crypto import encryption_available
from src.integrations.github import app_config, client

log = logging.getLogger(__name__)

STATE_TTL_SECONDS = 60 * 60  # GitHub's own limit for the whole flow
DEFAULT_NAME = "Company Brain"
# GitHub account and organization logins: letters, digits and single hyphens, at most 39 characters.
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
# GitHub App names: at most 34 characters.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,33}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

PERMISSIONS = {
    "metadata": "read",       # required; collaborators decide who reads a private repo
    "contents": "read",       # pull request details in the backfill; no file contents are stored
    "pull_requests": "read",  # pull requests, reviews, changed files
    "issues": "read",         # issues, written as tickets
    "members": "read",        # the member event (organization accounts)
}
EVENTS = ["pull_request", "pull_request_review", "issues", "repository", "member"]


class RegistrationError(ValueError):
    """A fixed reason: ``bad_owner``, ``bad_name``, ``no_public_url``, ``already_registered``,
    ``env_configured``, ``no_encryption_key``, ``state``, ``conversion_failed``."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def urls() -> dict[str, str]:
    base = settings.public_url
    return {
        "homepage": base,
        "webhook": f"{base}/github/webhook",
        "redirect": f"{base}/ops/github/app/created",
        "setup": f"{base}/console/oauth/github/setup",
        "verify": f"{base}/console/oauth/github/verify",
        "link": f"{base}/github/oauth/callback",
    }


def manifest(name: str = DEFAULT_NAME) -> dict:
    found = urls()
    return {
        "name": name,
        "url": found["homepage"],
        "description": "Company Brain reads pull requests, reviews and issues, read-only, into your company's graph.",
        "hook_attributes": {"url": found["webhook"], "active": True},
        "redirect_url": found["redirect"],
        # The install check (console Connect) and `link github` in Slack sign the user in here.
        "callback_urls": [found["verify"], found["link"]],
        "setup_url": found["setup"],
        # A changed repo selection, or a Connect on an account that already has the app, comes back too.
        "setup_on_update": True,
        "request_oauth_on_install": False,
        "public": True,
        "default_permissions": dict(PERMISSIONS),
        "default_events": list(EVENTS),
    }


def _state_key(state: str) -> str:
    return f"github:manifest:{hashlib.sha256(state.encode()).hexdigest()}"


def start(owner_org: str | None = None, name: str | None = None) -> dict:
    """``{post_url, manifest}``: the form the operator's browser posts to GitHub."""
    if app_config.env_configured():
        raise RegistrationError("env_configured")
    if app_config.stored_exists():
        raise RegistrationError("already_registered")
    if not settings.public_url:
        raise RegistrationError("no_public_url")
    if not encryption_available():
        raise RegistrationError("no_encryption_key")
    owner_org = (owner_org or "").strip() or None
    if owner_org is not None and not _LOGIN.match(owner_org):
        raise RegistrationError("bad_owner")
    name = " ".join((name or "").split()) or DEFAULT_NAME
    if not _NAME.match(name):
        raise RegistrationError("bad_name")
    state = secrets.token_urlsafe(32)
    kv().set(_state_key(state), json.dumps({"owner_org": owner_org, "at": int(time.time())}), ex=STATE_TTL_SECONDS)
    where = f"https://github.com/organizations/{owner_org}/settings/apps/new" if owner_org else "https://github.com/settings/apps/new"
    return {"post_url": f"{where}?state={state}", "manifest": json.dumps(manifest(name))}


def _take_state(state: str) -> dict | None:
    pipe = kv().pipeline()
    pipe.get(_state_key(state))
    pipe.delete(_state_key(state))
    raw, _ = pipe.execute()
    try:
        record = json.loads(raw) if raw else None
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def complete(code: str, state: str) -> app_config.AppCredentials:
    """Take the state once, convert the code, store the app. Raises ``RegistrationError``."""
    if not state or _take_state(state) is None:
        raise RegistrationError("state")
    if not code:
        raise RegistrationError("conversion_failed")
    if app_config.env_configured():
        raise RegistrationError("env_configured")
    if app_config.stored_exists():
        raise RegistrationError("already_registered")
    try:
        data = client.convert_manifest(code)
    except Exception as error:
        log.warning("[github] manifest conversion failed: %s", type(error).__name__)
        raise RegistrationError("conversion_failed") from None
    owner = data.get("owner") or {}
    slug = str(data.get("slug") or "")
    creds = app_config.AppCredentials(
        app_id=str(data.get("id") or ""), source="stored", slug=slug if _SLUG.match(slug) else "",
        name=str(data.get("name") or "")[:80], client_id=str(data.get("client_id") or ""), html_url=str(data.get("html_url") or ""),
        owner_login=str(owner.get("login") or owner.get("slug") or ""), owner_id=str(owner.get("id") or ""),
        private_key=str(data.get("pem") or ""), webhook_secret=str(data.get("webhook_secret") or ""), client_secret=str(data.get("client_secret") or ""),
    )
    if not (creds.app_id.isdigit() and creds.private_key and creds.webhook_secret and creds.slug):
        log.warning("[github] manifest conversion answered without an id, key, webhook secret or slug")
        raise RegistrationError("conversion_failed")
    try:
        app_config.store(creds)
    except app_config.AlreadyRegistered:
        raise RegistrationError("already_registered") from None
    except RuntimeError:
        # token_crypto refuses to store a credential unencrypted.
        raise RegistrationError("no_encryption_key") from None
    log.info("[github] registered app id=%s slug=%s owner=%s", creds.app_id, creds.slug, creds.owner_login)
    return creds
