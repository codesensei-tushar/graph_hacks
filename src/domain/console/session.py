"""Console sessions, kept server side, and the FastAPI dependencies that read them.

The browser holds only ``__Host-brain_session``: an opaque 32-byte id, Secure,
HttpOnly, SameSite=Lax, Path=/. The record lives in kv under
``console:session:<sha256(id)>``, so a kv dump can't be replayed as cookies:
``{org, person, team, slack_user, role, csrf}`` plus timestamps.

- Idle expiry: the key's TTL is the idle timeout (12 h), renewed on each request.
- Absolute expiry: 7 days from sign-in. The TTL is never extended past that, and
  rotation keeps the original sign-in time.
- Rotation: a new id (and CSRF token) at sign-in and when the session gains an org.
- Role refresh: at most 60 s old. The role is re-read with ``roles.role_of`` in the
  session's org, and a person who has left the org is signed out.

``console_user`` and ``console_admin`` also accept ``Authorization: Bearer
<BRAIN_ADMIN_TOKEN>`` (with ``?org=``, default the legacy org): the operator, as owner.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from src.core.config import settings
from src.domain.access import roles
from src.core import tenancy
from src.domain import orgs
from src.domain.console import csrf
from src.domain.slack.workspace import get_org_actor_by_slack_identity

log = logging.getLogger(__name__)

COOKIE = "__Host-brain_session"
ROLE_REFRESH_SECONDS = 60


@dataclass(frozen=True)
class Session:
    org: str | None
    person: str | None
    person_name: str | None
    team: str
    slack_user: str
    role: str
    csrf: str
    created_at: float
    last_seen: float
    role_checked_at: float
    # sha256 of the session id (never the id itself): the kv key, and what OAuth state binds to.
    # Empty for the operator, who has no stored session.
    id_hash: str = ""
    operator: bool = False

    @property
    def actor(self) -> str:
        return "operator" if self.operator else f"slack:{self.team}:{self.slack_user}"

    @property
    def creator_id(self) -> str:
        """How ``Org.created_by`` names this person."""
        return f"{self.team}:{self.slack_user}"


_STORED = ("org", "person", "person_name", "team", "slack_user", "role", "csrf", "created_at", "last_seen", "role_checked_at")


def _hash(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()


def _key(id_hash: str) -> str:
    return f"console:session:{id_hash}"


def _kv():
    # Looked up at call time so tests can patch the kv connection in one place.
    from src.db.falkordb_client import kv

    return kv()


def _save(session: Session, *, new: bool = False) -> bool:
    """Writes the record. An update only lands while the record exists, so a request that
    loaded a session before a parallel logout or rotation can't bring it back."""
    now = time.time()
    remaining = session.created_at + settings.session_absolute_seconds - now
    ttl = int(min(settings.session_idle_seconds, remaining))
    if ttl <= 0:
        _kv().delete(_key(session.id_hash))
        return False
    payload = json.dumps({name: getattr(session, name) for name in _STORED})
    return bool(_kv().set(_key(session.id_hash), payload, ex=ttl, nx=new, xx=not new))


def create(
    *,
    org: str | None,
    person: str | None,
    person_name: str | None,
    team: str,
    slack_user: str,
    role: str,
    replacing: str | None = None,
    created_at: float | None = None,
) -> tuple[str, Session]:
    """A new session id and record. ``replacing`` (the old cookie) is destroyed first."""
    if replacing:
        destroy(replacing)
    sid = secrets.token_urlsafe(32)
    now = time.time()
    session = Session(
        org=org,
        person=person,
        person_name=person_name,
        team=team,
        slack_user=slack_user,
        role=role,
        csrf=csrf.new_token(),
        created_at=created_at or now,
        last_seen=now,
        role_checked_at=now,
        id_hash=_hash(sid),
    )
    _save(session, new=True)
    return sid, session


def rotate(sid: str, session: Session, **changes) -> tuple[str, Session]:
    """A new id and CSRF token for a session whose privileges change; the sign-in time stays."""
    fields = {name: getattr(session, name) for name in ("org", "person", "person_name", "team", "slack_user", "role")}
    fields.update(changes)
    return create(**fields, replacing=sid, created_at=session.created_at)


def destroy(sid: str) -> None:
    _kv().delete(_key(_hash(sid)))


