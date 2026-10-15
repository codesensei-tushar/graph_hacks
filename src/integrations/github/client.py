"""GitHub REST calls as the GitHub App.

The app signs a short JWT with its private key, trades it for an installation token
(cached until shortly before it expires), and reads with that token. Everything here
is read-only. ``GitHubClient`` is the seam tests replace with a fake.
"""

import calendar
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import jwt

from src.core.config import settings

log = logging.getLogger(__name__)

API = "https://api.github.com"
OAUTH = "https://github.com/login/oauth"
API_VERSION = "2022-11-28"
_TIMEOUT_SECONDS = 15
# Installation tokens live an hour; refresh with this much to spare.
_TOKEN_MARGIN_SECONDS = 300


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub {status}: {message}")
        self.status = status


@dataclass(frozen=True)
class GitHubUserIdentity:
    id: int
    login: str


def _request(method: str, url: str, token: str | None = None, body: dict | None = None, auth: str = "Bearer") -> tuple[object, dict]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION, "User-Agent": "company-brain"}
    if token:
        headers["Authorization"] = f"{auth} {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            raw = response.read()
            return (json.loads(raw) if raw else None), dict(response.headers)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:300]
        raise GitHubError(error.code, detail) from error


def _next_link(headers: dict) -> str | None:
    for part in (headers.get("Link") or headers.get("link") or "").split(","):
        url, _, rel = part.partition(";")
        if 'rel="next"' in rel:
            return url.strip().strip("<>")
    return None


def _private_key() -> str:
    if settings.github_private_key:
        return settings.github_private_key.replace("\\n", "\n")
    return Path(settings.github_private_key_path).expanduser().read_text()


class GitHubClient:
    """Read-only calls for one installation of the app."""

    def __init__(self, installation_id: int):
        self.installation_id = installation_id

    def _token(self) -> str:
        return _installation_token(self.installation_id)

    def _get(self, path: str, params: dict | None = None) -> object:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        data, _ = _request("GET", f"{API}{path}{query}", self._token())
        return data

    def _get_all(self, path: str, params: dict | None = None, limit: int = 300, key: str | None = None) -> list[dict]:
        url: str | None = f"{API}{path}?{urllib.parse.urlencode({'per_page': 100, **(params or {})})}"
        items: list[dict] = []
        while url and len(items) < limit:
            data, headers = _request("GET", url, self._token())
            page = (data or {}).get(key, []) if key else (data or [])
            items.extend(page)
            url = _next_link(headers)
        return items[:limit]

    def list_repositories(self) -> list[dict]:
        return self._get_all("/installation/repositories", key="repositories")

    def list_collaborators(self, full_name: str) -> list[dict]:
        return self._get_all(f"/repos/{full_name}/collaborators", {"affiliation": "all"})

    def list_pull_requests(self, full_name: str, limit: int) -> list[dict]:
        return self._get_all(f"/repos/{full_name}/pulls", {"state": "all", "sort": "updated", "direction": "desc"}, limit=limit)

    def list_pull_request_files(self, full_name: str, number: int) -> list[dict]:
        return self._get_all(f"/repos/{full_name}/pulls/{number}/files")

    def list_pull_request_reviews(self, full_name: str, number: int) -> list[dict]:
        return self._get_all(f"/repos/{full_name}/pulls/{number}/reviews")

    def list_issues(self, full_name: str, limit: int) -> list[dict]:
        # The issues endpoint also returns pull requests; callers skip those.
        return self._get_all(f"/repos/{full_name}/issues", {"state": "all", "sort": "updated", "direction": "desc"}, limit=limit)


def app_jwt() -> str:
    now = int(time.time())
    # iat is backdated a minute for clock drift; GitHub caps exp at ten minutes.
    return jwt.encode({"iat": now - 60, "exp": now + 9 * 60, "iss": settings.github_app_id}, _private_key(), algorithm="RS256")


_tokens: dict[int, tuple[str, float]] = {}
_tokens_lock = threading.Lock()


def _installation_token(installation_id: int) -> str:
    with _tokens_lock:
        cached = _tokens.get(installation_id)
        if cached and cached[1] - _TOKEN_MARGIN_SECONDS > time.time():
            return cached[0]
    data, _ = _request("POST", f"{API}/app/installations/{installation_id}/access_tokens", app_jwt())
    token = data["token"]
    expires = calendar.timegm(time.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))
    with _tokens_lock:
        _tokens[installation_id] = (token, expires)
    return token


def forget_installation_token(installation_id: int) -> None:
    with _tokens_lock:
        _tokens.pop(installation_id, None)


def client_for(installation_id: int) -> GitHubClient:
    return GitHubClient(installation_id)


# --- user sign-in, only to confirm who someone is on GitHub --------------------------------


def authorize_url(state: str, redirect_uri: str) -> str:
    query = urllib.parse.urlencode({"client_id": settings.github_client_id, "state": state, "redirect_uri": redirect_uri})
    return f"{OAUTH}/authorize?{query}"


def exchange_code_for_identity(code: str, redirect_uri: str) -> GitHubUserIdentity:
    """Trade the OAuth code for a user token, read who it belongs to, then drop the token."""
    params = urllib.parse.urlencode(
        {
            "client_id": settings.github_client_id,
            "client_secret": settings.github_client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }
    ).encode()
    request = urllib.request.Request(f"{OAUTH}/access_token", data=params, method="POST", headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
        token = json.loads(response.read()).get("access_token")
    if not token:
        raise GitHubError(400, "GitHub did not return a user token")
    user, _ = _request("GET", f"{API}/user", token)
    return GitHubUserIdentity(id=int(user["id"]), login=str(user["login"]))
