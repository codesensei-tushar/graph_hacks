"""Which GitHub App this deployment runs as, and its credentials.

Two places can hold them, and the environment wins:

1. **Env** (as before): ``GITHUB_APP_ID``, ``GITHUB_WEBHOOK_SECRET``, ``GITHUB_PRIVATE_KEY`` or
   ``GITHUB_PRIVATE_KEY_PATH``, ``GITHUB_CLIENT_ID``/``GITHUB_CLIENT_SECRET``, and optionally
   ``GITHUB_APP_SLUG``. When the first three are set, nothing stored is read.
2. **Stored**: registered from the operator page with GitHub's App Manifest flow
   (``src/domain/github/app_registration.py``), kept in the control graph:

       (:GitHubApp {id: 'default', app_id, slug, name, client_id, html_url, owner_login, owner_id,
                    secret_enc, key_id, created_at})

   ``secret_enc`` is MultiFernet (``token_crypto``, ``allow_plaintext=False``) over JSON
   ``{kind: 'github_app', app_id, pem, webhook_secret, client_secret}``; ``app_id`` is checked on
   decrypt, so a value copied onto another node is refused. Nothing secret is ever returned by
   ``describe`` or logged.

``current()`` is read on every webhook and API call, so the stored app is cached in-process for
``CACHE_SECONDS``; ``forget()`` drops the cache after a registration.
"""

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from src.core.config import settings

log = logging.getLogger(__name__)

APP_NODE_ID = "default"
CACHE_SECONDS = 60.0
_SECRET_KIND = "github_app"


@dataclass(frozen=True)
class AppCredentials:
    app_id: str
    source: str  # 'env' | 'stored'
    slug: str = ""
    name: str = ""
    client_id: str = ""
    html_url: str = ""
    owner_login: str = ""
    owner_id: str = ""
    private_key: str = field(default="", repr=False)
    webhook_secret: str = field(default="", repr=False)
    client_secret: str = field(default="", repr=False)

    @property
    def can_link(self) -> bool:
        """Whether user sign-in (``link github``, the install check) has what it needs."""
        return bool(self.client_id and self.client_secret)

    def describe(self) -> dict:
        """Everything that is not secret."""
        return {
            "source": self.source, "app_id": self.app_id, "slug": self.slug or None, "name": self.name or None,
            "client_id": self.client_id or None, "html_url": self.html_url or None, "owner": self.owner_login or None,
            "install_url": install_url(self) or None, "user_sign_in": self.can_link,
        }


def install_url(creds: "AppCredentials | None") -> str:
    return f"https://github.com/apps/{creds.slug}/installations/new" if creds and creds.slug else ""


def _env_key() -> str:
    if settings.github_private_key:
        return settings.github_private_key.replace("\\n", "\n")
    if settings.github_private_key_path:
        try:
            return Path(settings.github_private_key_path).expanduser().read_text()
        except OSError:
            log.error("[github] GITHUB_PRIVATE_KEY_PATH cannot be read")
    return ""


def env_configured() -> bool:
    return bool(settings.github_enabled)


def _from_env() -> AppCredentials:
    return AppCredentials(
        app_id=str(settings.github_app_id), source="env", slug=settings.github_app_slug, client_id=settings.github_client_id,
        private_key=_env_key(), webhook_secret=settings.github_webhook_secret, client_secret=settings.github_client_secret,
    )


_cache: tuple[float, AppCredentials | None] | None = None
_lock = threading.Lock()


def forget() -> None:
    global _cache
    with _lock:
        _cache = None


def current() -> AppCredentials | None:
    """The app's credentials: env when set, else the stored app, else None."""
    if env_configured():
        return _from_env()
    global _cache
    now = time.monotonic()
    with _lock:
        if _cache is not None and now - _cache[0] < CACHE_SECONDS:
            return _cache[1]
    try:
        found = load_stored()
    except Exception as error:
        # Unreadable (no key, a wrong key, the control graph down): no app, and say so once a minute.
        log.error("[github] the stored GitHub App could not be read: %s", type(error).__name__)
        found = None
    with _lock:
        _cache = (now, found)
    return found


