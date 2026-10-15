"""Shared storage helpers for the ported Durable Object state.

company-brain kept this state in per-org DO SQLite, where every statement ran
serially. ``LOCK`` gives the same read-modify-write atomicity inside this process.
"""

import json
import threading
import time
from typing import Any

from src.db.falkordb_client import kv

LOCK = threading.RLock()


def now_ms() -> int:
    return int(time.time() * 1000)


def _decode(value: Any) -> Any:
    return value.decode() if isinstance(value, bytes) else value


def get_json(key: str) -> Any:
    raw = kv().get(key)
    return None if raw is None else json.loads(raw)


def set_json(key: str, value: Any, ttl_seconds: int | None = None) -> None:
    kv().set(key, json.dumps(value), ex=ttl_seconds)


def hget_json(key: str, field: str) -> Any:
    raw = kv().hget(key, field)
    return None if raw is None else json.loads(raw)


def hset_json(key: str, field: str, value: Any) -> None:
    kv().hset(key, field, json.dumps(value))


def hgetall_json(key: str) -> dict[str, Any]:
    return {_decode(field): json.loads(raw) for field, raw in (kv().hgetall(key) or {}).items()}


def hdel(key: str, *fields: str) -> None:
    if fields:
        kv().hdel(key, *fields)


def smembers(key: str) -> set[str]:
    return {_decode(member) for member in (kv().smembers(key) or set())}
