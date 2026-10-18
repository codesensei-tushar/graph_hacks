"""Repositories the GitHub App can read, and who may read what came from them.

    (:Repo {id, full_name, private, html_url, installation_id, source, scope, bf_*})
                                                         source 'installation', or 'public' (added by URL,
                                                         no installation); bf_* is the backfill's progress
    (:GitHubUser {id, login})-[:CAN_READ]->(:Repo)      repo collaborators
    (:GitHubUser)-[:IS]->(:Person)                       set by `link github` or Person.github

A public repo's pull requests and issues go to ``org_shared``. A private repo gets its
own scope, ``github_repo_<id>``, readable only in a DM by someone whose linked GitHub
account is a collaborator. Channels never read it: not everyone in a channel has repo
access. With no confirmed link or no collaborator row, the answer is no (fails closed).
"""

import logging

from src.db.falkordb_client import connect
from src.domain.memory.writeback import SHARED_TEAM_BRAIN_CONTAINER_TAG

log = logging.getLogger(__name__)

GITHUB_REPO_SCOPE_PREFIX = "github_repo_"


def repo_scope(repo: dict) -> str:
    if repo.get("private") is False:
        return SHARED_TEAM_BRAIN_CONTAINER_TAG
    # Missing visibility counts as private.
    return f"{GITHUB_REPO_SCOPE_PREFIX}{repo['id']}"


def upsert_repo(repo: dict, installation_id: int | None) -> str:
    """``installation_id`` None: a public repo added by URL (``public_repos``), read with no installation.
    A repo whose visibility changed has every node that came from it moved to its new scope."""
    scope = repo_scope(repo)
    _, graph = connect()
    rows = graph.query(
        """
        MERGE (r:Repo {id: $id})
        WITH r, r.scope AS before
        SET r.name = $full_name, r.full_name = $full_name, r.private = $private,
            r.html_url = $url, r.installation_id = $installation, r.scope = $scope,
            r.source = CASE WHEN $installation IS NULL THEN 'public' ELSE 'installation' END
        RETURN before
        """,
        {
            "id": str(repo["id"]),
            "full_name": repo["full_name"],
            "private": repo.get("private") is not False,
            "url": repo.get("html_url") or "",
            "installation": int(installation_id) if installation_id is not None else None,
            "scope": scope,
        },
    ).result_set
    if rows and rows[0][0] is not None and rows[0][0] != scope:
        rescope(repo["id"], scope)
    return scope


def rescope(repo_id: str | int, scope: str) -> int:
    """Move what came from a repo to ``scope`` (it went public or private)."""
    _, graph = connect()
    rows = graph.query("MATCH (n)-[:IN_REPO]->(:Repo {id: $id}) SET n.scope = $scope RETURN count(n)", {"id": str(repo_id), "scope": scope}).result_set
    log.info("[github] repo=%s moved to scope=%s nodes=%d", repo_id, scope, rows[0][0] if rows else 0)
    return int(rows[0][0]) if rows else 0


# --- backfill progress, kept on the Repo node ---------------------------------------------------

BACKFILL_FIELDS = ("bf_phase", "bf_page", "bf_issues", "bf_pulls", "bf_limit", "bf_error", "bf_failures", "bf_started_at", "bf_done_at", "bf_poll_since")


def get_repo(repo_id: str | int) -> dict | None:
    _, graph = connect()
    fields = ("id", "full_name", "private", "installation_id", "source", "html_url", *BACKFILL_FIELDS)
    rows = graph.query(f"MATCH (r:Repo {{id: $id}}) RETURN {', '.join('r.' + name for name in fields)}", {"id": str(repo_id)}).result_set
    return dict(zip(fields, rows[0], strict=True)) if rows else None


def set_backfill(repo_id: str | int, **fields) -> None:
    unknown = set(fields) - set(BACKFILL_FIELDS)
    if unknown:
        raise ValueError(f"not backfill fields: {sorted(unknown)}")
    _, graph = connect()
    assignments = ", ".join(f"r.{name} = ${name}" for name in fields)
    graph.query(f"MATCH (r:Repo {{id: $id}}) SET {assignments}", {"id": str(repo_id), **fields})


def counts(repo_ids: list[str]) -> dict[str, dict]:
    """Pull requests and issues in the graph, per repo."""
    if not repo_ids:
        return {}
    _, graph = connect()
    rows = graph.ro_query(
        """
        MATCH (r:Repo) WHERE r.id IN $ids
        OPTIONAL MATCH (p:PullRequest)-[:IN_REPO]->(r)
        WITH r, count(p) AS pulls
        OPTIONAL MATCH (t:Ticket)-[:IN_REPO]->(r)
        RETURN r.id, pulls, count(t)
        """,
        {"ids": [str(item) for item in repo_ids]},
    ).result_set
    return {row[0]: {"pull_requests": int(row[1]), "issues": int(row[2])} for row in rows}


