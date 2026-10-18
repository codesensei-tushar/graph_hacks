"""The one place the console's auth code touches tenancy and the orgs API.

Package B owns tenancy (``src/core/tenancy.py``) and the control graph's orgs. Until
its contracts land, this module carries stand-ins with the spec's names and node
shapes, so auth can be built and tested now. When B's branch merges, the stand-ins
are replaced by imports and nothing else in ``src/domain/console`` changes.

Org identity: ``created_by`` is ``"<team_id>:<slack_user_id>"``, the Slack identity
of the person who created the org.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from src.db.falkordb_client import client

CONTROL_GRAPH = "brain_control"
LEGACY_ORG_ID = "quillfern"

# Stand-in for tenancy's ContextVar. B's graph_name() reads its own; see bind_org.
_current_org: ContextVar[str | None] = ContextVar("console_org_stub", default=None)


@dataclass(frozen=True)
class Org:
    id: str
    slug: str
    name: str
    graph: str
    legacy: bool = False
    created_by: str | None = None


def current_org() -> str | None:
    return _current_org.get()


@contextmanager
def org_context(org_id: str) -> Iterator[None]:
    token = _current_org.set(org_id)
    try:
        yield
    finally:
        _current_org.reset(token)


def bind_org(org_id: str) -> None:
    """Set the org for the rest of this request's context (a FastAPI dependency's job).

    Not reset: each request runs in its own copied context, so it ends with the request.
    """
    _current_org.set(org_id)


def control_graph():
    return client().select_graph(CONTROL_GRAPH)


_ORG_FIELDS = "o.id, o.slug, o.name, o.graph, coalesce(o.legacy, false), o.created_by"


def _org(row) -> Org:
    return Org(id=row[0], slug=row[1], name=row[2], graph=row[3], legacy=bool(row[4]), created_by=row[5])


def get_org(org_id: str) -> Org | None:
    rows = control_graph().query(f"MATCH (o:Org {{id: $id}}) RETURN {_ORG_FIELDS}", {"id": org_id}).result_set
    return _org(rows[0]) if rows else None


def org_for_team(team_id: str) -> Org | None:
    rows = control_graph().query(
        f"MATCH (:WorkspaceRoute {{team_id: $team}})-[:OF]->(o:Org) RETURN {_ORG_FIELDS}", {"team": team_id}
    ).result_set
    return _org(rows[0]) if rows else None


def org_created_by(created_by: str) -> Org | None:
    """An org this Slack identity created that no workspace routes to yet (its bot isn't installed)."""
    rows = control_graph().query(
        f"MATCH (o:Org {{created_by: $by}}) WHERE NOT ()-[:OF]->(o) RETURN {_ORG_FIELDS} ORDER BY o.created_at LIMIT 1",
        {"by": created_by},
    ).result_set
    return _org(rows[0]) if rows else None


def _slug(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")
    return "-".join(part for part in cleaned.split("-") if part)[:40] or "org"


def create_org(name: str, created_by: str) -> Org:
    org_id = f"org_{secrets.token_hex(10)}"
    slug = f"{_slug(name)}-{org_id[-4:]}"
    control_graph().query(
        "CREATE (:Org {id: $id, slug: $slug, name: $name, graph: $id, legacy: false, created_by: $by, created_at: $now})",
        {"id": org_id, "slug": slug, "name": name, "by": created_by, "now": int(time.time() * 1000)},
    )
    return Org(id=org_id, slug=slug, name=name, graph=org_id, created_by=created_by)
