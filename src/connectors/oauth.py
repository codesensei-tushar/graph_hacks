"""Vendor OAuth for connector installs: the code exchange, and a bearer token that is always fresh.

An OAuth install (``ConnectorInstall {auth: 'oauth'}``) keeps its tokens in ``secret_enc``, the
JSON ``{org, install, access_token, refresh_token, expires_at, ...}`` that package B's store
encrypts with MultiFernet. ``bearer_for(install)`` hands a connector the access token:

1. A token cached in memory with more than ``REFRESH_SKEW_SECONDS`` left is used as is.
2. Otherwise the stored secret is read again; another worker may have refreshed it already.
3. Otherwise, under the kv lock ``connector:refresh:<org>:<key>`` (``SET NX``, ``LOCK_TTL_SECONDS``),
   the secret is read a third time, the refresh token is spent at the vendor's token endpoint,
   and the new secret, with the rotated refresh token, is written to the store **before** the
   new access token is used. Atlassian rotates refresh tokens: one that was spent and not saved
   is lost after Atlassian's reuse interval (10 minutes), and the install must be reconnected.
   A failed write therefore fails the step, and the next step (within those minutes) spends the
   old refresh token again.

A refresh the vendor refuses (``invalid_grant``) raises ``CredentialsRejected``, which the sync
records as the install's last error and backs off for an hour; the console then offers
"Reconnect". No token, code or client secret ever reaches a log line, an exception message,
the graph outside ``secret_enc``, or a route's answer; vendor error text is reduced to its
OAuth error code.

Endpoints, verified against the vendors' docs (see docs/CONSOLE.md for the links):

- Atlassian 3LO: authorize ``https://auth.atlassian.com/authorize`` with ``audience=api.atlassian.com``
  and ``prompt=consent``; token ``POST https://auth.atlassian.com/oauth/token`` with a JSON body;
  access tokens last an hour; refresh tokens rotate.
- HubSpot (developer platform): authorize ``https://app.hubspot.com/oauth/authorize``; token
  ``POST https://api.hubapi.com/oauth/2026-09/token`` form-encoded; access tokens last 30 minutes;
  token metadata (``hub_id``) from ``POST /oauth/2026-09/token/introspect``.
"""

import json
import logging
import os
import re
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from src.connectors import http
from src.connectors.base import ConnectorError, CredentialsRejected, Install, OAuthSpec, RetryLater
from src.core.config import settings
from src.db.falkordb_client import kv

log = logging.getLogger(__name__)

# The fields an OAuth install keeps in ``secret_enc`` (``Connector.secret_fields``). ``expires_at`` is
# not secret, but it is written with the tokens it describes.
OAUTH_SECRET_FIELDS = ("access_token", "refresh_token", "expires_at")
# Refresh when less than this is left, so a token never expires in the middle of a step.
REFRESH_SKEW_SECONDS = 5 * 60
# The refresh lock. A holder that dies frees it after this; a refresh takes one HTTP call.
LOCK_TTL_SECONDS = 30
# How long a worker waits for another one's refresh before it gives the step back.
LOCK_WAIT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.25
TOKEN_TIMEOUT_SECONDS = 20
# Used when a token response names no lifetime: the shorter of the two vendors'.
DEFAULT_EXPIRES_IN = 30 * 60
# An OAuth error code as RFC 6749 spells them. Anything else from the vendor is dropped.
_ERROR_CODE = re.compile(r"^[a-z_]{1,64}$")
# Release the lock only if it is still ours (it may have expired and been taken by another worker).
_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


def client_id(spec: OAuthSpec) -> str:
    """The app's client id, from the variable the spec names (``JIRA_OAUTH_CLIENT_ID``), never ``CONNECTOR_*``."""
    return _setting(spec.client_id_setting)


def client_secret(spec: OAuthSpec) -> str:
    return _setting(spec.client_secret_setting)


def _setting(name: str) -> str:
    return (os.environ.get(name) or str(getattr(settings, name.lower(), "") or "")).strip()


def configured(spec: OAuthSpec | None) -> bool:
    return bool(spec and client_id(spec) and client_secret(spec))


def callback_url(source: str) -> str:
    """``<PUBLIC_URL>/console/oauth/<source>/callback``: what each vendor app has registered."""
    return f"{settings.public_url}/console/oauth/{source}/callback"


