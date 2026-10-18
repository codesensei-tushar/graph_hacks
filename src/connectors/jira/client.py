"""Jira Cloud REST v3 calls with an Atlassian API token.

One site (``CONNECTOR_JIRA_BASE_URL``), HTTP Basic with the account's email and a
classic API token, through ``src.connectors.http.JsonClient``: it retries short 429 and
503 waits (``Retry-After``) and 5xx in place, raises ``RetryLater`` for long ones and
``CredentialsRejected`` on a 401, never sends the token to another host, and never logs
it. The connector only reads: who the token belongs to, projects, their components, and
issues through ``POST /rest/api/3/search/jql`` (the old ``/search`` is gone), paged by
``nextPageToken``. ``scripts/seed_jira.py`` also writes through ``http``, and only to the
projects it created.
"""

import re
import urllib.parse
from collections.abc import Iterable, Iterator

from src.connectors.http import HttpError, JsonClient, basic_auth

# A project key as Jira allows it. Keys reach JQL only after this check.
PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,9}$")
# What the connector reads of an issue.
ISSUE_FIELDS = (
    "summary", "description", "status", "issuetype", "priority", "parent", "project",
    "reporter", "assignee", "components", "labels", "issuelinks", "security", "updated",
)
PAGE_SIZE = 100


def site_problems(base_url: str) -> list[str]:
    """Why a base URL is not a Jira site's https origin. Never names a credential."""
    parsed = urllib.parse.urlsplit(base_url or "")
    if parsed.scheme != "https" or not parsed.hostname or parsed.path.strip("/") or parsed.query:
        return ["CONNECTOR_JIRA_BASE_URL must be the site's https origin, e.g. https://example.atlassian.net"]
    return []


def site_host(base_url: str) -> str:
    return urllib.parse.urlsplit(base_url).hostname or ""


class JiraClient:
    """Calls to one Jira Cloud site as the token's account. Tests replace it with a fake."""

    def __init__(self, base_url: str, email: str, api_token: str):
        problems = site_problems(base_url)
        if problems:
            raise ValueError(problems[0])
        self.base_url = base_url.rstrip("/")
        self.http = JsonClient(self.base_url, headers=basic_auth(email, api_token))

    @classmethod
    def for_install(cls, install) -> "JiraClient":
        return cls(install.get("base_url"), install.get("email"), install.get("api_token"))

    def __repr__(self) -> str:
        return f"JiraClient({self.base_url!r})"

    def myself(self) -> dict:
        return self.http.get("/rest/api/3/myself") or {}

    def projects(self, keys: Iterable[str]) -> list[dict]:
        """The projects among ``keys`` the account can browse. Paged by ``startAt``."""
        keys = sorted(key for key in keys if PROJECT_KEY.fullmatch(key))
        found: list[dict] = []
        while keys:
            page = self.http.get("/rest/api/3/project/search", {"keys": keys, "maxResults": 50, "startAt": len(found)}) or {}
            values = page.get("values") or []
            found.extend(values)
            if page.get("isLast", True) or not values:
                break
        return found

    def project(self, key: str) -> dict | None:
        try:
            return self.http.get(f"/rest/api/3/project/{_segment(key)}")
        except HttpError as error:
            if error.status == 404:
                return None
            raise

    def components(self, project_key: str) -> list[dict]:
        return self.http.get(f"/rest/api/3/project/{_segment(project_key)}/components") or []

    def search_page(self, jql: str, next_page_token: str | None = None, max_results: int = PAGE_SIZE, fields: Iterable[str] = ISSUE_FIELDS) -> dict:
        """One page of ``/search/jql``: ``{"issues": [...], "nextPageToken": ..., "isLast": ...}``."""
        body: dict = {"jql": jql, "fields": list(fields), "maxResults": max_results}
        if next_page_token:
            body["nextPageToken"] = next_page_token
        return self.http.post("/rest/api/3/search/jql", body) or {}

    def search(self, jql: str, limit: int = 500, fields: Iterable[str] = ISSUE_FIELDS) -> Iterator[dict]:
        """Every issue the JQL finds, up to ``limit``, following ``nextPageToken``."""
        token: str | None = None
        seen = 0
        while seen < limit:
            page = self.search_page(jql, token, min(PAGE_SIZE, limit - seen), fields)
            issues = (page.get("issues") or [])[: limit - seen]
            seen += len(issues)
            yield from issues
            token = page.get("nextPageToken")
            if page.get("isLast") or not token or not issues:
                return

    def issue_links(self, key: str) -> list[dict]:
        issue = self.http.get(f"/rest/api/3/issue/{_segment(key)}", {"fields": "issuelinks"}) or {}
        return (issue.get("fields") or {}).get("issuelinks") or []

    def issue_link_types(self) -> list[dict]:
        return (self.http.get("/rest/api/3/issueLinkType") or {}).get("issueLinkTypes") or []


def _segment(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")
