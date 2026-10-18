"""Delayed jobs. Replaces the Durable Object ``schedule()`` / alarms company-brain uses.

Jobs live in Redis, so they survive a restart; handlers register at import time by name.

    scheduler.register("runApprovalExpiry", run_approval_expiry)
    scheduler.schedule(900, "runApprovalExpiry", {"approval_id": "..."})

**Orgs.** Jobs belong to an org. ``schedule`` stores the org in context with the job, and the
handler runs inside ``org_context`` of that org, so it reads and writes that org's graph. A job of
another org has its id prefixed with the org (``<org>:<job id>``), and ``exists``/``due_at``/
``cancel`` prefix the id they are given the same way, so every caller keeps naming jobs as before.
The legacy org's ids are not prefixed, and a job stored with no org (queued before orgs) is the
legacy org's, so jobs already queued keep working.

**Keys** (``QUEUE`` is ``brain:schedule``; a test run names its own through ``BRAIN_SCHEDULE_KEY``):

    QUEUE                 zset  stored id -> when it is due (epoch s)
    QUEUE:payload         hash  stored id -> {"name", "payload", "org"?}
    QUEUE:running         zset  stored id -> lease deadline (epoch s), while a worker runs it
    QUEUE:name:<name>     set   stored ids by handler name (``cancel_matching``)
    QUEUE:org:<org>       set   stored ids by org (``cancel_org``)
    QUEUE:lock:<org>:<group>    the one running job of that group in that org (SET NX PX)

**Running.** ``start()`` runs a small pool: ``WORKERS`` workers take any due job, and
``FAST_WORKERS`` take only the ``FAST`` jobs (an approval expiring), so a long connector sync or
model call never holds those up. A worker *claims* a job with one Lua script: the job moves from
the queue to ``running`` with a lease, and only if no other job of its group (by default its
name; see ``GROUPS``) is running in its org. The payload is deleted only after the handler
returns, and only if the handler did not re-arm the same id meanwhile (a sync step schedules its
next step under its own id). While a handler runs its lease is renewed; a worker that dies leaves
the lease to run out, and the reaper puts the job back in the queue, so a job runs at least once.
Every handler is written to be run again safely (each re-reads its state before acting).
``run_due()`` is the same claim, run in the caller's thread: the tests and scripts use it.
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

QUEUE = os.getenv("BRAIN_SCHEDULE_KEY", "") or "brain:schedule"
PAYLOADS = f"{QUEUE}:payload"
RUNNING = f"{QUEUE}:running"
INDEXED = f"{QUEUE}:indexed"
_QUEUE = QUEUE
_PAYLOADS = PAYLOADS
_POLL_SECONDS = 1.0

WORKERS = int(os.getenv("SCHEDULER_WORKERS", "4"))
FAST_WORKERS = 1
# How long a claim holds before the reaper may hand the job to another worker; renewed while it runs.
LEASE_SECONDS = 300
RENEW_SECONDS = 60
# Due jobs one claim reads per round trip inside the script, and the most it looks through in all.
CLAIM_SCAN = 200
CLAIM_MAX_SCAN = 5_000

# Jobs the fast lane takes: an approval card left unanswered must expire on time, whatever else runs.
FAST = frozenset({"runApprovalExpiry"})
# Jobs that touch the same state run one at a time per org: one group. Every other job is its own group.
GROUPS = {
    # A sync step, a disconnect's batched deletion and an orphan purge write and delete the same install's nodes.
    "runConnectorSync": "connector",
    "runConnectorRemoval": "connector",
    "runConnectorPurge": "connector",
    # The team invite wave's pages, provisioning and invite steps share its progress record and Slack's rate limit.
    "runTeamInviteDirectoryPage": "team_invite",
    "runTeamInviteProvisionStep": "team_invite",
    "runTeamInviteStep": "team_invite",
    "runSlackTeamJoinRetry": "team_invite",
    "runSlackUserChangeRetry": "team_invite",
    # The public channel rollout and its beachhead share the rollout record.
    "runPublicChannelRollout": "rollout",
    "runPublicChannelBeachhead": "rollout",
    # The GitHub jobs share the installation token's rate limit.
    "runCoordinationAlertRetry": "github",
    "runGitHubCollaboratorRefresh": "github",
}

_handlers: dict[str, Callable[[dict], None]] = {}
_started = threading.Event()
# Stored id -> lock key, for the jobs this process is running (the lease keeper renews them).
_running: dict[str, str] = {}
_running_lock = threading.Lock()

# Claim one due job: KEYS = queue, payloads, running. ARGV = now, lease (s), scan, lane, lock prefix,
# the FAST names and the GROUPS as JSON, the legacy org's id, and optionally (JSON) the only ids to
# consider. Returns {stored id, payload json, lock key} or nil.
_CLAIM = """
local now, lease, scan, lane, prefix = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]), ARGV[4], ARGV[5]
local fast = cjson.decode(ARGV[6])
local groups = cjson.decode(ARGV[7])
local legacy = ARGV[8]
local listed = ARGV[9] and ARGV[9] ~= ''
local max_scan = tonumber(ARGV[10])
-- Locks found taken in this call: every other job of that (org, group) is skipped without asking again,
-- and the scan goes on past them, so a backlog of one blocked group never hides anyone else's job.
local blocked = {}
local offset = 0
while true do
local ids
if listed then
  if offset > 0 then return nil end
  ids = cjson.decode(ARGV[9])
