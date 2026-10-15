"""Delayed jobs. Replaces the Durable Object ``schedule()`` / alarms company-brain uses.

Jobs live in a Redis sorted set (score = due time), so they survive a restart.
A daemon thread polls and runs due jobs by name; handlers register at import time.

    scheduler.register("runApprovalExpiry", run_approval_expiry)
    scheduler.schedule(900, "runApprovalExpiry", {"approval_id": "..."})
"""

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable

from src.db.falkordb_client import kv

log = logging.getLogger(__name__)

_QUEUE = "brain:schedule"
_PAYLOADS = "brain:schedule:payload"
_POLL_SECONDS = 1.0

_handlers: dict[str, Callable[[dict], None]] = {}
_started = threading.Event()


def register(name: str, handler: Callable[[dict], None]) -> None:
    _handlers[name] = handler


def schedule(delay_seconds: float, name: str, payload: dict | None = None, job_id: str | None = None) -> str:
    """Run ``name(payload)`` after ``delay_seconds``. Reusing ``job_id`` replaces the pending job."""
    job_id = job_id or uuid.uuid4().hex
    due = time.time() + max(0.0, delay_seconds)
    pipe = kv().pipeline()
    pipe.hset(_PAYLOADS, job_id, json.dumps({"name": name, "payload": payload or {}}))
    pipe.zadd(_QUEUE, {job_id: due})
    pipe.execute()
    return job_id


def exists(job_id: str) -> bool:
    return kv().zscore(_QUEUE, job_id) is not None


def cancel(job_id: str) -> None:
    pipe = kv().pipeline()
    pipe.zrem(_QUEUE, job_id)
    pipe.hdel(_PAYLOADS, job_id)
    pipe.execute()


def cancel_matching(name: str, predicate: Callable[[dict], bool] | None = None) -> int:
    """Cancel pending jobs by handler name (and optional payload predicate)."""
    cancelled = 0
    for job_id, raw in (kv().hgetall(_PAYLOADS) or {}).items():
        job = json.loads(raw)
        if job["name"] == name and (predicate is None or predicate(job["payload"])):
            cancel(job_id.decode() if isinstance(job_id, bytes) else job_id)
            cancelled += 1
    return cancelled


def run_due(now: float | None = None) -> int:
    """Run every job that is due. Each job is claimed with ZREM, so it runs once."""
    now = now or time.time()
    ran = 0
    for raw_id in kv().zrangebyscore(_QUEUE, 0, now):
        job_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
        if not kv().zrem(_QUEUE, job_id):
            continue
        raw = kv().hget(_PAYLOADS, job_id)
        kv().hdel(_PAYLOADS, job_id)
        if raw is None:
            continue
        job = json.loads(raw)
        handler = _handlers.get(job["name"])
        if handler is None:
            log.warning("[scheduler] no handler for %s; dropped", job["name"])
            continue
        try:
            handler(job["payload"])
            ran += 1
        except Exception:
            log.exception("[scheduler] job %s failed", job["name"])
    return ran


def start() -> None:
    if _started.is_set():
        return
    _started.set()

    def loop() -> None:
        while True:
            try:
                run_due()
            except Exception:
                log.exception("[scheduler] poll failed")
            time.sleep(_POLL_SECONDS)

    threading.Thread(target=loop, name="brain-scheduler", daemon=True).start()
