"""Catch the graph up with a repo: on install, when repos are added, for a public repo added by
URL, and on a timer.

A fresh install only gets webhooks for what happens next, so pull requests and issues are read
once through the API. Collaborators are refreshed every hour, because they decide who may read a
private repo's data.

**The stepped backfill** (``start_repo_backfill``) reads a repo one page per scheduler job, so a
repo with hundreds of pull requests never holds the shared scheduler thread for long, and a
restart resumes where it was. Its progress lives on the Repo node (``bf_*``, ``repos.py``):

    start  -> (installation: collaborators; public: re-read the repo, refuse it if it is private)
    issues -> ISSUES_PER_STEP a page, newest first, until ``bf_limit`` issues (0: all of them)
    pulls  -> PULLS_PER_STEP a page, newest first, each with its changed files and reviews,
              until ``bf_limit`` pull requests (0: all)
    done   -> a public repo (no webhooks) is then polled every POLL_SECONDS for what changed

Every write is an upsert, so a page read twice (a retry, a redelivered job) changes nothing: the
job is safe to run at least once. A rate limit (GitHub's ``Retry-After`` or
``X-RateLimit-Reset``) puts the same page off until then, at most an hour; any other failure
backs off 1, 5, 15, then 60 minutes. Jobs run in the repo's org (the scheduler carries it), and
each checks that the repo is still wanted there (``register_source``) before it reads anything.
"""

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

from src.core import scheduler
from src.domain.github import ingest, repos
from src.integrations.github.client import GitHubClient, GitHubError, client_for, public_client

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


def backfill_repo(client: GitHubClient, repo: dict, limit: int = PULL_REQUEST_LIMIT) -> dict:
    """The whole backfill of one repo in one call (the replay and the tests). Connected repos use
    the stepped backfill below."""
    repos.upsert_repo(repo, client.installation_id)
    sync_collaborators(client, repo)
    issues = 0
    # Issues first, so "closes #12" in a pull request finds its ticket.
    for issue in client.list_issues(repo["full_name"], limit or ISSUE_LIMIT):
        if ingest.upsert_issue(repo, issue):
            issues += 1
    pulls = 0
    for pr in client.list_pull_requests(repo["full_name"], limit or PULL_REQUEST_LIMIT):
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


# --- the stepped backfill -----------------------------------------------------------------------

REPO_STEP = "runGitHubRepoBackfill"
POLL_JOB = "runGitHubPublicPoll"
REMOVE_JOB = "runGitHubRepoRemoval"
DEFAULT_LIMIT = 50
MAX_LIMIT = 10000
ISSUES_PER_STEP = 100
PULLS_PER_STEP = 10  # each pull request costs two more calls (files, reviews)
STEP_DELAY_SECONDS = 1.0
MAX_WAIT_SECONDS = 60 * 60
FAILURE_BACKOFF_SECONDS = (60, 5 * 60, 15 * 60, 60 * 60)
POLL_SECONDS = 10 * 60
POLL_MAX_PAGES = 5
REMOVE_BATCH = 500

# Repo source ('public' | 'installation') -> whether the org in context still wants this repo row.
_sources: dict[str, Callable[[dict], bool]] = {}
# Name -> what to do once a removal job has deleted everything (forget the repo's record, say).
_finishers: dict[str, Callable[[dict], None]] = {}


def register_source(source: str, wanted: Callable[[dict], bool]) -> None:
    _sources[source] = wanted


def register_finisher(name: str, finish: Callable[[dict], None]) -> None:
    _finishers[name] = finish


def repo_job_id(repo_id) -> str:
    return f"github:backfill:repo:{repo_id}"


def poll_job_id(repo_id) -> str:
    return f"github:poll:repo:{repo_id}"


def clamp_limit(value) -> int:
    """A backfill depth: 0 is everything; otherwise 1..MAX_LIMIT. Raises ``ValueError``."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_LIMIT:
        raise ValueError(f"backfill_limit is 0 (everything) or 1..{MAX_LIMIT}")
    return value


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def start_repo_backfill(repo_id, limit: int | None = None, delay: float = 0.0) -> None:
    """(Re)start the stepped backfill of a repo already in the graph, in the org in context.
    ``limit`` None keeps the repo's depth (DEFAULT_LIMIT when it has none)."""
    fields = {"bf_phase": "start", "bf_page": 1, "bf_issues": 0, "bf_pulls": 0, "bf_error": None, "bf_failures": 0, "bf_started_at": _now_ms(), "bf_done_at": None}
    if limit is not None:
        fields["bf_limit"] = clamp_limit(limit)
    repos.set_backfill(repo_id, **fields)
    scheduler.cancel(poll_job_id(repo_id))
    scheduler.schedule(delay, REPO_STEP, {"repo": str(repo_id)}, job_id=repo_job_id(repo_id))


def stop_repo_jobs(repo_id) -> None:
    scheduler.cancel(repo_job_id(repo_id))
    scheduler.cancel(poll_job_id(repo_id))


