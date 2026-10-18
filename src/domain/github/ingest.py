"""Write GitHub pull requests, reviews, and issues into the company graph.

    (:GitHubUser)-[:AUTHORED]->(:PullRequest)-[:IN_REPO]->(:Repo)
    (:GitHubUser)-[:REVIEWED {state}]->(:PullRequest)
    (:GitHubUser)-[:REVIEW_REQUESTED]->(:PullRequest)    asked to review; GitHub drops it once they do
    (:PullRequest)-[:TOUCHES]->(:Service)       changed files under Service.paths
    (:PullRequest)-[:CLOSES]->(:Ticket)         "closes #12" in the description
    (:PullRequest)-[:REFERENCES|CLOSES]->(:Ticket {source: 'jira'})   "PAY-12" in the title, branch or description;
                                                "fixes PAY-12" closes it. Only keys of a synced Jira project count.
    (:GitHubUser)-[:OPENED|ASSIGNED]->(:Ticket)-[:IN_REPO]->(:Repo)
    (:Ticket)-[:ABOUT]->(:Service)              best match on title and body

Issues become ``Ticket`` nodes, so the existing who-knows and ticket tools see them.
Every node carries its repo's ``scope``. Only facts are kept: titles, descriptions,
people, file paths, and states. Code and diffs are never stored.
"""

import logging
import re

from src.connectors import graph as connector_graph
from src.db.falkordb_client import connect
from src.domain.github.repos import repo_scope
from src.domain.knowledge import ask_service

log = logging.getLogger(__name__)

SUMMARY_MAX_CHARS = 2000
_CLOSING = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b", re.IGNORECASE)
# A Jira issue key. Upper case in prose, any case in a branch name ("pay-12-retry-fix").
_JIRA_KEY = re.compile(r"(?<![A-Za-z0-9_-])([A-Z][A-Z0-9_]{1,9}-\d+)\b")
_JIRA_BRANCH_KEY = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9_]{1,9}-\d+)(?![0-9])")
_JIRA_CLOSING = re.compile(r"\b(?i:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+([A-Z][A-Z0-9_]{1,9}-\d+)\b")
# Where the connector's Jira projects are: their container's URL ends in /browse/<KEY>.
_JIRA_PROJECT_URL = re.compile(r"/browse/([A-Z][A-Z0-9_]{1,9})$")


def pull_request_id(repo_id: str | int, number: int) -> str:
    return f"github:{repo_id}:pr:{number}"


def issue_id(repo_id: str | int, number: int) -> str:
    return f"github:{repo_id}:issue:{number}"


def _summary(text: str | None) -> str:
    text = (text or "").strip()
    return text if len(text) <= SUMMARY_MAX_CHARS else text[: SUMMARY_MAX_CHARS - 1] + "…"


def _short_name(repo: dict, number: int) -> str:
    return f"{repo['full_name'].split('/')[-1]}#{number}"


def upsert_github_user(user: dict) -> str:
    """Upsert by GitHub's numeric id (stable across renames). Links to a Person whose
    ``github`` login matches, unless the user is already linked."""
    user_id = str(user["id"])
    login = str(user["login"])
    _, graph = connect()
    graph.query("MERGE (u:GitHubUser {id: $id}) SET u.login = $login, u.name = $login", {"id": user_id, "login": login})
    graph.query(
        """
        MATCH (u:GitHubUser {id: $id}), (p:Person)
        WHERE toLower(p.github) = toLower($login) AND NOT (u)-[:IS]->(:Person)
        MERGE (u)-[:IS]->(p)
        """,
        {"id": user_id, "login": login},
    )
    return user_id


def services_for_paths(paths: list[str]) -> list[str]:
    """Service ids whose ``paths`` prefixes cover any changed file (longest prefix wins)."""
    _, graph = connect()
    rows = graph.query("MATCH (s:Service) WHERE s.paths IS NOT NULL RETURN s.id, s.paths").result_set
    prefixes = [(prefix, service_id) for service_id, service_paths in rows for prefix in service_paths or []]
    prefixes.sort(key=lambda item: len(item[0]), reverse=True)
    matched: list[str] = []
    for path in paths:
        for prefix, service_id in prefixes:
            if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
                if service_id not in matched:
                    matched.append(service_id)
                break
    return matched


