"""GitHub REST calls as the GitHub App.

The app signs a short JWT with its private key, trades it for an installation token
(cached until shortly before it expires), and reads with that token. Everything here
is read-only. ``GitHubClient`` is the seam tests replace with a fake. The app's id, key
and OAuth credentials come from ``app_config.current()``: the environment, or the app
registered from the operator page.

A refusal is ``GitHubError``; when GitHub says to wait (a 429, or a 403 with
``X-RateLimit-Remaining: 0`` or ``Retry-After``), ``retry_after`` holds the seconds to wait.
"""

import base64
import calendar
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import jwt

from src.core.config import settings
from src.integrations.github import app_config

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


class NotConfigured(GitHubError):
    def __init__(self):
        super().__init__(503, "no GitHub App is configured")


def _retry_after(status: int, headers) -> float | None:
    """The wait a rate-limited answer asks for: ``Retry-After``, else until ``X-RateLimit-Reset``."""
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


def _creds() -> app_config.AppCredentials:
    creds = app_config.current()
    if creds is None or not creds.private_key:
        raise NotConfigured()
    return creds


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
    creds = _creds()
    now = int(time.time())
    # iat is backdated a minute for clock drift; GitHub caps exp at ten minutes.
    return jwt.encode({"iat": now - 60, "exp": now + 9 * 60, "iss": creds.app_id}, creds.private_key, algorithm="RS256")


# --- as the app itself (JWT) -------------------------------------------------------------------


def get_app() -> dict:
    """``GET /app``: the app's own record (slug, owner). Needs the JWT."""
    data, _ = _request("GET", f"{API}/app", app_jwt())
    return data or {}


def get_installation(installation_id: int) -> dict:
    """``GET /app/installations/{id}``: proves the installation is this app's, and names its account."""
    data, _ = _request("GET", f"{API}/app/installations/{int(installation_id)}", app_jwt())
    return data or {}


def list_app_installations(limit: int = 300) -> list[dict]:
    url: str | None = f"{API}/app/installations?per_page=100"
    found: list[dict] = []
    while url and len(found) < limit:
        data, headers = _request("GET", url, app_jwt())
        found.extend(data or [])
        url = _next_link(headers)
    return found[:limit]


def convert_manifest(code: str) -> dict:
    """``POST /app-manifests/{code}/conversions``: the new app's id, slug, pem, webhook secret and
    OAuth credentials. No authentication: the one-hour code is the credential."""
    data, _ = _request("POST", f"{API}/app-manifests/{urllib.parse.quote(code, safe='')}/conversions")
    return data or {}


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
    """Reads a public repo without an installation of its own. GitHub answers public data to any
    caller; what differs is the rate limit. Tried in order, each dropped for good on a 401:

    1. ``GITHUB_PUBLIC_REPO_TOKEN``: a token of the operator's (a fine-grained token with public read
       only is enough): 5,000 requests an hour.
    2. An installation token of this app's installation on its owner's own account (installation
       tokens read public repos too): 5,000 an hour. Never another company's installation, whose
       rate limit is theirs.
    3. The app's client id and secret as basic auth, which GitHub documents for OAuth apps
       (5,000 an hour; https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api).
    4. No authentication: 60 an hour. Slow, but the backfill waits out every limit, so it finishes.

    Only GETs of public data go out this way; collaborators are never read (a public repo is
    ``org_shared``, readable by everyone).
    """

    def __init__(self):
        super().__init__(0)
        self._dropped: set[str] = set()

    def _candidates(self) -> list[tuple[str, str | None, str]]:
        found: list[tuple[str, str | None, str]] = []
        if settings.github_public_repo_token:
            found.append(("token", settings.github_public_repo_token, "Bearer"))
        creds = app_config.current()
        if creds is not None and creds.private_key and "installation" not in self._dropped:
            installation = _any_installation()
            if installation is not None:
                try:
                    found.append(("installation", _installation_token(installation), "Bearer"))
                except Exception as error:
                    log.info("[github] no installation token for public reads: %s", type(error).__name__)
        if creds is not None and creds.client_id and creds.client_secret:
            basic = base64.b64encode(f"{creds.client_id}:{creds.client_secret}".encode()).decode()
            found.append(("client", basic, "Basic"))
        found.append(("anonymous", None, "Bearer"))
        return [item for item in found if item[0] not in self._dropped]

    def _call(self, url: str) -> tuple[object, dict]:
        last: GitHubError | None = None
        for name, token, scheme in self._candidates():
            try:
                return _request("GET", url, token, auth=scheme)
            except GitHubError as error:
                if error.status == 401 and name != "anonymous":
                    log.warning("[github] public reads: %s credentials refused; trying the next", name)
                    self._dropped.add(name)
                    last = error
                    continue
                raise
        raise last or GitHubError(401, "no way to read public data")

    @property
    def mode(self) -> str:
        candidates = self._candidates()
        return candidates[0][0] if candidates else "anonymous"