def _wanted(row: dict) -> bool:
    check = _sources.get(row.get("source") or "installation")
    return True if check is None else bool(check(row))


def _client(row: dict) -> GitHubClient:
    return public_client() if row.get("installation_id") is None else client_for(int(row["installation_id"]))


def _repo(row: dict) -> dict:
    return {"id": row["id"], "full_name": row["full_name"], "private": row.get("private") is not False, "html_url": row.get("html_url") or ""}


def _limit(row: dict) -> int:
    return DEFAULT_LIMIT if row.get("bf_limit") is None else int(row["bf_limit"])


def run_repo_step(payload: dict) -> None:
    """The scheduler job: one page of one repo, then the next step, until it is done."""
    repo_id = str((payload or {}).get("repo") or "")
    row = repos.get_repo(repo_id) if repo_id else None
    if row is None or row.get("bf_phase") in (None, "done", "stopped"):
        return
    if not _wanted(row):
        log.info("[github] backfill of repo=%s dropped: no longer connected", repo_id)
        return
    try:
        more = _step(row)
    except GitHubError as error:
        if error.retry_after:
            wait = min(float(error.retry_after), MAX_WAIT_SECONDS)
            repos.set_backfill(repo_id, bf_error=f"Waiting for GitHub's rate limit ({int(wait)} s)")
            log.info("[github] backfill repo=%s rate limited; next try in %ds", row["full_name"], wait)
            scheduler.schedule(wait, REPO_STEP, {"repo": repo_id}, job_id=repo_job_id(repo_id))
            return
        _failed(row, f"GitHub answered {error.status}")
        return
    except Exception as error:
        log.exception("[github] backfill step failed repo=%s", row["full_name"])
        _failed(row, type(error).__name__)
        return
    if more:
        scheduler.schedule(STEP_DELAY_SECONDS, REPO_STEP, {"repo": repo_id}, job_id=repo_job_id(repo_id))
    elif row.get("installation_id") is None:
        scheduler.schedule(POLL_SECONDS, POLL_JOB, {"repo": repo_id}, job_id=poll_job_id(repo_id))


def _failed(row: dict, reason: str) -> None:
    failures = int(row.get("bf_failures") or 0) + 1
    wait = FAILURE_BACKOFF_SECONDS[min(failures, len(FAILURE_BACKOFF_SECONDS)) - 1]
    repos.set_backfill(row["id"], bf_error=f"{reason}; trying again in {wait // 60} min", bf_failures=failures)
    scheduler.schedule(wait, REPO_STEP, {"repo": row["id"]}, job_id=repo_job_id(row["id"]))


_NOT_PUBLIC = "This repo is not public any more. Remove it, or connect it with the GitHub App."


def _step(row: dict) -> bool:
    """One page. Returns whether there is more to read."""
    client, repo, limit = _client(row), _repo(row), _limit(row)
    phase, page = row.get("bf_phase"), int(row.get("bf_page") or 1)
    if phase == "start":
        if row.get("installation_id") is None:
            meta = client.get_repo(row["full_name"])
            if meta.get("private") is not False or str(meta.get("id")) != str(row["id"]):
                # Gone private, or the name now points at another repo: nothing more is read.
                repos.set_backfill(row["id"], bf_phase="stopped", bf_error=_NOT_PUBLIC)
                return False
            repos.upsert_repo(meta, None)
        else:
            sync_collaborators(client, repo)
        repos.set_backfill(row["id"], bf_phase="issues", bf_page=1, bf_error=None, bf_failures=0)
        return True
    if phase == "issues":
        items, more = client.issues_page(repo["full_name"], page, ISSUES_PER_STEP)
        count = int(row.get("bf_issues") or 0)
        for issue in items:
            if limit and count >= limit:
                break
            if ingest.upsert_issue(repo, issue):
                count += 1
        if more and not (limit and count >= limit):
            repos.set_backfill(row["id"], bf_page=page + 1, bf_issues=count, bf_error=None, bf_failures=0)
        else:
            repos.set_backfill(row["id"], bf_phase="pulls", bf_page=1, bf_issues=count, bf_error=None, bf_failures=0)
        return True
    if phase == "pulls":
        items, more = client.pull_requests_page(repo["full_name"], page, PULLS_PER_STEP)
        count = int(row.get("bf_pulls") or 0)
        for pr in items:
            if limit and count >= limit:
                break
            _pull_request(client, repo, pr)
            count += 1
        if more and not (limit and count >= limit):
            repos.set_backfill(row["id"], bf_page=page + 1, bf_pulls=count, bf_error=None, bf_failures=0)
            return True
        done = _now_ms()
        started = int(row.get("bf_started_at") or done)
        repos.set_backfill(row["id"], bf_phase="done", bf_pulls=count, bf_done_at=done, bf_poll_since=_iso(started), bf_error=None, bf_failures=0)
        log.info("[github] backfilled repo=%s pull_requests=%d issues=%d", repo["full_name"], count, int(row.get("bf_issues") or 0))
        return False
    return False


