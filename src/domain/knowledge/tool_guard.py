"""A graph query that runs past FalkorDB's timeout, said as a sentence instead of a 500.

FalkorDB stops a query at its ``TIMEOUT`` and answers ``ResponseError("Query timed out")``;
redis-py raises ``TimeoutError`` when the socket gives up first. Either one, raised while a
tool runs, is a question too broad for the graph (a path between two far nodes on a large
graph), not a fault: ``guard`` logs it with what was being asked and raises ``ToolTimedOut``,
whose message is ``TIMEOUT_REPLY``, for the surface to answer with. Every other error passes
through untouched.

Used around tool execution by ``GET /api/ask``, the explorer routes and
``mcp_tools.call_tool``; the Slack turn is meant to use it the same way. The public surfaces
pass ``timeout_ms=PUBLIC_QUERY_TIMEOUT_MS``, so each graph query they run stops at that
instead of FalkorDB's server-wide ``TIMEOUT``: an anonymous caller cannot hold a worker or
fill the query queue for Slack and sync. A guard without ``timeout_ms`` keeps the server's.
They also pass ``deadline_ms=PUBLIC_REQUEST_DEADLINE_MS``: the whole request, however many
queries it sends, stops by then. ``public`` is that guard.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from redis.exceptions import ResponseError

from src.db import falkordb_client
from redis.exceptions import TimeoutError as RedisTimeoutError

log = logging.getLogger(__name__)

TIMEOUT_REPLY = "That took too long; try a narrower question."
# Per query, for the public ask page, the explorer and MCP. The server's TIMEOUT (10 s) stays for Slack and sync.
PUBLIC_QUERY_TIMEOUT_MS = 2000
# Per request, however many queries it sends (risk_map sends dozens).
PUBLIC_REQUEST_DEADLINE_MS = 4000


class ToolTimedOut(RuntimeError):
    """A graph query ran past its timeout. ``str()`` is the sentence to show the asker."""

    def __init__(self, message: str = TIMEOUT_REPLY) -> None:
        super().__init__(message)


def is_timeout(error: BaseException) -> bool:
    """Whether this error is a graph query that ran out of time, from FalkorDB or from the socket."""
    if isinstance(error, ToolTimedOut | RedisTimeoutError):
        return True
    return isinstance(error, ResponseError) and "timed out" in str(error).lower()


@contextmanager
def guard(surface: str, what: str = "", timeout_ms: int | None = None, deadline_ms: int | None = None) -> Iterator[None]:
    """Turn a query timeout inside the block into ``ToolTimedOut``, logged once with ``surface`` and ``what``.

    With ``timeout_ms``, every graph query in the block that names no timeout of its own stops at it.
    With ``deadline_ms``, all of the block's queries together must finish that long from now.
    """
    try:
        with falkordb_client.query_timeout(timeout_ms), falkordb_client.request_deadline(deadline_ms):
            yield
    except ToolTimedOut:
        raise
    except Exception as error:
        if not is_timeout(error):
            raise
        log.warning("[%s] graph query timed out (%s): %s", surface, error, " ".join(str(what).split())[:200])
        raise ToolTimedOut() from error


def public(surface: str, what: str = ""):
    """The guard for a public or MCP call: the short per-query timeout and the per-request deadline."""
    return guard(surface, what, timeout_ms=PUBLIC_QUERY_TIMEOUT_MS, deadline_ms=PUBLIC_REQUEST_DEADLINE_MS)
