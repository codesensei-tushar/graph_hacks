"""Orgs and what routes to them, in the control graph (``tenancy.control_graph()``).

    (:Org {id, slug, name, graph, legacy, created_by, created_at})
    (:WorkspaceRoute {team_id, created_at})-[:OF]->(:Org)       a Slack workspace's org
    (:ExternalAccount {key, created_at})-[:OF]->(:Org)          'jira:<cloudId>', 'hubspot:<hubId>'; one org each

Each org's own data lives in its graph (``org_<ulid>``); the legacy org (Quillfern) keeps
``settings.falkordb_graph``. A Slack team with no route belongs to the legacy org, which is
how every workspace installed before orgs keeps working. Invite codes and the audit log are
in the control graph too (package A's ``invites``/``audit``).
"""

import logging
import re
import threading
import time
from dataclasses import asdict, dataclass

from src.core import tenancy
from src.core.tenancy import LEGACY_ORG_ID, LEGACY_ORG_NAME, LEGACY_ORG_SLUG, OrgRef, control_graph

log = logging.getLogger(__name__)

ROUTE_CACHE_SECONDS = 60.0
_SLUG = re.compile(r"[^a-z0-9]+")
_FIELDS = ("id", "slug", "name", "graph", "legacy", "created_by", "created_at")


@dataclass(frozen=True)
class Org:
    id: str
    slug: str
    name: str
    graph: str
    legacy: bool = False
    created_by: str | None = None  # "<team_id>:<slack_user_id>"
    created_at: int | None = None

    def ref(self) -> OrgRef:
        if self.legacy:
            return tenancy.legacy_org()
        return OrgRef(id=self.id, graph=self.graph, slug=self.slug, name=self.name, legacy=False)

    def to_dict(self) -> dict:
        return asdict(self)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _org(row) -> Org:
    values = dict(zip(_FIELDS, row, strict=True))
    values["legacy"] = bool(values["legacy"])
    return Org(**values)


def _legacy_org() -> Org:
    ref = tenancy.legacy_org()
    return Org(id=LEGACY_ORG_ID, slug=LEGACY_ORG_SLUG, name=LEGACY_ORG_NAME, graph=ref.graph, legacy=True)


_RETURN = ", ".join(f"o.{name}" for name in _FIELDS)


def ensure_control_indexes() -> None:
    graph = control_graph()
    for label, prop in (("Org", "id"), ("Org", "slug"), ("WorkspaceRoute", "team_id"), ("ExternalAccount", "key")):
        try:
            graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
        except Exception as error:
            if "already" not in str(error).lower():
                raise


def get_org(org_id: str) -> Org | None:
    """The org, or None. The legacy org always exists, migrated or not; its graph is the configured one."""
    if org_id == LEGACY_ORG_ID:
        rows = control_graph().query(f"MATCH (o:Org {{id: $id}}) RETURN {_RETURN}", {"id": org_id}).result_set
        legacy = _legacy_org()
        if rows:
            stored = _org(rows[0])
            return Org(**{**stored.to_dict(), "graph": legacy.graph, "legacy": True})
        return legacy
    if not tenancy.ORG_ID.match(org_id or ""):
        return None
    rows = control_graph().query(f"MATCH (o:Org {{id: $id}}) RETURN {_RETURN}", {"id": org_id}).result_set
    return _org(rows[0]) if rows else None


_ref_cache: dict[str, tuple[float, OrgRef]] = {}


def get_org_ref(org_id: str) -> OrgRef | None:
    """The org's OrgRef, cached for a minute (the scheduler resolves one per job). An org's
    graph never changes, so a cached ref is only ever stale for a deleted org."""
    now = time.monotonic()
    cached = _ref_cache.get(org_id)
    if cached and now - cached[0] < ROUTE_CACHE_SECONDS:
        return cached[1]
    org = get_org(org_id)
    if org is None:
        _ref_cache.pop(org_id, None)
        return None
    _ref_cache[org_id] = (now, org.ref())
    return org.ref()