_installation_cache: tuple[float, int | None] | None = None


def _any_installation() -> int | None:
    """The app's unsuspended installation on its owner's account, cached ten minutes; None without one."""
    global _installation_cache
    now = time.monotonic()
    if _installation_cache is not None and now - _installation_cache[0] < 600:
        return _installation_cache[1]
    found = None
    try:
        creds = app_config.current()
        owner = (creds.owner_id if creds else "") or str((get_app().get("owner") or {}).get("id") or "")
        for installation in list_app_installations(limit=100):
            if owner and str((installation.get("account") or {}).get("id")) == owner and not installation.get("suspended_at"):
                found = int(installation["id"])
                break
    except Exception as error:
        log.info("[github] installations unavailable for public reads: %s", type(error).__name__)
    _installation_cache = (now, found)
    return found


def public_client() -> PublicClient:
    return PublicClient()


# --- user sign-in, only to confirm who someone is on GitHub --------------------------------


def authorize_url(state: str, redirect_uri: str) -> str:
    creds = app_config.current()
    query = urllib.parse.urlencode({"client_id": creds.client_id if creds else "", "state": state, "redirect_uri": redirect_uri})
    return f"{OAUTH}/authorize?{query}"


def _user_token(code: str, redirect_uri: str) -> str:
    creds = app_config.current()
    if creds is None or not creds.can_link:
        raise NotConfigured()
    params = urllib.parse.urlencode({"client_id": creds.client_id, "client_secret": creds.client_secret, "code": code, "redirect_uri": redirect_uri}).encode()
    request = urllib.request.Request(f"{OAUTH}/access_token", data=params, method="POST", headers={"Accept": "application/json", "User-Agent": "company-brain"})
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
        token = json.loads(response.read()).get("access_token")
    if not token:
        raise GitHubError(400, "GitHub did not return a user token")
    return token


def exchange_code_for_identity(code: str, redirect_uri: str) -> GitHubUserIdentity:
    """Trade the OAuth code for a user token, read who it belongs to, then drop the token."""
    token = _user_token(code, redirect_uri)
    user, _ = _request("GET", f"{API}/user", token)
    return GitHubUserIdentity(id=int(user["id"]), login=str(user["login"]))


def user_can_access_installation(code: str, redirect_uri: str, installation_id: int) -> GitHubUserIdentity | None:
    """Trade the OAuth code for a user token and check that the installation is one the user can reach
    (``GET /user/installations``), then drop the token. The setup URL's ``installation_id`` is anyone's
    to type, so this is what proves the person in the browser installed (or administers) it. Returns the
    user, or None when the installation is not theirs."""
    token = _user_token(code, redirect_uri)
    user, _ = _request("GET", f"{API}/user", token)
    url: str | None = f"{API}/user/installations?per_page=100"
    while url:
        data, headers = _request("GET", url, token)
        for installation in (data or {}).get("installations") or []:
            if int(installation.get("id") or 0) == int(installation_id):
                return GitHubUserIdentity(id=int(user["id"]), login=str(user["login"]))
        url = _next_link(headers)
    return None