else
  ids = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now, 'LIMIT', offset, scan)
  if #ids == 0 or offset >= max_scan then return nil end
end
offset = offset + scan
for _, id in ipairs(ids) do
  local due = redis.call('ZSCORE', KEYS[1], id)
  local raw = due and tonumber(due) <= now and redis.call('HGET', KEYS[2], id)
  if due and tonumber(due) > now then
    -- re-armed for later since it was listed: not due
  elseif not due then
    -- claimed or cancelled meanwhile
  elseif not raw then
    redis.call('ZREM', KEYS[1], id)
  else
    local ok, job = pcall(cjson.decode, raw)
    if not ok or type(job) ~= 'table' or type(job.name) ~= 'string' then
      redis.call('ZREM', KEYS[1], id)
      redis.call('HDEL', KEYS[2], id)
    elseif lane ~= 'fast' or fast[job.name] then
      local org = legacy
      if type(job.org) == 'string' and job.org ~= '' then org = job.org end
      local lock = prefix .. org .. ':' .. (groups[job.name] or job.name)
      if not blocked[lock] then
        if redis.call('SET', lock, id, 'NX', 'PX', lease * 1000) then
          redis.call('ZREM', KEYS[1], id)
          redis.call('ZADD', KEYS[3], now + lease, id)
          return {id, raw, lock}
        end
        blocked[lock] = true
      end
    end
  end
end
end
"""

# A claimed job is done: KEYS = queue, payloads, running, the name index, the org index, the lock.
# ARGV = stored id. The payload stays when the handler re-armed the id (it is in the queue again).
_FINISH = """
local id = ARGV[1]
redis.call('ZREM', KEYS[3], id)
if not redis.call('ZSCORE', KEYS[1], id) then
  redis.call('HDEL', KEYS[2], id)
  redis.call('SREM', KEYS[4], id)
  redis.call('SREM', KEYS[5], id)
end
if redis.call('GET', KEYS[6]) == id then
  redis.call('DEL', KEYS[6])
end
return 1
"""

# Jobs whose lease ran out (their worker died): back in the queue, due now. KEYS = queue, payloads,
# running. ARGV = now. Returns how many.
_REAP = """
local now = tonumber(ARGV[1])
local ids = redis.call('ZRANGEBYSCORE', KEYS[3], '-inf', now)
for _, id in ipairs(ids) do
  redis.call('ZREM', KEYS[3], id)
  if redis.call('HEXISTS', KEYS[2], id) == 1 and not redis.call('ZSCORE', KEYS[1], id) then
    redis.call('ZADD', KEYS[1], now, id)
  end
end
return #ids
"""


def register(name: str, handler: Callable[[dict], None]) -> None:
    _handlers[name] = handler


def _org() -> tenancy.OrgRef:
    """The org a job call is for: the one in context, else the legacy org (quietly: a job's
    org is never ambiguous, the legacy org was the only one before orgs)."""
    return tenancy.current_org_or_none() or tenancy.legacy_org()


def _stored_id(job_id: str, org: tenancy.OrgRef) -> str:
    return job_id if tenancy.is_legacy(org) else f"{org.id}:{job_id}"


def _name_key(name: str) -> str:
    return f"{QUEUE}:name:{name}"


def _org_key(org_id: str) -> str:
    return f"{QUEUE}:org:{org_id}"


def _lock_prefix() -> str:
    return f"{QUEUE}:lock:"


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
    previous = kv().hget(PAYLOADS, stored)
    pipe = kv().pipeline()
    if previous:
        old = _decode(previous)
        if old is not None and old["name"] != name:
            pipe.srem(_name_key(old["name"]), stored)
    pipe.hset(PAYLOADS, stored, json.dumps(job))
    pipe.zadd(QUEUE, {stored: due})
    pipe.sadd(_name_key(name), stored)
    pipe.sadd(_org_key(_job_org_id(job)), stored)
    pipe.execute()
    return job_id


def exists(job_id: str) -> bool:
    return kv().zscore(QUEUE, _stored_id(job_id, _org())) is not None


def due_at(job_id: str) -> float | None:
    """When the org's pending job runs (epoch seconds), or None."""
    return kv().zscore(QUEUE, _stored_id(job_id, _org()))