def upsert_pull_request(repo: dict, pr: dict, files: list[dict] | None = None, reviews: list[dict] | None = None) -> str:
    scope = repo_scope(repo)
    node_id = pull_request_id(repo["id"], pr["number"])
    _, graph = connect()
    graph.query(
        """
        MERGE (p:PullRequest {id: $id})
        SET p.name = $name, p.number = $number, p.title = $title, p.summary = $summary,
            p.state = $state, p.merged = $merged, p.url = $url, p.scope = $scope, p.updated_at = $updated
        WITH p
        MATCH (r:Repo {id: $repo})
        MERGE (p)-[:IN_REPO]->(r)
        """,
        {
            "id": node_id,
            "name": _short_name(repo, pr["number"]),
            "number": int(pr["number"]),
            "title": pr.get("title") or "",
            "summary": _summary(pr.get("body")),
            "state": pr.get("state") or "",
            "merged": bool(pr.get("merged") or pr.get("merged_at")),
            "url": pr.get("html_url") or "",
            "scope": scope,
            "updated": pr.get("updated_at") or "",
            "repo": str(repo["id"]),
        },
    )
    if pr.get("user"):
        author = upsert_github_user(pr["user"])
        graph.query(
            "MATCH (u:GitHubUser {id: $user}), (p:PullRequest {id: $pr}) MERGE (u)-[:AUTHORED]->(p)",
            {"user": author, "pr": node_id},
        )

    # Who is asked to review, kept in step with GitHub. A payload without the list leaves the edges alone.
    if pr.get("requested_reviewers") is not None:
        requested = [upsert_github_user(user) for user in pr["requested_reviewers"] if user.get("id")]
        graph.query(
            "MATCH (u:GitHubUser)-[r:REVIEW_REQUESTED]->(:PullRequest {id: $pr}) WHERE NOT u.id IN $ids DELETE r",
            {"pr": node_id, "ids": requested},
        )
        for user_id in requested:
            graph.query(
                "MATCH (u:GitHubUser {id: $user}), (p:PullRequest {id: $pr}) MERGE (u)-[:REVIEW_REQUESTED]->(p)",
                {"user": user_id, "pr": node_id},
            )

    if files is not None:
        services = services_for_paths([item["filename"] for item in files if item.get("filename")])
        graph.query("MATCH (:PullRequest {id: $pr})-[t:TOUCHES]->(:Service) DELETE t", {"pr": node_id})
        for service_id in services:
            graph.query(
                "MATCH (p:PullRequest {id: $pr}), (s:Service {id: $service}) MERGE (p)-[:TOUCHES]->(s)",
                {"pr": node_id, "service": service_id},
            )

    for number in sorted({int(found) for found in _CLOSING.findall(pr.get("body") or "")}):
        graph.query(
            "MATCH (p:PullRequest {id: $pr}), (t:Ticket {id: $ticket}) MERGE (p)-[:CLOSES]->(t)",
            {"pr": node_id, "ticket": issue_id(repo["id"], number)},
        )

    link_jira_tickets(node_id, pr)

    for review in reviews or []:
        upsert_review(repo, pr["number"], review)
    return node_id


def jira_keys(pr: dict, projects: set[str]) -> tuple[list[str], list[str]]:
    """The Jira keys a pull request names, as (referenced, closed). A key counts only when its project
    is one the Jira connector syncs, so a Linear id or a version string that looks like one does not."""
    prose = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
    branch = (pr.get("head") or {}).get("ref") or ""
    named = {*_JIRA_KEY.findall(prose), *(key.upper() for key in _JIRA_BRANCH_KEY.findall(branch))}
    named = {key for key in named if key.rsplit("-", 1)[0] in projects}
    closed = sorted({key for key in _JIRA_CLOSING.findall(pr.get("body") or "") if key in named})
    return sorted(named - set(closed)), closed