def authorize_url(spec: OAuthSpec, redirect_uri: str, state: str) -> str:
    query = {
        **spec.extra_params,
        "client_id": client_id(spec),
        "scope": " ".join(spec.scopes),
        "redirect_uri": redirect_uri,
        "state": state,
        "response_type": "code",
    }
    return f"{spec.authorize_url}?{urllib.parse.urlencode(query, quote_via=urllib.parse.quote)}"


class OAuthError(ConnectorError):
    """The token endpoint refused or failed. ``code`` is the OAuth error code, never vendor prose."""

    def __init__(self, code: str, status: int | None = None):
        super().__init__(f"token endpoint answered {status or 'nothing'} ({code})")
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Tokens:
    """What a token endpoint gave back. ``extra`` keeps the other fields (HubSpot's ``hub_id``, scopes)."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float
    scope: str = ""
    extra: dict = field(default_factory=dict, repr=False, compare=False, hash=False)

    def as_secret(self) -> dict:
        return {"access_token": self.access_token, "refresh_token": self.refresh_token, "expires_at": self.expires_at}


# --- the token endpoint ------------------------------------------------------------------------


def _post(url: str, fields: dict[str, str], body: str, headers: dict[str, str] | None = None) -> dict:
    """POST to a token endpoint and return its JSON. Redirects are refused (``http._urlopen``), and
    a failure carries only the status and the OAuth error code."""
    if body == "json":
        data, content_type = json.dumps(fields).encode(), "application/json"
    else:
        data, content_type = urllib.parse.urlencode(fields).encode(), "application/x-www-form-urlencoded"
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": content_type, "Accept": "application/json", "User-Agent": "company-brain", **(headers or {})},
    )
    try:
        with http._urlopen(request, timeout=TOKEN_TIMEOUT_SECONDS) as response:
            parsed = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        code = _error_code(error)
        if error.code == 429 or error.code >= 500:
            wait = http.retry_after_seconds(error.headers.get("Retry-After") if error.headers else None)
            raise RetryLater(max(wait or 30.0, 1.0), f"token endpoint answered {error.code}") from None
        raise OAuthError(code, error.code) from None
    except (urllib.error.URLError, socket.timeout, ConnectionError, ValueError) as error:
        raise RetryLater(30.0, f"token endpoint unreachable ({type(error).__name__})") from None
    if not isinstance(parsed, dict):
        raise OAuthError("invalid_response", 200)
    return parsed


def _error_code(error: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(error.read() or b"{}")
    except (ValueError, OSError):
        return "unknown"
    code = str(payload.get("error") or payload.get("status") or "").lower() if isinstance(payload, dict) else ""
    return code if _ERROR_CODE.match(code) else "unknown"


def _tokens(payload: dict, previous_refresh: str = "", now: float | None = None) -> Tokens:
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise OAuthError("invalid_response", 200)
    # A vendor that does not rotate (HubSpot) may leave the refresh token out of a refresh answer.
    refresh = payload.get("refresh_token") or previous_refresh
    try:
        expires_in = float(payload.get("expires_in") or DEFAULT_EXPIRES_IN)
    except (TypeError, ValueError):
        expires_in = DEFAULT_EXPIRES_IN
    extra = {key: value for key, value in payload.items() if key not in ("access_token", "refresh_token", "id_token")}
    return Tokens(access, str(refresh or ""), (now if now is not None else time.time()) + max(60.0, expires_in), str(payload.get("scope") or ""), extra)


def exchange_code(spec: OAuthSpec, code: str, redirect_uri: str) -> Tokens:
    """Trade an authorization code for tokens. Raises ``OAuthError`` when the vendor refuses it."""
    payload = _post(
        spec.token_url,
        {"grant_type": "authorization_code", "client_id": client_id(spec), "client_secret": client_secret(spec), "code": code, "redirect_uri": redirect_uri},
        spec.token_body,
    )
    tokens = _tokens(payload)
    if not tokens.refresh_token:
        # Without one the install dies in an hour (Atlassian: offline_access was not granted).
        raise OAuthError("no_refresh_token", 200)
    return tokens


def refresh(spec: OAuthSpec, refresh_token: str) -> Tokens:
    """Spend ``refresh_token`` for a new access token (and, with Atlassian, a new refresh token)."""
    payload = _post(
        spec.token_url,
        {"grant_type": "refresh_token", "client_id": client_id(spec), "client_secret": client_secret(spec), "refresh_token": refresh_token},
        spec.token_body,
    )
    return _tokens(payload, previous_refresh=refresh_token)


# --- the store (package B) ---------------------------------------------------------------------
# Every call into package B's store goes through these two functions.


def _read_secret(install: Install) -> dict:
    from src.connectors import store

    return store.read_secret(_org(install), install.key)


def _write_secret(install: Install, secret: dict) -> None:
    from src.connectors import store

    if not store.write_secret(_org(install), install.key, secret):
        raise LookupError("the install is gone")


def _org(install: Install) -> str:
    return getattr(install, "org", "") or ""


# --- bearer_for --------------------------------------------------------------------------------

# (org, install key) -> (access token, expires_at). Process-local; the store is the truth.
_cache: dict[tuple[str, str], tuple[str, float]] = {}


def lock_key(install: Install) -> str:
    return f"connector:refresh:{_org(install)}:{install.key}"


def _fresh(expires_at, now: float) -> bool:
    try:
        return float(expires_at) - REFRESH_SKEW_SECONDS > now
    except (TypeError, ValueError):
        return False


def _usable(install: Install, secret: dict, now: float) -> str | None:
    """The stored access token if it is fresh, else None. (The store has already checked that the
    secret was written for this org and install.)"""
    token = secret.get("access_token")
    if isinstance(token, str) and token and _fresh(secret.get("expires_at"), now):
        _cache[(_org(install), install.key)] = (token, float(secret["expires_at"]))
        return token
    return None


def bearer_for(install: Install, spec: OAuthSpec | None = None) -> str:
    """A fresh access token for this OAuth install, refreshed (and the rotation saved) when needed."""
    if spec is None:
        from src.connectors import registry

        connector = registry.get(install.source)
        spec = getattr(connector, "oauth", None)
    if spec is None:
        raise ConnectorError(f"{install.source} has no OAuth mode")
    now = time.time()
    cached = _cache.get((_org(install), install.key))
    if cached and _fresh(cached[1], now):
        return cached[0]
    if token := _usable(install, _read_secret(install), now):
        return token

    lock, owner = lock_key(install), secrets.token_hex(16)
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while not kv().set(lock, owner, nx=True, ex=LOCK_TTL_SECONDS):
        # Someone else is refreshing: wait for their write rather than spend the refresh token twice.
        _sleep(LOCK_POLL_SECONDS)
        if token := _usable(install, _read_secret(install), time.time()):
            return token
        if time.monotonic() > deadline:
            raise RetryLater(LOCK_TTL_SECONDS, f"{install.key}: a token refresh is in progress")
    try:
        secret = _read_secret(install)
        if token := _usable(install, secret, time.time()):
            return token
        refresh_token = secret.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise CredentialsRejected(f"{install.key}: no refresh token stored; reconnect it")
        if not configured(spec):
            raise ConnectorError(f"{install.source} OAuth app credentials are not set ({spec.client_id_setting}, {spec.client_secret_setting})")
        try:
            tokens = refresh(spec, refresh_token)
        except OAuthError as error:
            if error.status in (400, 401, 403):
                log.warning("[oauth] %s refresh refused (%s); the install must be reconnected", install.key, error.code)
                raise CredentialsRejected(f"{install.key}: the authorization was revoked or expired ({error.code}); reconnect it") from None
            raise
        updated = {**secret, **tokens.as_secret(), "refreshed_at": int(time.time())}
        try:
            _write_secret(install, updated)
        except Exception as error:
            # Not used: an unsaved rotation must not be relied on. Atlassian accepts the old refresh
            # token again within its reuse interval, so the next step retries.
            log.error("[oauth] %s refreshed but the new tokens could not be saved: %s", install.key, type(error).__name__)
            raise ConnectorError(f"{install.key}: refreshed tokens could not be saved") from None
        _cache[(_org(install), install.key)] = (tokens.access_token, tokens.expires_at)
        log.info("[oauth] %s token refreshed; rotated=%s", install.key, tokens.refresh_token != refresh_token)
        return tokens.access_token
    finally:
        try:
            kv().eval(_RELEASE, 1, lock, owner)
        except Exception:
            log.warning("[oauth] %s could not release the refresh lock; it expires in %ds", install.key, LOCK_TTL_SECONDS)


def forget(install: Install) -> None:
    """Drop the cached token (disconnect, reconnect)."""
    _cache.pop((_org(install), install.key), None)


_sleep = time.sleep
