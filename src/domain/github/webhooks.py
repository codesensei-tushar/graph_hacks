"""What each GitHub webhook does to the graph.

| Event                     | Actions handled                         | Effect                               |
|---------------------------|-----------------------------------------|--------------------------------------|
| installation              | created / deleted                       | backfill the repos / drop them       |
| installation_repositories | added / removed                         | backfill / drop those repos          |
| pull_request              | any                                     | upsert, with changed files on change |
| pull_request_review       | submitted / edited / dismissed          | upsert the review                    |
| issues                    | deleted / anything else                 | delete / upsert the ticket           |

Anything else is acknowledged and ignored.
"""

import logging

from src.domain.github import backfill, ingest, repos
from src.integrations.github.client import client_for

log = logging.getLogger(__name__)

# Actions that can change which files a pull request touches.
_FILE_ACTIONS = {"opened", "reopened", "synchronize", "edited", "ready_for_review"}


def handle_github_event(event: str, payload: dict) -> str:
    action = payload.get("action") or ""
    installation_id = (payload.get("installation") or {}).get("id")
    repo = payload.get("repository")

    if event == "installation":
        if action == "created":
            backfill.backfill_installation(int(installation_id))
            return "backfilled"
        if action == "deleted":
            for row in repos.list_repos(int(installation_id)):
                repos.remove_repo(row["id"])
            return "removed"
        return "ignored"

    if event == "installation_repositories":
        if action == "added":
            client = client_for(int(installation_id))
            added = {item["id"] for item in payload.get("repositories_added") or []}
            # The webhook's repo stubs lack visibility details; read the full objects.
            full = [repo for repo in client.list_repositories() if repo["id"] in added]
            backfill.backfill_installation(int(installation_id), only=full)
            return "backfilled"
        if action == "removed":
            for item in payload.get("repositories_removed") or []:
                repos.remove_repo(item["id"])
            return "removed"
        return "ignored"

    if not repo or installation_id is None:
        return "ignored"
    repos.upsert_repo(repo, int(installation_id))

    if event == "pull_request":
        pr = payload["pull_request"]
        files = None
        if action in _FILE_ACTIONS:
            files = client_for(int(installation_id)).list_pull_request_files(repo["full_name"], pr["number"])
        ingest.upsert_pull_request(repo, pr, files=files)
        return "pull_request"

    if event == "pull_request_review":
        if action in ("submitted", "edited", "dismissed"):
            ingest.upsert_pull_request(repo, payload["pull_request"])
            ingest.upsert_review(repo, payload["pull_request"]["number"], payload["review"])
            return "review"
        return "ignored"

    if event == "issues":
        if action == "deleted":
            ingest.delete_issue(repo, payload["issue"]["number"])
            return "deleted"
        ingest.upsert_issue(repo, payload["issue"])
        return "issue"

    return "ignored"
