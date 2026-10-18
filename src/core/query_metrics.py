"""Graph query metrics: durations, rows, timeouts and refusals, per tool and per org, in process.

``install()`` registers ``record`` as an ``AFTER_QUERY`` hook on ``falkordb_client`` (the
``_GraphProxy`` every ``connect()`` graph goes through), so every query is seen once, after it
returns or raises. For each one it notes:

- **tool**: the name set by ``tool_scope(name)`` if a caller set one; otherwise the nearest
  function on the stack named like a catalogue tool (``who_owns`` or ``_run_who_owns``);
  otherwise ``module:function`` of the nearest caller in ``src/`` outside the DB layer. All of
  these come from code, never from user input, so the set stays small.
- **org**: the org id in context (``src/core/tenancy.py``), or ``-`` with none.
- duration, rows returned, and whether it **timed out** (FalkorDB's "Query timed out", or the
  socket's), was **refused** because FalkorDB's queue was full ("Max pending queries
  exceeded", see ``MAX_QUEUED_QUERIES`` in docs/DEPLOY.md), or failed otherwise.

A query slower than ``QUERY_SLOW_MS`` (default 200) is logged once at WARNING with the tool,
org, graph, rows and the query's shape: whitespace collapsed, every string literal replaced by
``?``, cut at 200 characters. Parameter values are never logged; they may hold user text.

``snapshot()`` is what ``GET /ops/metrics`` (admin token) returns: per tool and per org, the
count, errors, timeouts, refusals, slow queries, rows, and p50/p95/max over the last
``WINDOW`` durations. Everything is bounded: at most ``MAX_KEYS`` tools and orgs each (later
ones are counted under ``_other``), and ``WINDOW`` durations per key. It resets on restart.
"""

import logging
import os
import re
import sys
import threading
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from src.db import falkordb_client
from src.db.falkordb_client import QueryCall

log = logging.getLogger(__name__)

SLOW_MS = float(os.getenv("QUERY_SLOW_MS", "200"))
WINDOW = 512
MAX_KEYS = 200
OTHER = "_other"
NO_ORG = "-"
QUEUE_FULL = "max pending queries exceeded"
_SHAPE_LIMIT = 200

query_tool: ContextVar[str | None] = ContextVar("query_tool", default=None)

_STRING = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
# Frames in these files are the query plumbing, never "the caller".
_SKIP_FILES = (os.sep + os.path.join("src", "db") + os.sep, os.sep + os.path.join("src", "core", "query_metrics.py"))
_SRC = os.sep + "src" + os.sep


@contextmanager
def tool_scope(name: str) -> Iterator[None]:
    """Within the block, every graph query is counted under tool ``name``."""
    token = query_tool.set(name)
    try:
        yield
    finally:
        query_tool.reset(token)


class _Stats:
    __slots__ = ("count", "errors", "timeouts", "rejected", "slow", "rows", "durations")

    def __init__(self) -> None:
        self.count = self.errors = self.timeouts = self.rejected = self.slow = self.rows = 0
        self.durations: deque[float] = deque(maxlen=WINDOW)

    def add(self, duration_ms: float, rows: int, outcome: str, slow: bool) -> None:
        self.count += 1
        self.rows += rows
        self.slow += slow
        self.durations.append(duration_ms)
        if outcome == "timeout":
            self.timeouts += 1
        elif outcome == "rejected":
            self.rejected += 1
        elif outcome == "error":
            self.errors += 1

    def to_dict(self) -> dict:
        ordered = sorted(self.durations)
        return {
            "count": self.count,
            "errors": self.errors,
            "timeouts": self.timeouts,
            "rejected": self.rejected,
            "slow": self.slow,
            "rows": self.rows,
            "p50_ms": _percentile(ordered, 50),
            "p95_ms": _percentile(ordered, 95),
            "max_ms": round(ordered[-1], 1) if ordered else None,
            "window": len(ordered),
        }


