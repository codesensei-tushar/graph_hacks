"""An installation's life in its org: backfill on connect, sync now, disconnect.

Everything here runs in the installation's org (``tenancy.org_context``), as the console route,
the operator route, a webhook or a scheduler job sets it. ``installations`` keeps the binding.

- ``start_backfill``: a scheduler job (``runGitHubInstallationBackfill``) lists the installation's
  repos, upserts them, drops repos it no longer sees, and starts each repo's stepped backfill
  (``backfill.start_repo_backfill``, ``backfill_limit`` deep). Safe to run more than once.
- ``disconnect``: the binding goes ``removing`` (its webhooks are dropped from then on), every repo
  it brought is deleted in batches (``backfill.start_removal``), then the account claim is released
  and the binding kept as a tombstone (a console disconnect: the app is still installed on GitHub)
  or forgotten (GitHub uninstalled it).
"""

import logging

from src.core import scheduler, tenancy
from src.domain.github import backfill, installations, repos
from src.integrations.github.client import GitHubError, client_for

log = logging.getLogger(__name__)

INSTALL_JOB = "runGitHubInstallationBackfill"
FINISHER = "installation_removed"


def install_job_id(installation_id, only: list | None = None) -> str:
    suffix = f":{','.join(sorted(str(item) for item in only))[:120]}" if only else ""
    return f"github:backfill:installation:{installation_id}{suffix}"


def _bound_here(installation_id) -> dict | None:
    current = installations.get(installation_id)
    if current is None or current["status"] != installations.ACTIVE or current["org"] != tenancy.current_org_ref().id:
        return None
    return current


def start_backfill(installation_id, only: list | None = None, delay: float = 0.0) -> None:
    """Queue the installation's backfill (``only``: just these repo ids, from ``installation_repositories``)."""
    payload = {"installation": int(installation_id), "only": [int(item) for item in only] if only else None}
    scheduler.schedule(delay, INSTALL_JOB, payload, job_id=install_job_id(installation_id, only))


def ensure_collaborator_refresh() -> None:
    if not scheduler.exists(backfill.COLLABORATOR_REFRESH_JOB_ID):
        backfill.schedule_collaborator_refresh()


def run_install_backfill(payload: dict) -> None:
    installation_id = int((payload or {}).get("installation") or 0)
    only = payload.get("only")
    binding = _bound_here(installation_id) if installation_id else None
    if binding is None:
        log.info("[github] backfill of installation=%s dropped: not connected to this org", installation_id)
        return
    try:
        found = client_for(installation_id).list_repositories()
    except GitHubError as error:
        wait = min(float(error.retry_after), backfill.MAX_WAIT_SECONDS) if error.retry_after else 300.0
        log.warning("[github] repos of installation=%s unavailable (%s); again in %ds", installation_id, error.status, wait)
        start_backfill(installation_id, only, delay=wait)
        return
    limit = binding.get("backfill_limit")
    wanted = {int(item) for item in only} if only else None
    seen = set()
    for repo in found:
        seen.add(str(repo["id"]))
        if wanted is not None and int(repo["id"]) not in wanted:
            continue
        repos.upsert_repo(repo, installation_id)
        backfill.start_repo_backfill(repo["id"], limit=limit if limit is not None else backfill.DEFAULT_LIMIT)
    if wanted is None:
        stale = [row["id"] for row in repos.list_repos(installation_id) if str(row["id"]) not in seen]
        if stale:
            backfill.start_removal(stale, orphans=False)
    ensure_collaborator_refresh()
    log.info("[github] installation=%s backfill started for %d repos", installation_id, len(seen) if wanted is None else len(wanted & {int(i) for i in seen}))


def resync(installation_id, limit: int | None = None) -> None:
    """Sync now: optionally a new depth, then every repo read again from the start."""
    if limit is not None:
        installations.set_limit(installation_id, backfill.clamp_limit(limit))
    start_backfill(installation_id)


def disconnect(installation_id, forget: bool = False) -> None:
    installations.set_status(installation_id, installations.REMOVING)
    scheduler.cancel(install_job_id(installation_id))
    ids = [row["id"] for row in repos.list_repos(int(installation_id))]
    backfill.start_removal(ids, finisher=FINISHER, context={"installation": str(installation_id), "forget": forget})
    log.info("[github] installation=%s disconnect started org=%s repos=%d", installation_id, tenancy.current_org_ref().id, len(ids))


def _finish(context: dict) -> None:
    installations.finish_removal(context.get("installation"), bool(context.get("forget")))


def _wanted(row: dict) -> bool:
    """The backfill's check for an installation's repo: its installation still feeds this org. A repo
    from before installations were bound (the legacy org's env app) counts while it has no binding."""
    if row.get("installation_id") is None:
        return False
    current = installations.get(row["installation_id"])
    if current is None:
        return tenancy.is_legacy(tenancy.current_org_ref())
    return current["status"] == installations.ACTIVE and current["org"] == tenancy.current_org_ref().id


def status(binding: dict) -> dict:
    """What the console and the ops page show for one installation: nothing secret."""
    rows = repos.list_repos(int(binding["id"]))
    found = repos.counts([row["id"] for row in rows])
    listed = []
    for row in rows:
        state = repos.get_repo(row["id"]) or {}
        phase = state.get("bf_phase")
        listed.append({
            "id": row["id"], "full_name": row["full_name"], "private": bool(row["private"]),
            "status": "ready" if phase == "done" else ("stopped" if phase == "stopped" else "backfilling" if phase else "waiting"),
            "pull_requests": found.get(row["id"], {}).get("pull_requests", 0), "issues": found.get(row["id"], {}).get("issues", 0),
            "last_error": state.get("bf_error"), "backfilled_at": state.get("bf_done_at"),
        })
    account, kind = binding.get("account_login") or "", binding.get("account_type") or ""
    manage = (
        f"https://github.com/organizations/{account}/settings/installations/{binding['id']}" if kind == "Organization" and account
        else f"https://github.com/settings/installations/{binding['id']}"
    )
    return {
        "id": str(binding["id"]), "account": account or None, "account_type": kind or None,
        "status": binding["status"], "suspended": bool(binding.get("suspended")),
        "backfill_limit": backfill.DEFAULT_LIMIT if binding.get("backfill_limit") is None else int(binding["backfill_limit"]),
        "connected_by": binding.get("connected_by"), "created_at": binding.get("created_at"),
        "repos": listed, "manage_url": manage,
    }


def list_status(org_id: str) -> list[dict]:
    return [status(binding) for binding in installations.list_for_org(org_id)]


scheduler.register(INSTALL_JOB, run_install_backfill)
backfill.register_source("installation", _wanted)
backfill.register_finisher(FINISHER, _finish)