def list_orgs() -> list[Org]:
    rows = control_graph().query(f"MATCH (o:Org) RETURN {_RETURN} ORDER BY o.created_at").result_set
    return [_org(row) for row in rows]


def _slug_for(name: str) -> str:
    base = _SLUG.sub("-", (name or "").lower()).strip("-")[:40] or "org"
    taken = {row[0] for row in control_graph().query("MATCH (o:Org) WHERE o.slug STARTS WITH $base RETURN o.slug", {"base": base}).result_set}
    if base not in taken:
        return base
    index = 2
    while f"{base}-{index}" in taken:
        index += 1
    return f"{base}-{index}"


def build_org_indexes(org: OrgRef) -> None:
    """The indexes main.py builds at startup, in ``org``'s graph."""
    from src.connectors import graph as connector_graph
    from src.connectors import store as connector_store
    from src.domain.knowledge.search import ensure_search_indexes
    from src.domain.memory.memory_service import ensure_indexes

    with tenancy.org_context(org):
        ensure_indexes()
        connector_graph.ensure_indexes()
        connector_store.ensure_indexes()
        ensure_search_indexes()


def create_org(name: str, created_by: str | None) -> Org:
    """A new org with its own graph (``org_<ulid>``) and that graph's indexes. ``created_by`` is
    ``"<team_id>:<slack_user_id>"``; the caller has checked the invite code."""
    name = " ".join((name or "").split())[:80]
    if not name:
        raise ValueError("an org needs a name")
    ulid = tenancy.new_ulid()
    org = Org(id=ulid, slug=_slug_for(name), name=name, graph=f"org_{ulid}", legacy=False, created_by=created_by, created_at=_now_ms())
    build_org_indexes(org.ref())
    control_graph().query(
        """
        CREATE (o:Org {id: $id, slug: $slug, name: $name, graph: $graph, legacy: false,
                       created_by: $created_by, created_at: $created_at})
        """,
        {key: value for key, value in org.to_dict().items() if key != "legacy"},
    )
    log.info("[orgs] created org=%s graph=%s", org.id, org.graph)
    return org


def ensure_legacy_org() -> Org:
    """The Org node for the legacy org (the migration). Idempotent."""
    legacy = _legacy_org()
    control_graph().query(
        """
        MERGE (o:Org {id: $id})
        ON CREATE SET o.created_at = $now, o.created_by = null
        SET o.slug = $slug, o.name = coalesce(o.name, $name), o.graph = $graph, o.legacy = true
        """,
        {"id": legacy.id, "slug": legacy.slug, "name": legacy.name, "graph": legacy.graph, "now": _now_ms()},
    )
    return get_org(LEGACY_ORG_ID) or legacy


# --- Slack workspace routes ----------------------------------------------------------------

_route_cache: dict[str, tuple[float, str | None]] = {}
_route_lock = threading.Lock()


def route_team(team_id: str, org_id: str) -> None:
    """Route a Slack team to ``org_id``. A team routes to one org; a reinstall into another org moves it."""
    if not team_id:
        raise ValueError("team_id is required")
    if get_org(org_id) is None:
        raise LookupError(f"no such org: {org_id!r}")
    if org_id == LEGACY_ORG_ID:
        ensure_legacy_org()
    control_graph().query(
        """
        MATCH (o:Org {id: $org})
        MERGE (r:WorkspaceRoute {team_id: $team})
        ON CREATE SET r.created_at = $now
        WITH r, o
        OPTIONAL MATCH (r)-[old:OF]->(:Org)
        DELETE old
        WITH r, o
        MERGE (r)-[:OF]->(o)
        """,
        {"team": team_id, "org": org_id, "now": _now_ms()},
    )
    with _route_lock:
        _route_cache.pop(team_id, None)


def unroute_team(team_id: str) -> None:
    control_graph().query("MATCH (r:WorkspaceRoute {team_id: $team}) DETACH DELETE r", {"team": team_id})
    with _route_lock:
        _route_cache.pop(team_id, None)


