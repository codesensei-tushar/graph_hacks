"""Every index a company graph needs, and the numbered migrations that bring an old graph up to date.

``ensure(graph)`` is the one call: startup runs it for every org's graph, org creation for a new
one, and a seed after it writes. It is idempotent and safe to run concurrently:

1. Each range index in ``INDEXES`` and each full-text index (``FULLTEXT``, and ``search.INDEXED``)
   is created if missing. A new entry here reaches every graph on the next start.
2. Each migration in ``MIGRATIONS`` newer than the graph's ``SchemaVersion`` runs once, in order,
   and the version is recorded after each, so a crash resumes where it stopped. A migration must
   be idempotent and batched (a large graph never takes one long write).

Range indexes are what make ``(n:Label {id: $id})`` an index seek instead of a label scan.
"""

import logging
import time
from collections.abc import Callable

from src.db.falkordb_client import DarkGraph, connect

log = logging.getLogger(__name__)

# (label, property) range indexes. Lookups by id for every label a query anchors on; the
# connector's install and email lookups; the memory layer's.
INDEXES: tuple[tuple[str, str], ...] = (
    # Knowledge graph
    ("Company", "id"),
    ("Person", "id"),
    ("Team", "id"),
    ("Service", "id"),
    ("Document", "id"),
    ("Ticket", "id"),
    ("Decision", "id"),
    ("PullRequest", "id"),
    ("GitHubUser", "id"),
    ("Repo", "id"),
    ("Chat", "id"),
    # Connectors
    ("Container", "id"),
    ("Container", "install"),
    ("ExternalUser", "id"),
    ("ExternalUser", "email"),
    # Accounts link to people on the lower-cased email, never on a toLower() scan.
    ("ExternalUser", "email_lc"),
    ("Person", "email_lc"),
    # what_changed reads a window of time: WHERE t.changed_at_ms >= $since.
    ("Ticket", "changed_at_ms"),
    ("Service", "changed_at_ms"),
    ("Document", "changed_at_ms"),
    ("ConnectorState", "id"),
    ("ConnectorInstall", "id"),
    # Memory layer
    ("Memory", "id"),
    ("Memory", "scope"),
    ("Memory", "custom_id"),
    ("Tag", "id"),
    ("Tag", "container"),
    ("Scope", "id"),
    ("SlackUser", "id"),
    ("Channel", "id"),
    # This module's own bookkeeping
    ("SchemaVersion", "id"),
)
# (label, property) full-text indexes besides the ones ``search`` reads (``search.INDEXED``).
FULLTEXT: tuple[tuple[str, str], ...] = (("Memory", "text"),)

# Labels whose nodes read a missing ``scope`` as org_shared (``resolve.SCOPED``); migration 1
# writes it down. Memories are left alone: a memory's scope is never defaulted.
SCOPED_LABELS = ("Company", "Person", "Team", "Service", "Document", "Ticket", "Decision", "PullRequest", "GitHubUser", "Repo", "Chat")
BATCH = 5000
VERSION_ID = "schema"


def _backfill_scope(graph) -> None:
    for label in SCOPED_LABELS:
        while True:
            done = graph.query(
                f"MATCH (n:{label}) WHERE n.scope IS NULL WITH n LIMIT {BATCH} SET n.scope = 'org_shared' RETURN count(n)"
            ).result_set[0][0]
            if done < BATCH:
                break


def _backfill_changed_at(graph) -> None:
    """``changed_at_ms`` from ``source_updated_at``, parsed as the connector writer parses it, for
    synced items written before the writer set it (what_changed reads a window on it). An item
    whose time cannot be read is marked -1 (never in a window, and not read again)."""
    from src.connectors.graph import changed_at_ms

    for label in ("Ticket", "Service", "Document"):
        while True:
            rows = graph.query(
                f"MATCH (n:{label}) WHERE n.changed_at_ms IS NULL AND n.source_updated_at IS NOT NULL "
                f"RETURN n.id, n.source_updated_at LIMIT {BATCH}"
            ).result_set
            if not rows:
                break
            stamps = [{"id": node_id, "at": changed_at_ms(str(updated)) or -1} for node_id, updated in rows]
            graph.query(f"UNWIND $rows AS row MATCH (n:{label} {{id: row.id}}) SET n.changed_at_ms = row.at", {"rows": stamps})
            if len(rows) < BATCH:
                break


def _range_indexes(graph) -> None:
    present = indexes(graph)
    for label, prop in INDEXES:
        if "RANGE" in present.get(label, {}).get(prop, set()):
            continue
        try:
            graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
        except Exception as error:  # a concurrent ensure() made it first
            if "already" not in str(error).lower():
                raise


# (version, what it does, the step). Append only; never renumber or edit one that shipped.
MIGRATIONS: tuple[tuple[int, str, Callable], ...] = (
    (1, "a missing scope on knowledge nodes is written as org_shared", _backfill_scope),
    (2, "range indexes on every anchored label", _range_indexes),
    (3, "synced items written before changed_at_ms get it from their source time", _backfill_changed_at),
)
LATEST = MIGRATIONS[-1][0]


def indexes(graph) -> dict[str, dict[str, set[str]]]:
    """The graph's indexes: label -> property -> kinds ("RANGE", "FULLTEXT", ...)."""
    rows = _read(graph, "CALL db.indexes() YIELD label, types RETURN label, types")
    out: dict[str, dict[str, set[str]]] = {}
    for label, types in rows:
        for prop, kinds in (types or {}).items():
            out.setdefault(label, {}).setdefault(prop, set()).update(kinds or [])
    return out


def _read(graph, query: str, params: dict | None = None) -> list:
    """A read that finds nothing in a graph not created yet (a new org's, before its first write)."""
    try:
        return graph.ro_query(query, params).result_set
    except Exception as error:
        if "empty key" in str(error).lower():
            return []
        raise


def version(graph) -> int:
    rows = _read(graph, "MATCH (v:SchemaVersion {id: $id}) RETURN v.version", {"id": VERSION_ID})
    return int(rows[0][0] or 0) if rows else 0


def ensure(graph=None, migrate: bool = True) -> int:
    """Bring ``graph`` (the org in context's when None) to the latest schema; returns its version.

    ``migrate=False`` (a seed) builds the indexes only: a seed writes data as it is, and the next
    start migrates it."""
    if graph is None:
        _, graph = connect()
    if isinstance(graph, DarkGraph):
        return 0
    _range_indexes(graph)
    _fulltext_indexes(graph)
    current = version(graph)
    if not migrate:
        return current
    for number, what, step in MIGRATIONS:
        if number <= current:
            continue
        started = time.monotonic()
        step(graph)
        graph.query(
            "MERGE (v:SchemaVersion {id: $id}) SET v.version = $version, v.migrated_at = timestamp()",
            {"id": VERSION_ID, "version": number},
        )
        log.info("[schema] %s: migration %d (%s) in %.1fs", getattr(graph, "name", "?"), number, what, time.monotonic() - started)
        current = number
    return current


def _fulltext_indexes(graph) -> None:
    from src.domain.knowledge.search import ensure_search_indexes

    present = indexes(graph)
    for label, prop in FULLTEXT:
        if "FULLTEXT" in present.get(label, {}).get(prop, set()):
            continue
        try:
            graph.query("CALL db.idx.fulltext.createNodeIndex($label, $field)", {"label": label, "field": prop})
        except Exception as error:
            if "already" not in str(error).lower():
                raise
    # Never raises: a failure is logged and the tools that would search keep their refusal.
    ensure_search_indexes(graph)
