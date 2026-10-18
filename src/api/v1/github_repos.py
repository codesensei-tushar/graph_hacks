"""Public GitHub repos added by URL (``src/domain/github/public_repos.py``), for the operator and the console.

Operator (``BRAIN_ADMIN_TOKEN``; ``?org=<id>``, default the legacy org):

    GET    /ops/github/public-repos                 the org's public repos, with backfill progress
    POST   /ops/github/public-repos                 {repo, backfill?}: add one. ``repo`` is ``owner/name`` or its
                                                     GitHub URL; ``backfill`` is "all" or a number of pull
                                                     requests (default 50)
    POST   /ops/github/public-repos/{repo_id}/sync  {backfill?}: read it again from the start
    DELETE /ops/github/public-repos/{repo_id}       stop reading it; delete what it brought in

Console (the session's org; members list, owners and admins change; CSRF as every console mutation):

    GET    /console/api/github/public-repos
    POST   /console/api/github/public-repos
    POST   /console/api/github/public-repos/{repo_id}/sync
    DELETE /console/api/github/public-repos/{repo_id}

Every call runs in the org's context, so it reads and writes that org's graph only. A refusal is
``{error: <code>, message}`` with fixed text; GitHub's own text is never echoed.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from src.api.v1.slack_install import _admin_denial, _admin_org
from src.core import tenancy
from src.core.config import settings
from src.domain.console.session import console_admin, console_user
from src.domain.github import public_repos

log = logging.getLogger(__name__)

MESSAGES = {
    "bad_url": "That is not a GitHub repo. Give owner/name or https://github.com/<owner>/<repo>.",
    "not_found": "GitHub has no public repo there.",
    "not_public": "That repo is private. Connect it with the GitHub App instead, so only its collaborators read it.",
    "already_connected": "This company already reads that repo through the GitHub App.",
    "removing": "That repo is being removed. Add it again once that is done.",
    "unreachable": "GitHub could not be reached just now. Try again in a minute.",
    "bad_limit": "backfill is \"all\" or a number of pull requests up to 10000.",
}
_STATUS = {"bad_url": 400, "bad_limit": 400, "not_found": 404, "not_public": 409, "already_connected": 409, "removing": 409, "unreachable": 502}


def _refused(error: public_repos.PublicRepoError) -> JSONResponse:
    return JSONResponse({"error": error.code, "message": MESSAGES.get(error.code, "That did not work.")}, status_code=_STATUS.get(error.code, 400))


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _limit(body: dict):
    """The body's depth: ``backfill`` ("all" or a number; ``backfill_limit`` is the same, 0 = all).
    None when absent; anything else is passed on as -1, to be refused."""
    value = body.get("backfill", body.get("backfill_limit"))
    if value is None:
        return None
    if value == "all":
        return 0
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def _repo_of(body: dict):
    value = body.get("repo", body.get("url"))
    return value if isinstance(value, str) else None


# --- the shared work, in an org -------------------------------------------------------------------


def _list(org: tenancy.OrgRef) -> dict:
    with tenancy.org_context(org):
        return {"org": org.id, "repos": public_repos.list_for_org()}


def _add(org: tenancy.OrgRef, url: str, limit, actor: str):
    with tenancy.org_context(org):
        try:
            return public_repos.add(url, limit, actor=actor)
        except public_repos.PublicRepoError as error:
            return _refused(error)


def _sync(org: tenancy.OrgRef, repo_id: str, limit):
    with tenancy.org_context(org):
        try:
            return public_repos.resync(repo_id, limit)
        except public_repos.PublicRepoError as error:
            return _refused(error)


def _remove(org: tenancy.OrgRef, repo_id: str):
    with tenancy.org_context(org):
        if not public_repos.remove(repo_id):
            return JSONResponse({"error": "not_found", "message": "This company does not read that repo."}, status_code=404)
        return JSONResponse({"ok": True, "status": "removing"}, status_code=202)


def _audit(org: tenancy.OrgRef, actor: str, action: str, target: str) -> None:
    from src.domain.console import audit

    audit.record(org.id, actor, action, target, "ok")


# --- operator ---------------------------------------------------------------------------------

ops_router = APIRouter(tags=["github-repos"], include_in_schema=False)


def _ops_org(request: Request, org: str | None) -> tuple[tenancy.OrgRef | None, JSONResponse | None]:
    if denied := _admin_denial(request):
        return None, denied
    ref = _admin_org(org)
    if ref is None:
        return None, JSONResponse({"error": "no_org", "message": "No such org."}, status_code=404)
    return ref, None


@ops_router.get("/ops/github/public-repos")
async def ops_list(request: Request, org: str | None = None):
    ref, denied = _ops_org(request, org)
    if denied:
        return denied
    return await run_in_threadpool(_list, ref)


@ops_router.post("/ops/github/public-repos")
async def ops_add(request: Request, org: str | None = None):
    ref, denied = _ops_org(request, org)
    if denied:
        return denied
    body = await _body(request)
    url = _repo_of(body)
    if url is None:
        return _refused(public_repos.PublicRepoError("bad_url"))
    result = await run_in_threadpool(_add, ref, url, _limit(body), "operator")
    if not isinstance(result, JSONResponse):
        _audit(ref, "operator", "github.public_repo.add", result["full_name"])
    return result


@ops_router.post("/ops/github/public-repos/{repo_id}/sync")
async def ops_sync(repo_id: str, request: Request, org: str | None = None):
    ref, denied = _ops_org(request, org)
    if denied:
        return denied
    return await run_in_threadpool(_sync, ref, repo_id, _limit(await _body(request)))


@ops_router.delete("/ops/github/public-repos/{repo_id}")
async def ops_remove(repo_id: str, request: Request, org: str | None = None):
    ref, denied = _ops_org(request, org)
    if denied:
        return denied
    result = await run_in_threadpool(_remove, ref, repo_id)
    _audit(ref, "operator", "github.public_repo.remove", repo_id)
    return result


# --- console ----------------------------------------------------------------------------------


def _enabled() -> None:
    if not settings.console_enabled:
        raise HTTPException(status_code=404, detail="Not Found")


console_router = APIRouter(prefix="/console/api/github", tags=["console"], dependencies=[Depends(_enabled)])


def _session_org(session) -> tenancy.OrgRef:
    try:
        return tenancy.resolve(session.org)
    except (LookupError, TypeError) as error:
        raise HTTPException(status_code=403, detail="no_org") from error


@console_router.get("/public-repos")
async def console_list(session=Depends(console_user)):
    return await run_in_threadpool(_list, _session_org(session))


@console_router.post("/public-repos")
async def console_add(request: Request, session=Depends(console_admin)):
    org = _session_org(session)
    body = await _body(request)
    url = _repo_of(body)
    if url is None:
        return _refused(public_repos.PublicRepoError("bad_url"))
    result = await run_in_threadpool(_add, org, url, _limit(body), session.actor)
    if not isinstance(result, JSONResponse):
        _audit(org, session.actor, "github.public_repo.add", result["full_name"])
    return result


@console_router.post("/public-repos/{repo_id}/sync")
async def console_sync(repo_id: str, request: Request, session=Depends(console_admin)):
    return await run_in_threadpool(_sync, _session_org(session), repo_id, _limit(await _body(request)))


@console_router.delete("/public-repos/{repo_id}")
async def console_remove(repo_id: str, session=Depends(console_admin)):
    org = _session_org(session)
    result = await run_in_threadpool(_remove, org, repo_id)
    _audit(org, session.actor, "github.public_repo.remove", repo_id)
    return result
