"""Public GitHub repos added by URL: read-only, with no installation of the app.

A public repo's pull requests, reviews and issues are anyone's to read, so an org (the operator for
any org, or an org's own owner or admin in the console) can add one by its URL. Its data goes to
``org_shared`` (``repos.repo_scope``): everyone in the org reads it, and for the legacy org that
includes the public ask page and MCP. No collaborators are read and no webhooks arrive: the
stepped backfill (``backfill.start_repo_backfill``) reads it, ``bf_limit`` pull requests and
issues deep (0: all of them), then polls it every 10 minutes. How it authenticates (or not) is
``client.PublicClient``'s business.

Which org reads which public repo is kept in the control graph:

    (:GitHubPublicRepo {key: '<org>:<repo id>', org, repo_id, full_name, status: 'active'|'removing',
                        added_by, created_at})-[:OF]->(:Org)

Several orgs may add one public repo; each gets its own copy in its own graph. A repo the org
already reads through an installation of the app is refused (``already_connected``), so one Repo
node never has two masters. Removing one deletes everything it brought in, 500 nodes a job, then
its record.
"""

import logging
import re
import time

from src.core import tenancy
from src.domain.github import backfill, repos
from src.integrations.github.client import GitHubError, public_client

log = logging.getLogger(__name__)