def routed_org_id(team_id: str) -> str | None:
    """The org a team is routed to, or None (no route). Cached for a minute."""
    now = time.monotonic()
    with _route_lock:
        cached = _route_cache.get(team_id)
        if cached and now - cached[0] < ROUTE_CACHE_SECONDS:
            return cached[1]
    rows = control_graph().query("MATCH (:WorkspaceRoute {team_id: $team})-[:OF]->(o:Org) RETURN o.id", {"team": team_id}).result_set
    org_id = rows[0][0] if rows else None
    with _route_lock:
        _route_cache[team_id] = (now, org_id)
    return org_id


def org_for_team(team_id: str) -> Org | None:
    """The org a Slack team is routed to, or None when it has no route."""
    org_id = routed_org_id(team_id) if team_id else None
    return get_org(org_id) if org_id else None


class RouteUnavailable(RuntimeError):
    """The control graph could not be read, so which org a team belongs to is unknown."""


def org_ref_for_team(team_id: str | None) -> OrgRef:
    """Where a Slack event for ``team_id`` runs: its routed org, else (no route) the legacy org.
    Raises ``RouteUnavailable`` when the lookup fails: never guess, or another org's event would
    run in the legacy graph. Slack routes answer 503 then, so Slack retries."""
    try:
        org_id = routed_org_id(team_id) if team_id else None
        ref = get_org_ref(org_id) if org_id else None
    except Exception as error:
        log.exception("[orgs] route lookup failed team=%s", team_id)
        raise RouteUnavailable(str(team_id)) from error
    if org_id and ref is None:
        # Routed to an org that no longer exists: not the legacy org's either.
        raise RouteUnavailable(str(team_id))
    return ref or tenancy.legacy_org()


def legacy_migrated() -> bool:
    """Whether ``POST /ops/orgs/migrate`` has run: the legacy org's node exists, so every team it serves is routed."""
    rows = control_graph().query("MATCH (o:Org {id: $id}) RETURN count(o)", {"id": LEGACY_ORG_ID}).result_set
    return bool(rows and rows[0][0])


def teams_of(org_id: str) -> list[str]:
    rows = control_graph().query("MATCH (r:WorkspaceRoute)-[:OF]->(:Org {id: $org}) RETURN r.team_id ORDER BY r.team_id", {"org": org_id}).result_set
    return [row[0] for row in rows]


def clear_route_cache() -> None:
    with _route_lock:
        _route_cache.clear()
    _ref_cache.clear()


# --- external accounts ---------------------------------------------------------------------


def external_account_org(key: str) -> str | None:
    rows = control_graph().query("MATCH (:ExternalAccount {key: $key})-[:OF]->(o:Org) RETURN o.id", {"key": key}).result_set
    return rows[0][0] if rows else None


def claim_external_account(key: str, org_id: str) -> bool:
    """Claim ``key`` ('jira:<cloudId>', 'hubspot:<hubId>') for ``org_id``. False when another org holds it;
    True when it is now (or already was) this org's. One account feeds one org."""
    if not key or ":" not in key:
        raise ValueError("an external account key is '<source>:<id>'")
    if org_id == LEGACY_ORG_ID:
        ensure_legacy_org()
    graph = control_graph()
    graph.query(
        """
        MATCH (o:Org {id: $org})
        MERGE (a:ExternalAccount {key: $key})
        ON CREATE SET a.created_at = $now, a.org = $org
        WITH a, o
        WHERE a.org = $org
        MERGE (a)-[:OF]->(o)
        """,
        {"key": key, "org": org_id, "now": _now_ms()},
    )
    return external_account_org(key) == org_id


def release_external_account(key: str, org_id: str) -> None:
    control_graph().query("MATCH (a:ExternalAccount {key: $key, org: $org}) DETACH DELETE a", {"key": key, "org": org_id})
