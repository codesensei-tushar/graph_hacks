"""The FalkorDB connection, and a switch that makes every graph read return nothing.

``dark_graph()`` is for the proof route: the same questions run twice, once against
FalkorDB and once with every read returning an empty result set, so the report can show
which capabilities collapse without the graph. While it is set, ``connect()`` returns a
proxy graph instead of a real one and never touches the database.

Every real graph ``connect()`` hands out is wrapped in ``_GraphProxy``, the single point every
query passes through. It does two things and forwards everything else untouched:

- **Per-context timeout.** While ``query_timeout(ms)`` is active (``tool_guard`` sets it for
  the public and MCP surfaces), each ``query`` and ``ro_query`` that names no timeout of its
  own gets that one, so a public caller cannot hold a worker for FalkorDB's full ``TIMEOUT``.
  Outside it (Slack turns, sync, jobs) queries keep the server default.
- **Hooks.** ``BEFORE_QUERY`` and ``AFTER_QUERY`` are lists of callbacks, each called with the
  same ``QueryCall`` for every query: before it runs, and after it returns or raises (with
  ``duration_ms``, and ``result`` or ``error``, filled in). Register with ``add_query_hook``.
  A hook that raises is logged and skipped; it never fails or changes the query.

It also keeps each graph's **generation**: a counter in Redis that every ``query`` changing
something (nodes, edges, properties, labels) and every ``delete`` bumps. Caches of what a graph
holds (the answer cache) put ``generation(name)`` in their key, so a write anywhere, in any
process, makes them read again: a disconnected tool's data is not served from a cache after its
nodes are deleted. Within ``generation_snapshot()`` (one question) Redis is asked once per graph.
"""

import logging
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from falkordb import FalkorDB

from src.core.config import settings

log = logging.getLogger(__name__)

# True while the proof runs its "without the graph" pass. Context-local, so a proof
# request never darkens another request's reads.
graph_reads_disabled: ContextVar[bool] = ContextVar("graph_reads_disabled", default=False)

# The timeout, in milliseconds, for queries that name none of their own; None is the server's TIMEOUT.
query_timeout_ms: ContextVar[int | None] = ContextVar("query_timeout_ms", default=None)

_WRITE = re.compile(r"\b(?:CREATE|MERGE|SET|DELETE|REMOVE)\b", re.IGNORECASE)

GENERATION_KEY = "brain:graph_generation:{}"
# Generations already read in this context, by graph name; set by ``generation_snapshot``.
_generations: ContextVar[dict | None] = ContextVar("graph_generations", default=None)
# The statistics of a write that changed something.
_CHANGES = ("nodes_created", "nodes_deleted", "relationships_created", "relationships_deleted", "properties_set", "properties_removed", "labels_added", "labels_removed")


@contextmanager
def query_timeout(ms: int | None) -> Iterator[None]:
    """Within the block, a query that names no timeout gets ``ms``. Nested blocks keep the shorter one."""
    current = query_timeout_ms.get()
    token = query_timeout_ms.set(ms if current is None or (ms is not None and ms < current) else current)
    try:
        yield
    finally:
        query_timeout_ms.reset(token)


@dataclass
class QueryCall:
    """One query through ``_GraphProxy``, as the hooks see it. ``timeout`` is what was sent (None: the server's)."""

    graph: str
    method: str
    query: str
    params: Any
    timeout: int | None
    duration_ms: float | None = None
    result: Any = None
    error: BaseException | None = None


QueryHook = Callable[[QueryCall], None]
# Called before each query runs, and after it returns or raises. Append with add_query_hook.
BEFORE_QUERY: list[QueryHook] = []
AFTER_QUERY: list[QueryHook] = []


def add_query_hook(before: QueryHook | None = None, after: QueryHook | None = None) -> None:
    """Register callbacks for every query through ``connect()`` (metrics, logging). Never for dark graphs."""
    if before is not None:
        BEFORE_QUERY.append(before)
    if after is not None:
        AFTER_QUERY.append(after)


def remove_query_hook(hook: QueryHook) -> None:
    for hooks in (BEFORE_QUERY, AFTER_QUERY):
        while hook in hooks:
            hooks.remove(hook)


def _run_hooks(hooks: list[QueryHook], call: QueryCall) -> None:
    for hook in list(hooks):
        try:
            hook(call)
        except Exception:
            log.exception("[falkordb] query hook %r failed; the query is unaffected", hook)


