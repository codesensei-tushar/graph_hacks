"""What each GitHub webhook does to the graph. It runs in the org the installation is bound to
(``installations.route``, called by the route before this), so it writes that org's graph only.

| Event                     | Actions handled                         | Effect                                         |
|---------------------------|-----------------------------------------|------------------------------------------------|
| installation              | created / deleted                       | queue the backfill / remove its data, forget it |
| installation              | suspend / unsuspend                     | mark the binding                               |
| installation_repositories | added / removed                         | queue their backfill / remove their data       |
| repository                | deleted / anything else                 | remove its data / upsert (a visibility change rescopes) |
| member                    | any                                     | read the repo's collaborators again            |
| pull_request              | any                                     | upsert, with changed files on change           |
| pull_request              | opened / reopened / ready_for_review    | then the Slack heads-up (alerts.py)            |
| pull_request_review       | submitted / edited / dismissed          | upsert the review                              |
| issues                    | deleted / anything else                 | delete / upsert the ticket                     |

Backfills and removals are scheduler jobs (``connections``, ``backfill``). Anything else is
acknowledged and ignored.
"""

import logging

from src.domain.github import alerts, backfill, connections, ingest, installations, repos
from src.integrations.github.client import client_for

log = logging.getLogger(__name__)

# Actions that can change which files a pull request touches.
_FILE_ACTIONS = {"opened", "reopened", "synchronize", "edited", "ready_for_review"}
# Actions after which a pull request may need to reach people who weren't on it.
_ALERT_ACTIONS = {"opened", "reopened", "ready_for_review"}


def handle_github_event(event: str, payload: dict) -> str:
    action = payload.get("action") or ""
    installation_id = (payload.get("installation") or {}).get("id")
    repo = payload.get("repository")

    if event == "installation":
        if action == "created":
            connections.start_backfill(int(installation_id))
            return "backfill_queued"
        if action == "deleted":
            connections.disconnect(int(installation_id), forget=True)
            return "removal_queued"
        if action in ("suspend", "unsuspend"):
            installations.set_suspended(int(installation_id), action == "suspend")
            return action
        return "ignored"

    if event == "installation_repositories":
        if action == "added":
            # The webhook's repo stubs lack visibility details; the job reads the full objects.
            added = [item["id"] for item in payload.get("repositories_added") or []]
            if added:
                connections.start_backfill(int(installation_id), only=added)
            return "backfill_queued"
        if action == "removed":
            removed = [item["id"] for item in payload.get("repositories_removed") or []]
            if removed:
                backfill.start_removal(removed, orphans=False)
            return "removal_queued"
        return "ignored"

    if not repo or installation_id is None:
        return "ignored"

    if event == "repository":
        if action == "deleted":
            backfill.start_removal([repo["id"]], orphans=False)
            return "removal_queued"
        # Renamed, transferred, archived, made public or private: a visibility change moves its data's scope.
        repos.upsert_repo(repo, int(installation_id))
        return "repository"

    repos.upsert_repo(repo, int(installation_id))

    if event == "member":
        backfill.sync_collaborators(client_for(int(installation_id)), repo)
        return "collaborators"

    if event == "pull_request":
        pr = payload["pull_request"]
        files = None
        if action in _FILE_ACTIONS:
            files = client_for(int(installation_id)).list_pull_request_files(repo["full_name"], pr["number"])
        ingest.upsert_pull_request(repo, pr, files=files)
        if action in _ALERT_ACTIONS:
            _alert(repo, pr)
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


def _alert(repo: dict, pr: dict) -> None:
    """The Slack heads-up, after ingest and never in its way: a failure is logged and the event still counts as handled."""
    try:
        outcome = alerts.alert_pull_request(repo, pr)
        log.info("[github] coordination alert repo=%s pr=%s outcome=%s", repo.get("full_name"), pr.get("number"), outcome)
    except Exception:
        log.exception("[github] coordination alert failed repo=%s pr=%s", repo.get("full_name"), pr.get("number"))