FINISHER = "public_repo_removed"
# github.com/<owner>/<repo>, with or without https://, www., .git or a trailing path; or owner/repo.
_URL = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:github\.com/)?(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<name>[A-Za-z0-9._-]{1,100}?)(?:\.git)?(?:[/?#].*)?$"
)


class PublicRepoError(ValueError):
    """A fixed reason: ``bad_url``, ``not_found``, ``not_public``, ``already_connected``, ``unreachable``, ``bad_limit``."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def parse(url: str) -> str:
    """``owner/name`` from a GitHub URL or ``owner/name``. Raises ``PublicRepoError('bad_url')``."""
    match = _URL.match((url or "").strip())
    if not match or match.group("name") in (".", ".."):
        raise PublicRepoError("bad_url")
    return f"{match.group('owner')}/{match.group('name')}"


def _key(org_id: str, repo_id) -> str:
    return f"{org_id}:{repo_id}"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _record(org_id: str, repo_id) -> dict | None:
    rows = tenancy.control_graph().query(
        "MATCH (p:GitHubPublicRepo {key: $key}) RETURN p.status, p.full_name, p.added_by, p.created_at", {"key": _key(org_id, repo_id)}
    ).result_set
    if not rows:
        return None
    status, full_name, added_by, created_at = rows[0]
    return {"status": status, "full_name": full_name, "added_by": added_by, "created_at": created_at}


def wanted(row: dict) -> bool:
    """For the backfill: the org in context still reads this public repo."""
    record = _record(tenancy.current_org_ref().id, row["id"])
    return bool(record and record["status"] == "active")


def add(url: str, limit: int | None = None, actor: str = "operator") -> dict:
    """Add a public repo to the org in context and start its backfill. Returns its status row."""
    full_name = parse(url)
    if limit is not None:
        try:
            limit = backfill.clamp_limit(limit)
        except ValueError:
            raise PublicRepoError("bad_limit") from None
    try:
        meta = public_client().get_repo(full_name)
    except GitHubError as error:
        if error.status == 404:
            raise PublicRepoError("not_found") from None
        log.warning("[github] public repo lookup failed repo=%s status=%s", full_name, error.status)
        raise PublicRepoError("unreachable") from None
    except Exception as error:
        log.warning("[github] public repo lookup failed repo=%s: %s", full_name, type(error).__name__)
        raise PublicRepoError("unreachable") from None
    if not meta.get("id"):
        raise PublicRepoError("not_found")
    if meta.get("private") is not False:
        raise PublicRepoError("not_public")
    org = tenancy.current_org_ref()
    repo_id = str(meta["id"])
    existing = repos.get_repo(repo_id)
    if existing is not None and existing.get("installation_id") is not None:
        raise PublicRepoError("already_connected")
    record = _record(org.id, repo_id)
    if record and record["status"] == "removing":
        raise PublicRepoError("removing")
    if org.legacy:
        from src.domain import orgs

        orgs.ensure_legacy_org()
    tenancy.control_graph().query(
        """
        MATCH (o:Org {id: $org})
        MERGE (p:GitHubPublicRepo {key: $key})
        ON CREATE SET p.org = $org, p.repo_id = $repo, p.added_by = $actor, p.created_at = $now
        SET p.full_name = $full_name, p.status = 'active'
        MERGE (p)-[:OF]->(o)
        """,
        {"org": org.id, "key": _key(org.id, repo_id), "repo": repo_id, "actor": actor, "now": _now_ms(), "full_name": meta.get("full_name") or full_name},
    )
    repos.upsert_repo(meta, None)
    backfill.start_repo_backfill(repo_id, limit=limit if limit is not None else (None if existing else backfill.DEFAULT_LIMIT))
    log.info("[github] public repo added org=%s repo=%s limit=%s", org.id, full_name, limit)
    return status(repo_id)


def list_for_org() -> list[dict]:
    org = tenancy.current_org_ref()
    rows = tenancy.control_graph().query(
        "MATCH (p:GitHubPublicRepo {org: $org}) RETURN p.repo_id ORDER BY p.created_at", {"org": org.id}
    ).result_set
    return [found for (repo_id,) in rows if (found := status(repo_id)) is not None]


def status(repo_id) -> dict | None:
    """What the ops page and the console show for one public repo of the org in context. Nothing secret."""
    org = tenancy.current_org_ref()
    record = _record(org.id, repo_id)
    if record is None:
        return None
    row = repos.get_repo(repo_id) or {}
    found = repos.counts([str(repo_id)]).get(str(repo_id), {"pull_requests": 0, "issues": 0})
    phase = row.get("bf_phase")
    return {
        "id": str(repo_id),
        "full_name": row.get("full_name") or record["full_name"],
        "url": row.get("html_url") or f"https://github.com/{record['full_name']}",
        "status": "removing" if record["status"] == "removing" else ("stopped" if phase == "stopped" else "ready" if phase == "done" else "backfilling"),
        "phase": phase,
        "backfill_limit": backfill.DEFAULT_LIMIT if row.get("bf_limit") is None else int(row["bf_limit"]),
        "pull_requests": found["pull_requests"],
        "issues": found["issues"],
        "last_error": row.get("bf_error"),
        "backfilled_at": row.get("bf_done_at"),
        "added_by": record["added_by"],
        "created_at": record["created_at"],
        "scope": "org_shared",
    }


def resync(repo_id, limit: int | None = None) -> dict:
    """Read the repo again from the start (a new depth, or new Service.paths for TOUCHES)."""
    record = _record(tenancy.current_org_ref().id, repo_id)
    if record is None:
        raise PublicRepoError("not_found")
    if record["status"] != "active":
        raise PublicRepoError("removing")
    if limit is not None:
        try:
            limit = backfill.clamp_limit(limit)
        except ValueError:
            raise PublicRepoError("bad_limit") from None
    backfill.start_repo_backfill(str(repo_id), limit=limit)
    return status(repo_id)


def remove(repo_id) -> bool:
    """Stop reading it and delete what it brought in (batched jobs), then forget it. False if unknown."""
    org = tenancy.current_org_ref()
    record = _record(org.id, repo_id)
    if record is None:
        return False
    tenancy.control_graph().query("MATCH (p:GitHubPublicRepo {key: $key}) SET p.status = 'removing'", {"key": _key(org.id, repo_id)})
    backfill.start_removal([str(repo_id)], finisher=FINISHER, context={"key": _key(org.id, repo_id)})
    log.info("[github] public repo removal started org=%s repo=%s", org.id, record["full_name"])
    return True


def _forget(context: dict) -> None:
    tenancy.control_graph().query("MATCH (p:GitHubPublicRepo {key: $key, status: 'removing'}) DETACH DELETE p", {"key": context.get("key")})


backfill.register_source("public", wanted)
backfill.register_finisher(FINISHER, _forget)