def _pull_request(client: GitHubClient, repo: dict, pr: dict) -> None:
    files = client.list_pull_request_files(repo["full_name"], pr["number"])
    reviews = client.list_pull_request_reviews(repo["full_name"], pr["number"])
    ingest.upsert_pull_request(repo, pr, files=files, reviews=reviews)


def run_public_poll(payload: dict) -> None:
    """A public repo has no webhooks: every POLL_SECONDS, read what changed since the last poll."""
    repo_id = str((payload or {}).get("repo") or "")
    row = repos.get_repo(repo_id) if repo_id else None
    if row is None or row.get("installation_id") is not None or row.get("bf_phase") != "done" or not _wanted(row):
        return
    started = _now_ms()
    delay = POLL_SECONDS
    try:
        if _poll(row):
            repos.set_backfill(repo_id, bf_poll_since=_iso(started - 60_000), bf_error=None, bf_failures=0)
        else:
            return
    except GitHubError as error:
        delay = min(float(error.retry_after), MAX_WAIT_SECONDS) if error.retry_after else POLL_SECONDS
        repos.set_backfill(repo_id, bf_error=f"GitHub answered {error.status} to the last poll")
    except Exception as error:
        log.exception("[github] poll failed repo=%s", row.get("full_name"))
        repos.set_backfill(repo_id, bf_error=f"{type(error).__name__} in the last poll")
    scheduler.schedule(delay, POLL_JOB, {"repo": repo_id}, job_id=poll_job_id(repo_id))


def _poll(row: dict) -> bool:
    """What changed since ``bf_poll_since``. False when the repo is no longer public (polling stops)."""
    client, repo = _client(row), _repo(row)
    since = row.get("bf_poll_since") or _iso(_now_ms() - POLL_SECONDS * 1000)
    meta = client.get_repo(repo["full_name"])
    if meta.get("private") is not False or str(meta.get("id")) != str(row["id"]):
        repos.set_backfill(row["id"], bf_phase="stopped", bf_error=_NOT_PUBLIC)
        return False
    repos.upsert_repo(meta, None)
    for page in range(1, POLL_MAX_PAGES + 1):
        items, more = client.issues_page(repo["full_name"], page, ISSUES_PER_STEP, since=since)
        for issue in items:
            ingest.upsert_issue(repo, issue)
        if not more:
            break
    for page in range(1, POLL_MAX_PAGES + 1):
        items, more = client.pull_requests_page(repo["full_name"], page, PULLS_PER_STEP, sort="updated")
        fresh = [pr for pr in items if (pr.get("updated_at") or "") >= since]
        for pr in fresh:
            _pull_request(client, repo, pr)
        if not more or len(fresh) < len(items):
            break
    return True


# --- removal: batched, so a big repo never holds a request or the scheduler for long ---------


def _remove_job_id(ids: list[str]) -> str:
    return f"github:remove:{','.join(ids)[:200]}"


def start_removal(repo_ids: list, finisher: str | None = None, context: dict | None = None, orphans: bool = True) -> None:
    """Delete everything these repos brought in (org in context), REMOVE_BATCH nodes a job; then
    the repos, the GitHub accounts left with nothing (``orphans``), then ``finisher(context)``."""
    for repo_id in repo_ids:
        stop_repo_jobs(repo_id)
    ids = sorted(str(item) for item in repo_ids)
    payload = {"repos": ids, "finisher": finisher, "context": context or {}, "orphans": orphans}
    scheduler.schedule(0, REMOVE_JOB, payload, job_id=_remove_job_id(ids))


def run_removal(payload: dict) -> None:
    ids = [str(item) for item in (payload or {}).get("repos") or []]
    try:
        deleted = repos.delete_repo_batch(ids, REMOVE_BATCH) if ids else 0
        if deleted < REMOVE_BATCH and payload.get("orphans"):
            deleted += repos.delete_orphan_users(REMOVE_BATCH)
    except Exception:
        log.exception("[github] removal batch failed repos=%s; retrying in a minute", ids)
        scheduler.schedule(60, REMOVE_JOB, payload, job_id=_remove_job_id(ids))
        return
    if deleted >= REMOVE_BATCH:
        scheduler.schedule(0, REMOVE_JOB, payload, job_id=_remove_job_id(ids))
        return
    finish = _finishers.get(payload.get("finisher") or "")
    if finish is not None:
        try:
            finish(payload.get("context") or {})
        except Exception:
            log.exception("[github] removal finisher %s failed", payload.get("finisher"))
    log.info("[github] removed repos=%s", ids)


scheduler.register(COLLABORATOR_REFRESH, refresh_all_collaborators)
scheduler.register(REPO_STEP, run_repo_step)
scheduler.register(POLL_JOB, run_public_poll)
scheduler.register(REMOVE_JOB, run_removal)