def load(sid: str | None) -> Session | None:
    """The live session for a cookie value, or None. Expired records are deleted."""
    if not sid or len(sid) > 128:
        return None
    id_hash = _hash(sid)
    raw = _kv().get(_key(id_hash))
    if not raw:
        return None
    try:
        data = json.loads(raw)
        session = Session(**{name: data[name] for name in _STORED}, id_hash=id_hash)
    except (ValueError, KeyError, TypeError):
        _kv().delete(_key(id_hash))
        return None
    now = time.time()
    if now - session.created_at >= settings.session_absolute_seconds or now - session.last_seen >= settings.session_idle_seconds:
        _kv().delete(_key(id_hash))
        return None
    return session


def _current_role(session: Session, org: orgs.Org) -> str | None:
    """The role now, or None when the person is no longer in the org. Runs in the org's context."""
    if org.created_by and secrets.compare_digest(org.created_by, session.creator_id):
        return roles.ROLE_OWNER
    if session.person:
        resolution = get_org_actor_by_slack_identity(session.team, session.slack_user)
        if resolution.actor is None or resolution.actor.person_id != session.person:
            return None
    return roles.role_of(session.slack_user, env_roles=org.legacy)


def refresh(session: Session) -> Session | None:
    """Renews the idle timer, re-reading the role when it is over a minute old.

    None (and the record deleted) when the org is gone or the person has left it.
    """
    now = time.time()
    changes: dict = {"last_seen": now}
    if session.org and now - session.role_checked_at >= ROLE_REFRESH_SECONDS:
        org = orgs.get_org(session.org)
        role = None
        if org is not None:
            with tenancy.org_context(org.ref()):
                role = _current_role(session, org)
        if role is None:
            log.info("[console] session ended: org=%s person=%s is no longer a member", session.org, session.person)
            _kv().delete(_key(session.id_hash))
            return None
        changes.update(role=role, role_checked_at=now)
    updated = dataclasses.replace(session, **changes)
    return updated if _save(updated) else None


def set_cookie(response: Response, sid: str) -> None:
    response.set_cookie(
        COOKIE,
        sid,
        max_age=settings.session_absolute_seconds,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")


# --- FastAPI dependencies --------------------------------------------------------------


def require_console() -> None:
    """Router dependency: every console route is a 404 while CONSOLE_ENABLED is off."""
    if not settings.console_enabled:
        raise HTTPException(status_code=404, detail="Not Found")


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


def is_operator(request: Request) -> bool:
    presented = _bearer(request)
    return bool(settings.brain_admin_token and presented and secrets.compare_digest(presented, settings.brain_admin_token))


def _operator_session(request: Request) -> Session:
    org_id = request.query_params.get("org") or tenancy.LEGACY_ORG_ID
    if orgs.get_org(org_id) is None:
        raise HTTPException(status_code=404, detail="unknown_org")
    now = time.time()
    return Session(
        org=org_id, person=None, person_name="Operator", team="", slack_user="operator", role=roles.ROLE_OWNER,
        csrf="", created_at=now, last_seen=now, role_checked_at=now, operator=True,
    )


async def console_session(request: Request) -> Session:
    """A signed-in person, with or without an org yet (``/console/start``)."""
    require_console()
    if _bearer(request):
        if not is_operator(request):
            raise HTTPException(status_code=401, detail="signed_out")
        session = await run_in_threadpool(_operator_session, request)
    else:
        sid = request.cookies.get(COOKIE)
        loaded = await run_in_threadpool(load, sid)
        session = await run_in_threadpool(refresh, loaded) if loaded else None
        if session is None:
            raise HTTPException(status_code=401, detail="signed_out")
        # A cookie rides along on cross-site requests; a bearer token doesn't, so only cookies need CSRF checks.
        csrf.check(request, session.csrf)
    request.state.console_session = session
    if session.org:
        ref = await run_in_threadpool(orgs.get_org_ref, session.org)
        if ref is None:
            raise HTTPException(status_code=401, detail="signed_out")
        # Set here, in the request's own task, so the endpoint (and its threadpool copy of the
        # context) sees it. Not reset: the context ends with the request.
        tenancy.set_org(ref)
    return session


async def console_user(request: Request) -> Session:
    """A member of an org; the request runs in that org's context."""
    session = await console_session(request)
    if not session.org:
        raise HTTPException(status_code=403, detail="no_org")
    return session


async def console_admin(request: Request) -> Session:
    """An owner or admin of an org: connect, disconnect and sync."""
    session = await console_user(request)
    if not roles.role_at_least(session.role, roles.ROLE_ADMIN):
        raise HTTPException(status_code=403, detail="admin_only")
    return session
