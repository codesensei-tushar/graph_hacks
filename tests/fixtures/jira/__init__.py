"""Synthetic Jira Cloud responses in the shape the REST v3 API returns them, and a fake client over them.

Quillfern's projects PAY and PLAT (allow-listed in the tests) and HR (never allow-listed).
Every name, account and email is invented.
"""

import copy
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.connectors.base import CredentialsRejected
from src.connectors.jira import mapper

HERE = Path(__file__).parent
SITE = "https://quillfern.atlassian.net"
# What the fake takes "now" to be when it reads a relative JQL window.
NOW = datetime(2026, 10, 18, 6, 0, tzinfo=UTC)
CONFIG = {"base_url": SITE, "email": "maya@quillfern.example.com", "api_token": "fake-token-not-real", "projects": "PAY,PLAT"}

_PROJECT = re.compile(r"\bproject = (\d+)\b")
_SINCE = re.compile(r"\bupdated >= -(\d+)([dm])\b")


def load(name: str):
    return json.loads((HERE / name).read_text())


class FakeJiraClient:
    """Answers the calls ``JiraConnector`` makes, from the fixtures. Reads the JQL it is sent the way Jira
    would for the clauses the connector writes: ``project = <id>``, ``updated >= -<n>d|m`` and the order."""

    def __init__(self, issues: list[dict] | None = None, page_size: int = 100, now: datetime = NOW):
        self.issues = load("issues.json") if issues is None else issues
        self.page_size = page_size
        self.now = now
        self.queries: list[tuple[str, str | None, int]] = []
        self.rejected = False

    def myself(self) -> dict:
        if self.rejected:
            raise CredentialsRejected("GET /rest/api/3/myself answered 401")
        return load("myself.json")

    def projects(self, keys) -> list[dict]:
        keys = set(keys)
        return [self._with_insight(project) for project in load("projects.json")["values"] if project["key"] in keys]

    def all_projects(self, limit: int = 500) -> list[dict]:
        return [self._with_insight(project) for project in load("projects.json")["values"][:limit]]

    def _with_insight(self, project: dict) -> dict:
        """As ``project/search?expand=insight`` answers: the project's issue count under ``insight``."""
        count = sum(1 for issue in self.issues if issue["fields"]["project"]["id"] == project["id"])
        return {**project, "insight": {"totalIssueCount": count, "lastIssueUpdateTime": "2026-10-18T06:00:00.000+0000"}}

    def search_page(self, jql: str, next_page_token: str | None = None, max_results: int = 100, fields=None) -> dict:
        if self.rejected:
            raise CredentialsRejected("POST /rest/api/3/search/jql answered 401")
        self.queries.append((jql, next_page_token, max_results))
        project = _PROJECT.search(jql).group(1)
        found = [issue for issue in self.issues if issue["fields"]["project"]["id"] == project]
        if since := _SINCE.search(jql):
            amount, unit = int(since.group(1)), since.group(2)
            floor = self.now - (timedelta(days=amount) if unit == "d" else timedelta(minutes=amount))
            found = [issue for issue in found if mapper.parse_time(issue["fields"]["updated"]) >= floor]
        found.sort(key=lambda issue: (mapper.parse_time(issue["fields"]["updated"]), issue["key"]), reverse="ORDER BY updated DESC" in jql)
        start = int(next_page_token or 0)
        chunk = found[start : start + min(max_results, self.page_size)]
        end = start + len(chunk)
        page = {"issues": copy.deepcopy(chunk), "isLast": end >= len(found)}
        if end < len(found):
            page["nextPageToken"] = str(end)
        return page