def cancel(job_id: str) -> None:
    _cancel_stored(_stored_id(job_id, _org()))


def _decode(raw) -> dict | None:
    try:
        job = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return job if isinstance(job, dict) and isinstance(job.get("name"), str) else None


def _cancel_stored(stored: str, job: dict | None = None) -> None:
    if job is None:
        job = _decode(kv().hget(PAYLOADS, stored))
    pipe = kv().pipeline()
    pipe.zrem(QUEUE, stored)
    pipe.hdel(PAYLOADS, stored)
    if job is not None:
        pipe.srem(_name_key(job["name"]), stored)
        pipe.srem(_org_key(_job_org_id(job)), stored)
    pipe.execute()


def _job_org_id(job: dict) -> str:
    return job.get("org") or tenancy.LEGACY_ORG_ID


def _ensure_indexes() -> None:
    """Index jobs queued before the name and org indexes existed: once per queue, on a marker key."""
    if kv().get(INDEXED):
        return
    pipe = kv().pipeline()
    for stored, raw in (kv().hgetall(PAYLOADS) or {}).items():
        job = _decode(raw)
        if job is not None:
            pipe.sadd(_name_key(job["name"]), stored)
            pipe.sadd(_org_key(_job_org_id(job)), stored)
    pipe.set(INDEXED, "1")
    pipe.execute()


def _indexed_jobs(index_key: str) -> list[tuple[str, dict]]:
    """The jobs an index names, read in one round trip; ids whose job is gone leave the index."""
    _ensure_indexes()
    ids = sorted(kv().smembers(index_key) or [])
    if not ids:
        return []
    raws = kv().hmget(PAYLOADS, ids)
    found, gone = [], []
    for stored, raw in zip(ids, raws, strict=True):
        job = _decode(raw) if raw is not None else None
        if job is None:
            gone.append(stored)
        else:
            found.append((stored, job))
    if gone:
        kv().srem(index_key, *gone)
    return found


def cancel_matching(name: str, predicate: Callable[[dict], bool] | None = None) -> int:
    """Cancel the org's pending jobs by handler name (and optional payload predicate)."""
    org_id = _org().id
    cancelled = 0
    for stored, job in _indexed_jobs(_name_key(name)):
        if _job_org_id(job) != org_id or job["name"] != name:
            continue
        if predicate is None or predicate(job["payload"]):
            _cancel_stored(stored, job)
            cancelled += 1
    return cancelled


def cancel_org(org_id: str) -> int:
    """Cancel every pending job of ``org_id`` (an org being removed)."""
    cancelled = 0
    for stored, job in _indexed_jobs(_org_key(org_id)):
        if _job_org_id(job) == org_id:
            _cancel_stored(stored, job)
            cancelled += 1
    return cancelled


# --- running --------------------------------------------------------------------------------


def _claim(now: float, lane: str = "any", only: list[str] | None = None) -> tuple[str, str, str] | None:
    if lane == "fast" and only is None:
        # The fast lane reads its own jobs through the name index: however long the queue, an
        # approval expiry is found at once.
        only = sorted({stored for name in FAST for stored in (kv().smembers(_name_key(name)) or [])})
        if not only:
            return None
    claimed = kv().eval(
        _CLAIM, 3, QUEUE, PAYLOADS, RUNNING,
        now, LEASE_SECONDS, CLAIM_SCAN, lane, _lock_prefix(), json.dumps({name: True for name in FAST}),
        json.dumps(GROUPS), tenancy.LEGACY_ORG_ID, json.dumps(only) if only else "", CLAIM_MAX_SCAN,
    )
    if not claimed:
        return None
    return tuple(part.decode() if isinstance(part, bytes) else part for part in claimed)