def _percentile(ordered: list[float], pct: int) -> float | None:
    """Nearest-rank percentile of an ascending list."""
    if not ordered:
        return None
    rank = max(1, -(-pct * len(ordered) // 100))
    return round(ordered[rank - 1], 1)


class _Registry:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.since = datetime.now(UTC)
        self.total = _Stats()
        self.tools: dict[str, _Stats] = {}
        self.orgs: dict[str, _Stats] = {}

    @staticmethod
    def _slot(table: dict[str, _Stats], key: str) -> _Stats:
        stats = table.get(key)
        if stats is None:
            # MAX_KEYS entries at most, ``_other`` included.
            if len(table) - (OTHER in table) >= MAX_KEYS - 1:
                key = OTHER
            stats = table.setdefault(key, _Stats())
        return stats

    def add(self, tool: str, org: str, duration_ms: float, rows: int, outcome: str, slow: bool) -> None:
        with self.lock:
            self.total.add(duration_ms, rows, outcome, slow)
            self._slot(self.tools, tool).add(duration_ms, rows, outcome, slow)
            self._slot(self.orgs, org).add(duration_ms, rows, outcome, slow)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "since": self.since.isoformat(timespec="seconds"),
                "seconds": round((datetime.now(UTC) - self.since).total_seconds(), 1),
                "slow_ms": SLOW_MS,
                "window": WINDOW,
                "total": self.total.to_dict(),
                "tools": {name: stats.to_dict() for name, stats in sorted(self.tools.items())},
                "orgs": {name: stats.to_dict() for name, stats in sorted(self.orgs.items())},
            }


_registry = _Registry()
_catalogue = None


def _is_tool(name: str) -> bool:
    """Whether ``name`` is a catalogue tool. The catalogue is imported on first use; if it cannot
    be, callers are named by function instead."""
    global _catalogue
    if _catalogue is None:
        try:
            from src.domain.knowledge import catalogue

            _catalogue = catalogue
        except Exception:
            log.debug("query metrics: no tool catalogue; callers are named by function", exc_info=True)
            _catalogue = False
    return bool(_catalogue) and _catalogue.get_tool(name) is not None


def caller() -> str:
    """The tool this query runs for: see the module docstring."""
    named = query_tool.get()
    if named:
        return named
    nearest = None
    frame = sys._getframe(1)
    while frame is not None:
        path = frame.f_code.co_filename
        if _SRC in path and not any(skip in path for skip in _SKIP_FILES):
            function = frame.f_code.co_name
            bare = function.removeprefix("_run_")
            if _is_tool(bare):
                return bare
            if nearest is None:
                module = frame.f_globals.get("__name__", "?").removeprefix("src.")
                nearest = f"{module}:{function}"
        frame = frame.f_back
    return nearest or "?"


def outcome(error: BaseException | None) -> str:
    if error is None:
        return "ok"
    if isinstance(error, RedisTimeoutError):
        return "timeout"
    if isinstance(error, ResponseError):
        text = str(error).lower()
        if "timed out" in text:
            return "timeout"
        if QUEUE_FULL in text:
            return "rejected"
    return "error"


def shape(query: str) -> str:
    """The query with string literals blanked and whitespace collapsed, for logs."""
    return " ".join(_STRING.sub("?", query or "").split())[:_SHAPE_LIMIT]


def _rows(result) -> int:
    rows = getattr(result, "result_set", None)
    return len(rows) if isinstance(rows, list) else 0


def record(call: QueryCall) -> None:
    """The AFTER_QUERY hook."""
    from src.core.tenancy import current_org

    duration = call.duration_ms or 0.0
    tool = caller()
    org = current_org() or NO_ORG
    rows = _rows(call.result)
    result = outcome(call.error)
    slow = duration >= SLOW_MS
    _registry.add(tool, org, duration, rows, result, slow)
    if slow or result in ("timeout", "rejected"):
        log.warning(
            "[falkordb] %s query %.0f ms tool=%s org=%s graph=%s method=%s rows=%d timeout=%s: %s",
            "slow" if result == "ok" else result,
            duration,
            tool,
            org,
            call.graph,
            call.method,
            rows,
            call.timeout,
            shape(call.query),
        )


def snapshot() -> dict:
    return _registry.snapshot()


def reset() -> None:
    with _registry.lock:
        _registry.reset()


_installed = False
_install_lock = threading.Lock()


def install() -> None:
    """Register the hook once per process."""
    global _installed
    with _install_lock:
        if not _installed:
            falkordb_client.add_query_hook(after=record)
            _installed = True


def uninstall() -> None:
    global _installed
    with _install_lock:
        falkordb_client.remove_query_hook(record)
        _installed = False
