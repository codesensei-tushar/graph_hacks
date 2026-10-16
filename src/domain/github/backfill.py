"""Catch the graph up with a repo: on install, when repos are added, and on a timer.

A fresh install only gets webhooks for what happens next, so the recent pull requests
and issues are read once through the API. Collaborators are refreshed every hour,
because they decide who may read a private repo's data.
"""

import logging

from src.core import scheduler
from src.domain.github import ingest, repos
from src.integrations.github.client import GitHubClient, GitHubError, client_for

log = logging.getLogger(__name__)

PULL_REQUEST_LIMIT = 50
ISSUE_LIMIT = 50
COLLABORATOR_REFRESH = "runGitHubCollaboratorRefresh"
COLLABORATOR_REFRESH_JOB_ID = "github:collaborator-refresh"
COLLABORATOR_REFRESH_SECONDS = 60 * 60


def sync_collaborators(client: GitHubClient, repo: dict) -> None:
    try:
        users = client.list_collaborators(repo["full_name"])
    except GitHubError as error:
        # Fail closed: without a fresh list, nobody keeps access to a private repo.
        log.warning("[github] collaborators unavailable repo=%s status=%s; clearing access", repo["full_name"], error.status)
        users = []
    count = repos.set_collaborators(repo["id"], users)
    log.info("[github] collaborators repo=%s count=%d", repo["full_name"], count)


def backfill_repo(client: GitHubClient, repo: dict) -> dict:
    repos.upsert_repo(repo, client.installation_id)
    sync_collaborators(client, repo)
    issues = 0
    # Issues first, so "closes #12" in a pull request finds its ticket.
    for issue in client.list_issues(repo["full_name"], ISSUE_LIMIT):
        if ingest.upsert_issue(repo, issue):
            issues += 1
    pulls = 0
    for pr in client.list_pull_requests(repo["full_name"], PULL_REQUEST_LIMIT):
        files = client.list_pull_request_files(repo["full_name"], pr["number"])
        reviews = client.list_pull_request_reviews(repo["full_name"], pr["number"])
        ingest.upsert_pull_request(repo, pr, files=files, reviews=reviews)
        pulls += 1
    log.info("[github] backfilled repo=%s pull_requests=%d issues=%d", repo["full_name"], pulls, issues)
    return {"repo": repo["full_name"], "pull_requests": pulls, "issues": issues}


def backfill_installation(installation_id: int, only: list[dict] | None = None) -> list[dict]:
    """Backfill every repo the installation can see, or just ``only`` (from a webhook)."""
    client = client_for(installation_id)
    targets = only if only is not None else client.list_repositories()
    results = []
    for repo in targets:
        try:
            results.append(backfill_repo(client, repo))
        except Exception:
            log.exception("[github] backfill failed repo=%s", repo.get("full_name"))
    return results


def refresh_all_collaborators(_payload: dict | None = None) -> None:
    try:
        for row in repos.list_repos():
            if row["installation_id"] is None:
                continue
            sync_collaborators(client_for(int(row["installation_id"])), {"id": row["id"], "full_name": row["full_name"]})
    except Exception:
        log.exception("[github] collaborator refresh failed")
    finally:
        schedule_collaborator_refresh()


def schedule_collaborator_refresh() -> None:
    scheduler.schedule(COLLABORATOR_REFRESH_SECONDS, COLLABORATOR_REFRESH, job_id=COLLABORATOR_REFRESH_JOB_ID)


scheduler.register(COLLABORATOR_REFRESH, refresh_all_collaborators)
