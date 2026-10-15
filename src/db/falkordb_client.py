from functools import lru_cache

from falkordb import FalkorDB

from src.core.config import settings


@lru_cache(maxsize=1)
def client() -> FalkorDB:
    return FalkorDB(host=settings.falkordb_host, port=settings.falkordb_port, password=settings.falkordb_password or None)


def graph_name() -> str:
    return settings.falkordb_graph


def connect():
    db = client()
    return db, db.select_graph(graph_name())


def kv():
    """The Redis connection under FalkorDB. Holds short-lived keys: event dedupe, approvals, bot threads."""
    return client().connection
