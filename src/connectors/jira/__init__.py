"""The Jira Cloud connector: allow-listed projects, polled with an updated-since cursor.

    CONNECTOR_JIRA_BASE_URL   https://example.atlassian.net
    CONNECTOR_JIRA_EMAIL      the account the API token belongs to
    CONNECTOR_JIRA_API_TOKEN  a classic Atlassian API token
    CONNECTOR_JIRA_PROJECTS   PAY,PLAT: only these projects are read
    CONNECTOR_JIRA_PEOPLE     optional: accountId=email pairs for people whose email Jira hides

One code path for the backfill and every poll after it. The backfill (no cursor) reads the
project's issues updated in the last ``BACKFILL_DAYS`` days, newest first, up to
``MAX_ISSUES_PER_PROJECT``, a page at a time; the cursor then holds the newest ``updated``
it saw. Each poll asks for issues updated since then, oldest first, so the cursor only
moves forward. The window is written relative to now (``updated >= -42m``) because JQL
reads absolute times in the token owner's time zone. A few minutes of overlap make a poll
read some issues again; writing one twice changes nothing. A poll that catches up moves the
cursor to when it began, less the overlap, so a quiet project does not re-read its last
updates forever.

Issues with a security level are never written, and are deleted if an earlier poll wrote
them. Deleted issues are not detected in v1. See docs/JIRA.md.
"""

import json
import logging
import math
from datetime import UTC, datetime, timedelta

from src.connectors import registry
from src.connectors.base import Connector, ConnectorError, Container, Edge, Install, InstallInfo, Page
from src.connectors.jira import mapper
from src.connectors.jira.client import PAGE_SIZE, PROJECT_KEY, JiraClient, site_host, site_problems

log = logging.getLogger(__name__)

BACKFILL_DAYS = 90
MAX_ISSUES_PER_PROJECT = 500
# Minutes of overlap on every poll, for clock skew between us and Jira.
OVERLAP_MINUTES = 5


def client_for(install: Install) -> JiraClient:
    problems = site_problems(install.get("base_url"))
    if problems:
        raise ConnectorError(problems[0])
    return JiraClient.for_install(install)


def people(install: Install) -> dict[str, str]:
    """``CONNECTOR_JIRA_PEOPLE``: accountId -> email, asserted by an admin for people whose email Jira hides."""
    found = {}
    for pair in install.get_list("people"):
        account_id, _, email = pair.partition("=")
        if account_id.strip() and "@" in email:
            found[account_id.strip()] = email.strip()
        else:
            log.warning("[jira] CONNECTOR_JIRA_PEOPLE has an entry that is not accountId=email; skipped")
    return found