def delete_repo_batch(repo_ids: list[str], limit: int = 500) -> int:
    """Delete up to ``limit`` nodes that came from these repos; once none are left, the repos too.
    Returns how many went, so a job can come back until it is 0."""
    _, graph = connect()
    ids = [str(item) for item in repo_ids]
    rows = graph.query(
        "MATCH (n)-[:IN_REPO]->(r:Repo) WHERE r.id IN $ids WITH n LIMIT $limit DETACH DELETE n RETURN count(*)", {"ids": ids, "limit": limit}
    ).result_set
    deleted = int(rows[0][0]) if rows else 0
    if deleted < limit:
        graph.query("MATCH (r:Repo) WHERE r.id IN $ids DETACH DELETE r", {"ids": ids})
    return deleted


def delete_orphan_users(limit: int = 500) -> int:
    """GitHub accounts left with nothing: no pull request, review, issue or repo, and not linked to a
    person (a `link github` stays, so it still works when the data comes back)."""
    _, graph = connect()
    rows = graph.query(
        """
        MATCH (u:GitHubUser)
        WHERE NOT (u)-[:AUTHORED|REVIEWED|REVIEW_REQUESTED|OPENED|ASSIGNED|CAN_READ|IS]->()
        WITH u LIMIT $limit DETACH DELETE u RETURN count(*)
        """,
        {"limit": limit},
    ).result_set
    return int(rows[0][0]) if rows else 0


def remove_repo(repo_id: str | int) -> None:
    """The app lost access: drop the repo and everything that came from it."""
    _, graph = connect()
    graph.query("MATCH (n)-[:IN_REPO]->(:Repo {id: $id}) DETACH DELETE n", {"id": str(repo_id)})
    graph.query("MATCH (r:Repo {id: $id}) DETACH DELETE r", {"id": str(repo_id)})


def list_repos(installation_id: int | None = None) -> list[dict]:
    _, graph = connect()
    rows = graph.query(
        """
        MATCH (r:Repo)
        WHERE $installation IS NULL OR r.installation_id = $installation
        RETURN r.id, r.full_name, r.private, r.installation_id
        """,
        {"installation": installation_id},
    ).result_set
    return [{"id": row[0], "full_name": row[1], "private": row[2], "installation_id": row[3]} for row in rows]


def set_collaborators(repo_id: str | int, users: list[dict]) -> int:
    """Replace the repo's CAN_READ edges with the current collaborator list."""
    from src.domain.github.ingest import upsert_github_user

    ids = [upsert_github_user(user) for user in users if user.get("id") and user.get("login")]
    _, graph = connect()
    graph.query(
        "MATCH (:GitHubUser)-[r:CAN_READ]->(:Repo {id: $repo}) WHERE NOT r.user IN $ids DELETE r",
        {"repo": str(repo_id), "ids": ids},
    )
    for user_id in ids:
        graph.query(
            "MATCH (u:GitHubUser {id: $user}), (r:Repo {id: $repo}) MERGE (u)-[c:CAN_READ]->(r) SET c.user = $user",
            {"user": user_id, "repo": str(repo_id)},
        )
    return len(ids)


def readable_github_scopes_for_slack_user(slack_user_id: str | None) -> list[str]:
    """Private-repo scopes this Slack user may read: linked GitHub account x collaborator."""
    if not (slack_user_id or "").strip():
        return []
    try:
        _, graph = connect()
        rows = graph.query(
            f"""
            MATCH (:SlackUser {{id: $user}})-[:IS]->(:Person)<-[:IS]-(:GitHubUser)-[:CAN_READ]->(r:Repo)
            WHERE r.scope STARTS WITH '{GITHUB_REPO_SCOPE_PREFIX}'
            RETURN DISTINCT r.scope
            """,
            {"user": slack_user_id},
        ).result_set
    except Exception:
        log.exception("[github] readable repo lookup failed user=%s", slack_user_id)
        return []
    return [row[0] for row in rows]


def repo_names(scopes: list[str]) -> dict[str, str]:
    """``github_repo_<id>`` -> ``owner/name`` for citing a scope back to the asker."""
    ids = [scope.removeprefix(GITHUB_REPO_SCOPE_PREFIX) for scope in scopes if scope.startswith(GITHUB_REPO_SCOPE_PREFIX)]
    if not ids:
        return {}
    _, graph = connect()
    rows = graph.query("MATCH (r:Repo) WHERE r.id IN $ids RETURN r.id, r.full_name", {"ids": ids}).result_set
    return {f"{GITHUB_REPO_SCOPE_PREFIX}{repo_id}": name for repo_id, name in rows}