def _finish(stored: str, job: dict | None, lock: str) -> None:
    name_key = _name_key(job["name"]) if job else f"{QUEUE}:name:?"
    org_key = _org_key(_job_org_id(job)) if job else f"{QUEUE}:org:?"
    kv().eval(_FINISH, 6, QUEUE, PAYLOADS, RUNNING, name_key, org_key, lock, stored)


def reap(now: float | None = None) -> int:
    """Put back the jobs whose worker stopped renewing their lease. Returns how many."""
    return int(kv().eval(_REAP, 3, QUEUE, PAYLOADS, RUNNING, now or time.time()) or 0)


def _renew() -> None:
    """Extend the lease of every job this process is running (the lease keeper, every RENEW_SECONDS)."""
    with _running_lock:
        held = dict(_running)
    if not held:
        return
    deadline = time.time() + LEASE_SECONDS
    pipe = kv().pipeline()
    for stored, lock in held.items():
        pipe.zadd(RUNNING, {stored: deadline}, xx=True)
        pipe.pexpire(lock, LEASE_SECONDS * 1000)
    pipe.execute()


def _run_claimed(stored: str, raw: str, lock: str) -> bool:
    """Run one claimed job in the org it belongs to; True when its handler ran without raising."""
    job = _decode(raw)
    with _running_lock:
        _running[stored] = lock
    try:
        if job is None:
            return False
        handler = _handlers.get(job["name"])
        if handler is None:
            log.warning("[scheduler] no handler for %s; dropped", job["name"])
            return False
        try:
            org = tenancy.resolve(_job_org_id(job))
        except LookupError:
            log.warning("[scheduler] job %s belongs to org %s, which no longer exists; dropped", job["name"], job.get("org"))
            return False
        except Exception:
            # The control graph is unreachable: put the job back rather than run it in the wrong org.
            log.exception("[scheduler] org lookup failed for job %s; retrying in a minute", job["name"])
            kv().zadd(QUEUE, {stored: time.time() + 60}, nx=True)
            return False
        try:
            with tenancy.org_context(org):
                handler(job["payload"])
            return True
        except Exception:
            log.exception("[scheduler] job %s failed", job["name"])
            return False
    finally:
        with _running_lock:
            _running.pop(stored, None)
        try:
            _finish(stored, job, lock)
        except Exception:
            # The lease runs out and the reaper puts the job back: it runs again, never silently lost.
            log.exception("[scheduler] finishing %s failed; its lease will hand it back", stored)


def run_due(now: float | None = None, lane: str = "any") -> int:
    """Run every job that was due when called, one after another in this thread. Each is claimed
    first, so it runs once even with workers running too; a job that re-arms itself as due now runs
    on the next call, as before. Returns how many handlers ran without raising."""
    now = now or time.time()
    reap(now)
    pending = list(kv().zrangebyscore(QUEUE, "-inf", now))
    ran = 0
    while pending:
        claimed = _claim(now, lane, pending)
        if claimed is None:
            break
        pending.remove(claimed[0])
        ran += _run_claimed(*claimed)
    return ran


def _worker(lane: str, stop: threading.Event | None = None) -> None:
    """Claim and run due jobs of ``lane`` ("any" or "fast") until ``stop`` is set (never, in the app)."""
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            claimed = _claim(time.time(), lane)
        except Exception:
            log.exception("[scheduler] claim failed")
            claimed = None
        if claimed is None:
            stop.wait(_POLL_SECONDS)
            continue
        _run_claimed(*claimed)


def _keeper() -> None:
    while True:
        time.sleep(min(RENEW_SECONDS, _POLL_SECONDS * 5))
        try:
            _renew()
            reap()
        except Exception:
            log.exception("[scheduler] lease keeper failed")


def start() -> None:
    if _started.is_set():
        return
    _started.set()
    try:
        _ensure_indexes()
    except Exception:
        log.exception("[scheduler] indexing queued jobs failed; cancel_matching reads them on first use")
    for n in range(WORKERS):
        threading.Thread(target=_worker, args=("any",), name=f"brain-scheduler-{n}", daemon=True).start()
    for n in range(FAST_WORKERS):
        threading.Thread(target=_worker, args=("fast",), name=f"brain-scheduler-fast-{n}", daemon=True).start()
    threading.Thread(target=_keeper, name="brain-scheduler-leases", daemon=True).start()