class JiraConnector(Connector):
    source = "jira"
    poll_seconds = 300
    required = ("base_url", "email", "api_token", "projects")
    accepts_webhooks = False

    def __init__(self) -> None:
        # Seams for tests: the client, and what time it is.
        self.client_for = client_for
        self.now = lambda: datetime.now(UTC)

    def check_credentials(self, install: Install) -> InstallInfo:
        me = self.client_for(install).myself()
        return InstallInfo(account=me.get("displayName") or me.get("accountId") or "", site=site_host(install.get("base_url")))

    def containers(self, install: Install) -> list[Container]:
        wanted = {key.upper() for key in install.get_list("projects")}
        bad = sorted(key for key in wanted if not PROJECT_KEY.fullmatch(key))
        if bad:
            log.warning("[jira] CONNECTOR_JIRA_PROJECTS has values that are not project keys: %s", ", ".join(bad))
        found = self.client_for(install).projects(wanted - set(bad))
        missing = sorted(wanted - set(bad) - {project.get("key") for project in found})
        if missing:
            log.warning("[jira] projects not visible to the token, not synced: %s", ", ".join(missing))
        base_url = install.get("base_url").rstrip("/")
        return [mapper.container(install, project, base_url) for project in found if project.get("key") in wanted]

    def fetch(self, install: Install, container: Container, cursor: str | None) -> Page:
        if not container.external_id.isdigit():
            raise ConnectorError(f"project id {container.external_id!r} is not a Jira project id")
        state = _load(cursor)
        client = self.client_for(install)
        if "since" in state:
            return self._poll(install, client, container, state)
        return self._backfill(install, client, container, state)

    def _backfill(self, install: Install, client, container: Container, state: dict) -> Page:
        jql = state.get("jql") or f"project = {container.external_id} AND updated >= -{BACKFILL_DAYS}d ORDER BY updated DESC"
        seen = int(state.get("seen") or 0)
        result = client.search_page(jql, state.get("token"), min(PAGE_SIZE, MAX_ISSUES_PER_PROJECT - seen))
        page, newest = self._page(install, container, result.get("issues") or [])
        newest = max(filter(None, (state.get("newest"), newest)), default=None)
        seen += len(result.get("issues") or [])
        token = result.get("nextPageToken")
        # Where polling picks up: what changed since the newest issue seen (the poll applies the overlap).
        # The framework switches to it when the project reaches its cap before the backfill ends.
        page.poll_cursor = json.dumps({"since": newest or mapper.format_time(self.now())})
        if token and not result.get("isLast") and result.get("issues") and seen < MAX_ISSUES_PER_PROJECT:
            page.next_cursor = json.dumps({"jql": jql, "token": token, "seen": seen, "newest": newest})
            page.has_more = True
        else:
            page.next_cursor = page.poll_cursor
        log.info("[jira] backfill project=%s issues=%d written=%d more=%s", container.name, seen, len(page.items), page.has_more)
        return page

    def _poll(self, install: Install, client, container: Container, state: dict) -> Page:
        since = state["since"]
        jql = state.get("jql")
        polled_at = self.now()
        if not jql:
            started = mapper.parse_time(since) or polled_at
            minutes = max(1, math.ceil((polled_at - started).total_seconds() / 60)) + OVERLAP_MINUTES
            jql = f"project = {container.external_id} AND updated >= -{minutes}m ORDER BY updated ASC"
        result = client.search_page(jql, state.get("token"), PAGE_SIZE)
        page, newest = self._page(install, container, result.get("issues") or [])
        newest = max(filter(None, (since, newest)))
        token = result.get("nextPageToken")
        if token and not result.get("isLast") and result.get("issues"):
            page.has_more = True
            # Oldest first, so everything not read yet was updated at or after ``newest``: start again
            # from there. When a whole page shares one minute, follow the token instead.
            page.next_cursor = json.dumps({"since": newest} if newest > since else {"since": since, "jql": jql, "token": token})
        elif state.get("token"):
            # The end of a token-paged poll: its window was fixed when the first page was read.
            page.next_cursor = json.dumps({"since": newest})
        else:
            # Caught up: everything updated before this poll began has been read, so the cursor moves to
            # then, less the overlap, even when nothing changed. Otherwise a quiet project would re-read
            # its last burst of updates on every poll.
            checked = mapper.format_time(polled_at - timedelta(minutes=OVERLAP_MINUTES))
            page.next_cursor = json.dumps({"since": max(newest, checked)})
        if page.items or page.deleted_ids:
            log.info("[jira] poll project=%s written=%d deleted=%d more=%s", container.name, len(page.items), len(page.deleted_ids), page.has_more)
        return page

    def _page(self, install: Install, container: Container, issues: list[dict]) -> tuple[Page, str | None]:
        """The records for these issues, and the newest ``updated`` among them (UTC ISO), for the cursor."""
        page = Page()
        base_url = install.get("base_url").rstrip("/")
        known = people(install)
        users: dict[str, object] = {}
        edges: dict[tuple[str, str, str], Edge] = {}
        newest = None
        for raw in issues:
            updated = mapper.updated_at(raw)
            newest = max(filter(None, (newest, updated)), default=None)
            reason = mapper.skip_reason(raw, container)
            if reason == mapper.SECURITY:
                page.deleted_ids.append(install.item_id(raw["id"]))
            if reason:
                continue
            mapped = mapper.issue(install, raw, container, base_url, known)
            page.items.append(mapped.item)
            users.update((user.id, user) for user in mapped.users)
            edges.update(((edge.rel, edge.from_id, edge.to_id), edge) for edge in mapped.edges)
        page.users = list(users.values())
        page.edges = list(edges.values())
        return page, newest


def _load(cursor: str | None) -> dict:
    """The cursor's state. An unreadable cursor starts over with the backfill."""
    if not cursor:
        return {}
    try:
        state = json.loads(cursor)
    except ValueError:
        log.warning("[jira] unreadable cursor; starting over")
        return {}
    return state if isinstance(state, dict) else {}


registry.register(JiraConnector())