class _GraphProxy:
    """A FalkorDB graph whose ``query`` and ``ro_query`` take the context's timeout and run the hooks.

    Every other attribute (``name``, ``delete``, ``explain``, ...) is the wrapped graph's own.
    """

    def __init__(self, graph) -> None:
        self._graph = graph

    def __getattr__(self, name: str):
        return getattr(self._graph, name)

    def query(self, q: str, params=None, timeout=None, **kwargs):
        return self._run("query", q, params, timeout, kwargs)

    def ro_query(self, q: str, params=None, timeout=None, **kwargs):
        return self._run("ro_query", q, params, timeout, kwargs)

    def delete(self):
        try:
            return self._graph.delete()
        finally:
            bump_generation(getattr(self._graph, "name", ""))

    def _run(self, method: str, q: str, params, timeout, kwargs):
        if timeout is None:
            timeout = query_timeout_ms.get()
        call = QueryCall(getattr(self._graph, "name", ""), method, q, params, timeout)
        _run_hooks(BEFORE_QUERY, call)
        started = time.perf_counter()
        try:
            call.result = getattr(self._graph, method)(q, params, timeout=timeout, **kwargs)
            if method == "query" and _changed(call.result):
                bump_generation(call.graph)
            return call.result
        except BaseException as error:
            call.error = error
            raise
        finally:
            call.duration_ms = (time.perf_counter() - started) * 1000
            _run_hooks(AFTER_QUERY, call)


@lru_cache(maxsize=1)
def client() -> FalkorDB:
    return FalkorDB(host=settings.falkordb_host, port=settings.falkordb_port, password=settings.falkordb_password or None)


def graph_name() -> str:
    """The graph of the org in context (src/core/tenancy.py); with none, the legacy org's,
    ``settings.falkordb_graph``. Under ``TENANCY_STRICT`` a missing org raises instead."""
    from src.core.tenancy import current_org_ref

    return current_org_ref().graph


class EmptyResult:
    """What a dark graph answers: the shape of a FalkorDB result with nothing in it."""

    result_set: list = []
    header: list = []
    nodes_created = relationships_created = properties_set = nodes_deleted = relationships_deleted = 0
    run_time_ms = 0.0


class DarkGraph:
    """A graph whose every query returns nothing, without a connection. Only reads are expected."""

    def __init__(self, name: str) -> None:
        self.name = name

    def ro_query(self, q: str, params=None, timeout=None) -> EmptyResult:
        return EmptyResult()

    def query(self, q: str, params=None, timeout=None, read_only: bool = False) -> EmptyResult:
        if _WRITE.search(q or ""):
            log.warning("[dark graph] a write was attempted while graph reads are disabled; nothing was written: %s", " ".join(q.split())[:120])
        return EmptyResult()


@contextmanager
def dark_graph():
    """Within the block, every ``connect()`` hands out a ``DarkGraph`` and no query reaches FalkorDB."""
    token = graph_reads_disabled.set(True)
    try:
        yield
    finally:
        graph_reads_disabled.reset(token)


def connect():
    if graph_reads_disabled.get():
        return None, DarkGraph(graph_name())
    db = client()
    return db, _GraphProxy(db.select_graph(graph_name()))


def kv():
    """The Redis connection under FalkorDB. Holds short-lived keys: event dedupe, approvals, bot threads."""
    return client().connection


def _changed(result) -> bool:
    try:
        return any(getattr(result, stat, 0) for stat in _CHANGES)
    except Exception:  # an unexpected result shape counts as a change: a cache may reload, never go stale
        return True


def generation(name: str) -> int | None:
    """How many changing writes graph ``name`` has had; None when Redis cannot say (cache nothing then)."""
    seen = _generations.get()
    if seen is not None and name in seen:
        return seen[name]
    try:
        value = int(kv().get(GENERATION_KEY.format(name)) or 0)
    except Exception as error:
        log.warning("[falkordb] generation of %s unreadable: %s", name, error)
        return None
    if seen is not None:
        seen[name] = value
    return value


def bump_generation(name: str) -> None:
    """Mark graph ``name`` changed: every cache keyed on its generation reads again."""
    seen = _generations.get()
    if seen is not None:
        seen.pop(name, None)
    try:
        kv().incr(GENERATION_KEY.format(name))
    except Exception as error:
        log.warning("[falkordb] generation of %s not bumped: %s", name, error)


@contextmanager
def generation_snapshot() -> Iterator[None]:
    """Within the block each graph's generation is read from Redis once, and again after a write."""
    token = _generations.set({})
    try:
        yield
    finally:
        _generations.reset(token)
