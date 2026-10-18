"""Delayed jobs. Replaces the Durable Object ``schedule()`` / alarms company-brain uses.

Jobs live in a Redis sorted set (score = due time), so they survive a restart.
A daemon thread polls and runs due jobs by name; handlers register at import time.

    scheduler.register("runApprovalExpiry", run_approval_expiry)
    scheduler.schedule(900, "runApprovalExpiry", {"approval_id": "..."})

Jobs belong to an org. ``schedule`` stores the org in context with the job, and ``run_due``
runs the handler inside ``org_context`` of that org, so it reads and writes that org's
graph. A job of another org has its id prefixed with the org (``<org>:<job id>``), and
``exists``/``due_at``/``cancel`` prefix the id they are given the same way, so every caller
keeps naming jobs as before. The legacy org's ids are not prefixed, and a job stored with
no org (queued before orgs) is the legacy org's, so jobs already queued keep working.
"""

import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable

from src.core import tenancy
from src.db.falkordb_client import kv

log = logging.getLogger(__name__)

# The Redis keys. One deployment has one queue; a test run names its own (``BRAIN_SCHEDULE_KEY``,
# set by tests/__init__.py per TEST_GRAPH_SUFFIX), so two runs on one Redis never run, or drop as
# "org no longer exists", each other's jobs.
QUEUE = os.getenv("BRAIN_SCHEDULE_KEY", "") or "brain:schedule"
PAYLOADS = f"{QUEUE}:payload"
_QUEUE = QUEUE
_PAYLOADS = PAYLOADS
_POLL_SECONDS = 1.0

_handlers: dict[str, Callable[[dict], None]] = {}
_started = threading.Event()


def register(name: str, handler: Callable[[dict], None]) -> None:
    _handlers[name] = handler


def _org() -> tenancy.OrgRef:
    """The org a job call is for: the one in context, else the legacy org (quietly: a job's
    org is never ambiguous, the legacy org was the only one before orgs)."""
    return tenancy.current_org_or_none() or tenancy.legacy_org()


def _stored_id(job_id: str, org: tenancy.OrgRef) -> str:
    return job_id if tenancy.is_legacy(org) else f"{org.id}:{job_id}"


def schedule(delay_seconds: float, name: str, payload: dict | None = None, job_id: str | None = None) -> str:
    """Run ``name(payload)`` after ``delay_seconds``, in the org in context. Reusing ``job_id``
    replaces that org's pending job. Returns the id as the caller names it (unprefixed)."""
    org = _org()
    job_id = job_id or uuid.uuid4().hex
    stored = _stored_id(job_id, org)
    job = {"name": name, "payload": payload or {}}
    if not tenancy.is_legacy(org):
        job["org"] = org.id
    due = time.time() + max(0.0, delay_seconds)
    pipe = kv().pipeline()
    pipe.hset(_PAYLOADS, stored, json.dumps(job))
    pipe.zadd(_QUEUE, {stored: due})
    pipe.execute()
    return job_id


def exists(job_id: str) -> bool:
    return kv().zscore(_QUEUE, _stored_id(job_id, _org())) is not None


def due_at(job_id: str) -> float | None:
    """When the org's pending job runs (epoch seconds), or None."""
    return kv().zscore(_QUEUE, _stored_id(job_id, _org()))


def cancel(job_id: str) -> None:
    _cancel_stored(_stored_id(job_id, _org()))


def _cancel_stored(stored: str) -> None:
    pipe = kv().pipeline()
    pipe.zrem(_QUEUE, stored)
    pipe.hdel(_PAYLOADS, stored)
    pipe.execute()


def _job_org_id(job: dict) -> str:
    return job.get("org") or tenancy.LEGACY_ORG_ID


def cancel_matching(name: str, predicate: Callable[[dict], bool] | None = None) -> int:
    """Cancel the org's pending jobs by handler name (and optional payload predicate)."""
    org_id = _org().id
    cancelled = 0
    for job_id, raw in (kv().hgetall(_PAYLOADS) or {}).items():
        job = json.loads(raw)
        if _job_org_id(job) != org_id:
            continue
        if job["name"] == name and (predicate is None or predicate(job["payload"])):
            _cancel_stored(job_id.decode() if isinstance(job_id, bytes) else job_id)
            cancelled += 1
    return cancelled


def cancel_org(org_id: str) -> int:
    """Cancel every pending job of ``org_id`` (an org being removed)."""
    cancelled = 0
    for job_id, raw in (kv().hgetall(_PAYLOADS) or {}).items():
        if _job_org_id(json.loads(raw)) == org_id:
            _cancel_stored(job_id.decode() if isinstance(job_id, bytes) else job_id)
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
            org = tenancy.resolve(_job_org_id(job))
        except LookupError:
            log.warning("[scheduler] job %s belongs to org %s, which no longer exists; dropped", job["name"], job.get("org"))
            continue
        except Exception:
            # The control graph is unreachable: put the job back rather than run it in the wrong org.
            log.exception("[scheduler] org lookup failed for job %s; retrying in a minute", job["name"])
            pipe = kv().pipeline()
            pipe.hset(_PAYLOADS, job_id, raw)
            pipe.zadd(_QUEUE, {job_id: time.time() + 60})
            pipe.execute()
            continue
        try:
            with tenancy.org_context(org):
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
