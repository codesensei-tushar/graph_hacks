"""The FalkorDB connection, and a switch that makes every graph read return nothing.

``dark_graph()`` is for the proof route: the same questions run twice, once against
FalkorDB and once with every read returning an empty result set, so the report can show
which capabilities collapse without the graph. While it is set, ``connect()`` returns a
proxy graph instead of a real one and never touches the database.
"""

import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache

from falkordb import FalkorDB

from src.core.config import settings

log = logging.getLogger(__name__)

# True while the proof runs its "without the graph" pass. Context-local, so a proof
# request never darkens another request's reads.
graph_reads_disabled: ContextVar[bool] = ContextVar("graph_reads_disabled", default=False)

_WRITE = re.compile(r"\b(?:CREATE|MERGE|SET|DELETE|REMOVE)\b", re.IGNORECASE)


@lru_cache(maxsize=1)
def client() -> FalkorDB:
    return FalkorDB(host=settings.falkordb_host, port=settings.falkordb_port, password=settings.falkordb_password or None)


def graph_name() -> str:
    return settings.falkordb_graph


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
    return db, db.select_graph(graph_name())


def kv():
    """The Redis connection under FalkorDB. Holds short-lived keys: event dedupe, approvals, bot threads."""
    return client().connection