def enabled() -> bool:
    return current() is not None


def link_enabled() -> bool:
    creds = current()
    return bool(creds and creds.can_link)


# --- the stored app (control graph) ------------------------------------------------------------

_FIELDS = ("app_id", "slug", "name", "client_id", "html_url", "owner_login", "owner_id", "secret_enc")


def _control():
    from src.core.tenancy import control_graph

    return control_graph()


def stored_exists() -> bool:
    rows = _control().query("MATCH (a:GitHubApp {id: $id}) RETURN count(a)", {"id": APP_NODE_ID}).result_set
    return bool(rows and rows[0][0])


def load_stored() -> AppCredentials | None:
    from src.domain.slack.token_crypto import decrypt_token

    rows = _control().query(
        f"MATCH (a:GitHubApp {{id: $id}}) RETURN {', '.join('a.' + name for name in _FIELDS)}", {"id": APP_NODE_ID}
    ).result_set
    if not rows:
        return None
    row = dict(zip(_FIELDS, rows[0], strict=True))
    secret = json.loads(decrypt_token(row["secret_enc"] or "", allow_plaintext=False))
    if secret.get("kind") != _SECRET_KIND or str(secret.get("app_id")) != str(row["app_id"]):
        raise ValueError("the stored GitHub App secret belongs to another app")
    return AppCredentials(
        app_id=str(row["app_id"]), source="stored", slug=row["slug"] or "", name=row["name"] or "", client_id=row["client_id"] or "",
        html_url=row["html_url"] or "", owner_login=row["owner_login"] or "", owner_id=str(row["owner_id"] or ""),
        private_key=secret.get("pem") or "", webhook_secret=secret.get("webhook_secret") or "", client_secret=secret.get("client_secret") or "",
    )


class AlreadyRegistered(RuntimeError):
    """A stored app exists; a second registration would strand its installations."""


def store(creds: AppCredentials, created_by: str = "operator") -> None:
    """Keep a newly registered app. Encrypted or refused; never replaces a stored app."""
    from src.domain.slack.token_crypto import current_key_id, encrypt_token

    secret = encrypt_token(
        json.dumps({"kind": _SECRET_KIND, "app_id": creds.app_id, "pem": creds.private_key, "webhook_secret": creds.webhook_secret, "client_secret": creds.client_secret}, sort_keys=True),
        allow_plaintext=False,
    )
    rows = _control().query(
        """
        MERGE (a:GitHubApp {id: $id})
        ON CREATE SET a.app_id = $app_id, a.slug = $slug, a.name = $name, a.client_id = $client_id, a.html_url = $html_url,
            a.owner_login = $owner_login, a.owner_id = $owner_id, a.secret_enc = $secret, a.key_id = $key_id,
            a.created_by = $created_by, a.created_at = $now
        RETURN a.app_id = $app_id AND a.created_at = $now
        """,
        {
            "id": APP_NODE_ID, "app_id": creds.app_id, "slug": creds.slug, "name": creds.name, "client_id": creds.client_id,
            "html_url": creds.html_url, "owner_login": creds.owner_login, "owner_id": creds.owner_id, "secret": secret,
            "key_id": current_key_id(), "created_by": created_by, "now": int(time.time() * 1000),
        },
    ).result_set
    forget()
    if not rows or not rows[0][0]:
        raise AlreadyRegistered("a GitHub App is already registered")


def rewrap() -> bool:
    """Re-encrypt the stored app's secret under the first key (key rotation). Whether it changed."""
    from src.domain.slack.token_crypto import current_key_id, rewrap_token

    rows = _control().query("MATCH (a:GitHubApp {id: $id}) RETURN a.secret_enc", {"id": APP_NODE_ID}).result_set
    if not rows or not rows[0][0]:
        return False
    new = rewrap_token(rows[0][0], allow_plaintext=False)
    if not new:
        return False
    _control().query(
        "MATCH (a:GitHubApp {id: $id}) WHERE a.secret_enc = $old SET a.secret_enc = $new, a.key_id = $key_id",
        {"id": APP_NODE_ID, "old": rows[0][0], "new": new, "key_id": current_key_id()},
    )
    forget()
    return True
