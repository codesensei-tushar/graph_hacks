"""GitHub REST calls as the GitHub App.

The app signs a short JWT with its private key, trades it for an installation token
(cached until shortly before it expires), and reads with that token. Everything here
is read-only. ``GitHubClient`` is the seam tests replace with a fake.

A refusal is ``GitHubError``; when GitHub says to wait (a 429, or a 403 with
``X-RateLimit-Remaining: 0`` or ``Retry-After``), ``retry_after`` holds the seconds to wait.
``PublicClient`` reads public repos with no installation (``public_repos``).
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
    def __init__(self, status: int, message: str, retry_after: float | None = None):
        super().__init__(f"GitHub {status}: {message}")
        self.status = status
        # Seconds GitHub asked us to wait (rate limits), or None.
        self.retry_after = retry_after


def _retry_after(status: int, headers) -> float | None:
    """The wait a rate-limited answer asks for: ``Retry-After``, else until ``X-RateLimit-Reset``
    (https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)."""
    if status not in (403, 429) or headers is None:
        return None
    after = headers.get("Retry-After")
    if after:
        try:
            return max(1.0, float(after))
        except ValueError:
            pass
    if headers.get("X-RateLimit-Remaining") == "0" and headers.get("X-RateLimit-Reset"):
        try:
            return max(1.0, float(headers["X-RateLimit-Reset"]) - time.time() + 1)
        except ValueError:
            return 60.0
    return 60.0 if status == 429 else None


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
        raise GitHubError(error.code, detail, _retry_after(error.code, error.headers)) from error


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

    def _call(self, url: str) -> tuple[object, dict]:
        return _request("GET", url, self._token())

    def _get(self, path: str, params: dict | None = None) -> object:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        data, _ = self._call(f"{API}{path}{query}")
        return data

    def _get_all(self, path: str, params: dict | None = None, limit: int = 300, key: str | None = None) -> list[dict]:
        url: str | None = f"{API}{path}?{urllib.parse.urlencode({'per_page': 100, **(params or {})})}"
        items: list[dict] = []
        while url and len(items) < limit:
            data, headers = self._call(url)
            page = (data or {}).get(key, []) if key else (data or [])
            items.extend(page)
            url = _next_link(headers)
        return items[:limit]

    def _page(self, path: str, params: dict, page: int, per_page: int) -> tuple[list[dict], bool]:
        """One page of a list, and whether GitHub has another after it."""
        query = urllib.parse.urlencode({**params, "per_page": per_page, "page": page})
        data, headers = self._call(f"{API}{path}?{query}")
        return list(data or []), _next_link(headers) is not None

    def get_repo(self, full_name: str) -> dict:
        return self._get(f"/repos/{full_name}") or {}

    def pull_requests_page(self, full_name: str, page: int, per_page: int, sort: str = "created") -> tuple[list[dict], bool]:
        return self._page(f"/repos/{full_name}/pulls", {"state": "all", "sort": sort, "direction": "desc"}, page, per_page)

    def issues_page(self, full_name: str, page: int, per_page: int, since: str | None = None) -> tuple[list[dict], bool]:
        params = {"state": "all", "sort": "updated" if since else "created", "direction": "desc"}
        if since:
            params["since"] = since
        return self._page(f"/repos/{full_name}/issues", params, page, per_page)

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


class PublicClient(GitHubClient):
    """Reads a public repo without an installation. GitHub answers public data to any caller; what
    differs is the rate limit: with ``GITHUB_PUBLIC_REPO_TOKEN`` (a fine-grained token with public
    read access only) 5,000 requests an hour, without it 60
    (https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api). A token
    GitHub refuses (401) is dropped and the read goes on unauthenticated. Only GETs of public data
    go out this way; collaborators are never read (a public repo is ``org_shared``)."""

    def __init__(self):
        super().__init__(0)
        self._token_refused = False

    @property
    def mode(self) -> str:
        return "token" if settings.github_public_repo_token and not self._token_refused else "anonymous"

    def _call(self, url: str) -> tuple[object, dict]:
        if self.mode == "token":
            try:
                return _request("GET", url, settings.github_public_repo_token)
            except GitHubError as error:
                if error.status != 401:
                    raise
                log.warning("[github] GITHUB_PUBLIC_REPO_TOKEN was refused; reading public repos unauthenticated")
                self._token_refused = True
        return _request("GET", url, None)


def public_client() -> PublicClient:
    return PublicClient()


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
