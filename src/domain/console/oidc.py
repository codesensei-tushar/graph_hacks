"""Sign in with Slack (OpenID Connect).

``begin`` stores a state with its nonce and PKCE verifier in kv for ten minutes and
returns Slack's authorize URL plus a browser binding: a random value the start route
sets as the ``__Host-brain_signin`` cookie, stored with the state only as a hash. The
callback must present the same cookie, so a code and state minted in an attacker's
browser can't sign a victim's browser in as the attacker (login CSRF).
``finish`` takes the state back exactly once, checks the binding, swaps the
code at ``openid.connect.token`` and verifies the id_token with PyJWT against Slack's
published keys: RS256 only, our client id as audience, Slack as issuer, a live
``exp`` and the nonce we sent. Anything else raises ``SignInError`` with a fixed code;
Slack's own error text never reaches the browser.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass

import httpx
import jwt

from src.core.config import settings
from src.domain.console.csrf import same
from src.db.falkordb_client import kv

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://slack.com/openid/connect/authorize"
JWKS_URL = "https://slack.com/openid/connect/keys"
ISSUER = "https://slack.com"
SCOPES = "openid email profile"
STATE_TTL_SECONDS = 600
CALLBACK_PATH = "/auth/slack/callback"
CLAIM_TEAM = "https://slack.com/team_id"
CLAIM_USER = "https://slack.com/user_id"
BINDING_COOKIE = "__Host-brain_signin"

# Tests swap in an httpx.MockTransport that plays Slack's token and keys endpoints.
transport: httpx.BaseTransport | None = None


class SignInError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SlackIdentity:
    team_id: str
    user_id: str
    email: str | None
    email_verified: bool
    name: str | None


def callback_url(origin: str) -> str:
    return f"{origin.rstrip('/')}{CALLBACK_PATH}"


def _state_key(state: str) -> str:
    return f"console:oidc:{state}"


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _binding_hash(binding: str) -> str:
    return hashlib.sha256(binding.encode()).hexdigest()


def begin(origin: str) -> tuple[str, str]:
    """``(authorize_url, binding)``. The caller sets ``binding`` as the BINDING_COOKIE."""
    binding = secrets.token_urlsafe(32)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    kv().set(_state_key(state), json.dumps({"nonce": nonce, "verifier": verifier, "binding": _binding_hash(binding)}), ex=STATE_TTL_SECONDS)
    query = httpx.QueryParams(
        {
            "response_type": "code",
            "scope": SCOPES,
            "client_id": settings.slack_client_id,
            "redirect_uri": callback_url(origin),
            "state": state,
            "nonce": nonce,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE_URL}?{query}", binding


def take_state(state: str | None) -> dict | None:
    """The stored nonce and verifier, deleted in the same step so a state works once."""
    if not state or len(state) > 128:
        return None
    raw = kv().getdel(_state_key(state))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k] for k in ("nonce", "verifier", "binding")):
        return None
    return data


def _http() -> httpx.Client:
    return httpx.Client(timeout=10.0, transport=transport)


def exchange_code(code: str, verifier: str, origin: str) -> str:
    """The id_token for an authorization code."""
    url = settings.slack_api_base_url.rstrip("/") + "/openid.connect.token"
    try:
        with _http() as http:
            response = http.post(
                url,
                data={
                    "client_id": settings.slack_client_id,
                    "client_secret": settings.slack_client_secret,
                    "code": code,
                    "redirect_uri": callback_url(origin),
                    "grant_type": "authorization_code",
                    "code_verifier": verifier,
                },
            )
        data = response.json()
    except (httpx.HTTPError, ValueError):
        log.warning("[console] openid.connect.token unreachable or not JSON")
        raise SignInError("slack_unavailable") from None
    if not isinstance(data, dict) or data.get("ok") is not True or not isinstance(data.get("id_token"), str):
        log.warning("[console] openid.connect.token refused: %s", (data or {}).get("error") if isinstance(data, dict) else "bad body")
        raise SignInError("slack_refused")
    return data["id_token"]


class _Keys:
    """Slack's signing keys, cached for an hour and refetched (at most once a minute) for an unknown kid."""

    MAX_AGE = 3600
    MIN_REFETCH = 60

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at = 0.0

    def clear(self) -> None:
        with self._lock:
            self._keys, self._fetched_at = {}, 0.0

    def _fetch(self) -> None:
        try:
            with _http() as http:
                data = http.get(JWKS_URL).json()
            keyset = jwt.PyJWKSet.from_dict(data)
        except (httpx.HTTPError, ValueError, jwt.PyJWKSetError):
            log.warning("[console] could not load Slack's signing keys")
            raise SignInError("slack_unavailable") from None
        self._keys = {key.key_id: key for key in keyset.keys if key.key_id}
        self._fetched_at = time.monotonic()

    def get(self, kid: str) -> jwt.PyJWK:
        with self._lock:
            age = time.monotonic() - self._fetched_at
            stale = not self._fetched_at or age > self.MAX_AGE
            if stale or (kid not in self._keys and age > self.MIN_REFETCH):
                self._fetch()
            key = self._keys.get(kid)
        if key is None:
            raise SignInError("bad_token")
        return key


keys = _Keys()


def verify_id_token(id_token: str, nonce: str) -> SlackIdentity:
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.InvalidTokenError:
        raise SignInError("bad_token") from None
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise SignInError("bad_token")
    key = keys.get(header["kid"])
    try:
        claims = jwt.decode(
            id_token,
            key=key.key,
            algorithms=["RS256"],
            audience=settings.slack_client_id,
            issuer=ISSUER,
            leeway=60,
            options={"require": ["exp", "iat", "aud", "iss", "sub", "nonce"]},
        )
    except jwt.InvalidTokenError as exc:
        log.warning("[console] id_token rejected: %s", type(exc).__name__)
        raise SignInError("bad_token") from None
    if not isinstance(claims.get("nonce"), str) or not same(claims["nonce"], nonce):
        raise SignInError("bad_token")
    team_id, user_id = claims.get(CLAIM_TEAM), claims.get(CLAIM_USER) or claims.get("sub")
    if not (isinstance(team_id, str) and team_id and isinstance(user_id, str) and user_id):
        raise SignInError("bad_token")
    email = claims.get("email") if isinstance(claims.get("email"), str) else None
    return SlackIdentity(
        team_id=team_id,
        user_id=user_id,
        email=email,
        email_verified=claims.get("email_verified") is True,
        name=claims.get("name") if isinstance(claims.get("name"), str) else None,
    )


def finish(code: str | None, state: str | None, origin: str, binding: str | None) -> SlackIdentity:
    stored = take_state(state)
    if stored is None:
        raise SignInError("expired")
    if not binding or len(binding) > 128 or not same(_binding_hash(binding), stored["binding"]):
        raise SignInError("wrong_browser")
    if not code or len(code) > 512:
        raise SignInError("denied")
    return verify_id_token(exchange_code(code, stored["verifier"], origin), stored["nonce"])