def link_jira_tickets(pull_request_id: str, pr: dict) -> None:
    """``REFERENCES`` (or ``CLOSES``, for "fixes PAY-12") the Jira tickets a pull request names, kept in step
    with its text. A ticket not synced yet links when it is (``connector_graph.link_references`` keeps the keys).

    Never in the way of GitHub ingest: a failure here is logged by the pull request's id and the
    error's type only (never its text, which may quote the description), and the pull request and
    its reviews are still written."""
    try:
        _, graph = connect()
        urls = graph.query("MATCH (c:Container {source: 'jira'}) RETURN c.url").result_set
        projects = {match.group(1) for (url,) in urls if (match := _JIRA_PROJECT_URL.search(url or ""))}
        if not projects:
            return
        referenced, closed = jira_keys(pr, projects)
        connector_graph.link_references(pull_request_id, referenced, "jira", "REFERENCES")
        connector_graph.link_references(pull_request_id, closed, "jira", "CLOSES")
    except Exception as error:
        log.warning("[github] jira links failed pr=%s error=%s; the pull request is ingested without them", pull_request_id, type(error).__name__)


def upsert_review(repo: dict, pr_number: int, review: dict) -> None:
    if not review.get("user"):
        return
    reviewer = upsert_github_user(review["user"])
    _, graph = connect()
    graph.query(
        """
        MATCH (u:GitHubUser {id: $user}), (p:PullRequest {id: $pr})
        MERGE (u)-[r:REVIEWED]->(p)
        SET r.state = $state
        """,
        {"user": reviewer, "pr": pull_request_id(repo["id"], pr_number), "state": (review.get("state") or "").lower()},
    )


def upsert_issue(repo: dict, issue: dict) -> str | None:
    if "pull_request" in issue:
        return None  # the issues API also lists pull requests
    scope = repo_scope(repo)
    node_id = issue_id(repo["id"], issue["number"])
    _, graph = connect()
    graph.query(
        """
        MERGE (t:Ticket {id: $id})
        SET t.name = $name, t.number = $number, t.title = $title, t.summary = $summary,
            t.state = $state, t.url = $url, t.scope = $scope, t.source = 'github', t.updated_at = $updated
        WITH t
        MATCH (r:Repo {id: $repo})
        MERGE (t)-[:IN_REPO]->(r)
        """,
        {
            "id": node_id,
            "name": _short_name(repo, issue["number"]),
            "number": int(issue["number"]),
            "title": issue.get("title") or "",
            "summary": _summary(issue.get("body")),
            "state": issue.get("state") or "",
            "url": issue.get("html_url") or "",
            "scope": scope,
            "updated": issue.get("updated_at") or "",
            "repo": str(repo["id"]),
        },
    )
    if issue.get("user"):
        opener = upsert_github_user(issue["user"])
        graph.query("MATCH (u:GitHubUser {id: $user}), (t:Ticket {id: $t}) MERGE (u)-[:OPENED]->(t)", {"user": opener, "t": node_id})

    assignees = [upsert_github_user(user) for user in issue.get("assignees") or [] if user.get("id")]
    graph.query(
        "MATCH (u:GitHubUser)-[a:ASSIGNED]->(:Ticket {id: $t}) WHERE NOT u.id IN $ids DELETE a",
        {"t": node_id, "ids": assignees},
    )
    for user_id in assignees:
        graph.query("MATCH (u:GitHubUser {id: $user}), (t:Ticket {id: $t}) MERGE (u)-[:ASSIGNED]->(t)", {"user": user_id, "t": node_id})

    service = ask_service.match_service(f"{issue.get('title') or ''} {issue.get('body') or ''}")
    graph.query("MATCH (:Ticket {id: $t})-[a:ABOUT]->(:Service) DELETE a", {"t": node_id})
    if service:
        graph.query("MATCH (t:Ticket {id: $t}), (s:Service {id: $s}) MERGE (t)-[:ABOUT]->(s)", {"t": node_id, "s": service["id"]})
    return node_id


def delete_issue(repo: dict, number: int) -> None:
    _, graph = connect()
    graph.query("MATCH (t:Ticket {id: $id}) DETACH DELETE t", {"id": issue_id(repo["id"], number)})
