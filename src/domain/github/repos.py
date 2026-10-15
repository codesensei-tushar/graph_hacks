"""Repositories the GitHub App can read, and who may read what came from them.

    (:Repo {id, full_name, private, html_url, installation_id, scope})
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


def upsert_repo(repo: dict, installation_id: int) -> str:
    scope = repo_scope(repo)
    _, graph = connect()
    graph.query(
        """
        MERGE (r:Repo {id: $id})
        SET r.name = $full_name, r.full_name = $full_name, r.private = $private,
            r.html_url = $url, r.installation_id = $installation, r.scope = $scope
        """,
        {
            "id": str(repo["id"]),
            "full_name": repo["full_name"],
            "private": repo.get("private") is not False,
            "url": repo.get("html_url") or "",
            "installation": int(installation_id),
            "scope": scope,
        },
    )
    return scope


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
