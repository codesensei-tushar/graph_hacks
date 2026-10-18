"""Keep each configured install in step with its source: one page per scheduler step.

A step lists the containers when that is due (hourly, and after a restart), refreshes who
may read the restricted ones, then fetches one page for a container that is due, the one
fetched least recently, so a long backfill and the other containers' polls take turns.
The page is written, its ``next_cursor`` stored on the container's state node, and the job
re-armed for the next step, always, in ``finally``. Short steps keep the shared scheduler
thread free for Slack's own jobs. A container that reaches ``MAX_ITEMS_PER_CONTAINER``
stops its backfill there: it keeps its cursor and is fetched again at its next poll.

| Outcome                    | Next step                                                   |
|----------------------------|-------------------------------------------------------------|
| more pages waiting         | ``MORE_DELAY_SECONDS``, or the page's ``retry_after``       |
| caught up                  | the connector's ``poll_seconds``                             |
| ``RetryLater(s)``          | ``s``, at most an hour; the cursor does not move             |
| ``CredentialsRejected``    | an hour                                                      |
| any other failure          | 1, 5, 15, then 60 minutes as failures repeat; others go on   |

A signed webhook is written through the same ``graph.write_page`` (``handle_delivery``);
it never moves a cursor, so the next poll fills in anything a delivery missed.

Every install of every org has its own job, run by the scheduler inside the install's org,
so it writes that org's graph. The legacy org's env install of a source keeps the job id
``connector:sync:<source>`` (and its payload names the source only); any other install's is
``connector:sync:<org>:<source>:<install>``. ``remove_install`` disconnects one: it stops the
job, deletes everything the install wrote 500 nodes at a time (a job, so a restart resumes
it), then its install node and its external account.
"""

import hashlib
import logging
import time

from src.connectors import graph, registry
from src.connectors.base import (
    MAX_RETRY_AFTER_SECONDS,
    Connector,
    ConnectorError,
    Container,
    CredentialsRejected,
    Install,
    RetryLater,
    WebhookRequest,
    clamp_wait,
)
from src.core import scheduler, tenancy
from src.db.falkordb_client import connect, kv

log = logging.getLogger(__name__)

SYNC_JOB = "runConnectorSync"
REMOVE_JOB = "runConnectorRemoval"
PURGE_JOB = "runConnectorPurge"
REMOVE_BATCH = 500
# A container keeps at most this many items; past it, new items are dropped and existing ones update.
MAX_ITEMS_PER_CONTAINER = 1000
MORE_DELAY_SECONDS = 2.0
CONTAINERS_REFRESH_SECONDS = 60 * 60
CREDENTIALS_BACKOFF_SECONDS = 60 * 60
FAILURE_BACKOFF_SECONDS = (60, 5 * 60, 15 * 60, 60 * 60)
DELIVERY_DEDUP_TTL_SECONDS = 60 * 60 * 24
ERROR_MAX_CHARS = 300

# Container objects as the connector last listed them, by (org, install key). Rebuilt after a restart.
_containers: dict[tuple[str, str], tuple[float, list[Container]]] = {}


def _is_legacy_env(install: Install) -> bool:
    return install.auth == "env" and install.org == tenancy.LEGACY_ORG_ID


def job_id(source: str, install: Install | None = None) -> str:
    """``connector:sync:<source>`` for the legacy env install, else ``connector:sync:<org>:<source>:<install>``."""
    if install is None or _is_legacy_env(install):
        return f"connector:sync:{source}"
    return f"connector:sync:{install.org}:{install.source}:{install.id}"


def _cache_key(install: Install) -> tuple[str, str]:
    return (install.org, install.key)


def resolve(source: str, install_id: str | None = None) -> tuple[Connector | None, Install | None]:
    """The connector and install a job or route names: the legacy env install of ``source`` when
    ``install_id`` is None, else the org in context's install ``install_id``."""
    connector = registry.get(source)
    if install_id is None:
        return connector, registry.install_for(source)
    return connector, registry.get_install(tenancy.current_org_ref().id, source, install_id)


def max_delay(source: str) -> float:
    """The longest a step may be put off: an hour, or the connector's poll interval when longer."""
    connector = registry.get(source)
    return float(max(MAX_RETRY_AFTER_SECONDS, CREDENTIALS_BACKOFF_SECONDS, *FAILURE_BACKOFF_SECONDS, connector.poll_seconds if connector else 0))


def _payload(install: Install | None, source: str) -> dict:
    if install is None or _is_legacy_env(install):
        return {"source": source}
    return {"source": source, "install": install.id}


def schedule(source: str, delay: float = 0.0, install: Install | None = None) -> None:
    """Arm the step of ``install`` (default: the legacy env install of ``source``), in its org."""
    delay = min(max(0.0, float(delay)), max_delay(source))
    org = install.org if install is not None else tenancy.LEGACY_ORG_ID
    with tenancy.org_context(org):
        scheduler.schedule(delay, SYNC_JOB, _payload(install, source), job_id=job_id(source, install))


def cancel(install: Install) -> None:
    with tenancy.org_context(install.org):
        scheduler.cancel(job_id(install.source, install))


def schedule_all() -> list[str]:
    """Arm every install of every org that has no step pending, or one put off further than any
    step may be (written before the clamp, or by a clock that jumped). Startup."""
    armed = []
    for connector, install in registry.all_installs():
        try:
            with tenancy.org_context(install.org):
                due = scheduler.due_at(job_id(connector.source, install))
        except Exception:
            log.exception("[connectors] cannot arm %s of org %s", install.key, install.org)
            continue
        if due is None or due - time.time() > max_delay(connector.source):
            schedule(connector.source, 0, install)
            armed.append(connector.source if _is_legacy_env(install) else f"{install.org}:{install.key}")
    return armed


def run_sync(payload: dict) -> None:
    """The scheduler job (in the install's org): one step, then always the next one while the install is configured."""
    source = (payload or {}).get("source") or ""
    install_id = (payload or {}).get("install")
    delay = FAILURE_BACKOFF_SECONDS[0]
    try:
        delay = sync_step(source, install_id).get("next_in", delay)
    except Exception:
        log.exception("[connectors] %s sync step failed", source)
    finally:
        _connector, install = resolve(source, install_id)
        if install is not None:
            schedule(source, delay, install)


def _error(error: BaseException) -> str:
    text = str(error) if isinstance(error, ConnectorError) else f"{type(error).__name__}: {error}"
    return text[:ERROR_MAX_CHARS]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _backoff(failures: int) -> float:
    return float(FAILURE_BACKOFF_SECONDS[min(max(failures, 1), len(FAILURE_BACKOFF_SECONDS)) - 1])


def sync_step(source: str, install_id: str | None = None) -> dict:
    """One step for an install (default: the legacy env install of ``source``), in the org in
    context. Returns what happened and ``next_in``, the seconds until the next step."""
    connector, install = resolve(source, install_id)
    if connector is None or install is None:
        return {"status": "not_configured"}
    if install.org != tenancy.current_org_ref().id:
        log.warning("[connectors] %s of org %s refused in org %s", install.key, install.org, tenancy.current_org_ref().id)
        return {"status": "not_configured"}
    report = _step(connector, install)
    _record_status(install, report)
    return report


def _record_status(install: Install, report: dict) -> None:
    """A stored install's status for the console: active once a step works, error on rejected credentials."""
    if install.auth == "env":
        return
    from src.connectors import store

    status = report.get("status")
    try:
        if status == "credentials_rejected":
            store.set_status(install.key, "error", "The credentials were rejected. Reconnect to fix it.")
        elif status in ("synced", "idle", "no_containers"):
            current = store.get(install.key)
            if current is not None and (current.status != "active" or current.last_error):
                store.set_status(install.key, "active")
    except Exception:
        log.exception("[connectors] status of %s not recorded", install.key)


def _step(connector: Connector, install: Install) -> dict:
    state = graph.get_state(install.key)
    failures = int(state.get("failures") or 0)
    try:
        containers = _current_containers(connector, install)
    except RetryLater as error:
        graph.set_state(install.key, install, last_error=_error(error))
        return {"status": "retry_later", "next_in": error.seconds}
    except CredentialsRejected as error:
        graph.set_state(install.key, install, last_error=_error(error), failures=failures + 1)
        log.warning("[connectors] %s credentials rejected; next try in %ds", install.key, CREDENTIALS_BACKOFF_SECONDS)
        return {"status": "credentials_rejected", "next_in": CREDENTIALS_BACKOFF_SECONDS}
    except Exception as error:
        graph.set_state(install.key, install, last_error=_error(error), failures=failures + 1)
        log.warning("[connectors] %s listing containers failed: %s", install.key, _error(error))
        return {"status": "failed", "next_in": _backoff(failures + 1)}
    if not containers:
        return {"status": "no_containers", "next_in": float(connector.poll_seconds)}

    now = _now_ms()
    ceiling = max_delay(install.source)
    states = {container.id: graph.get_state(container.id) for container in containers}
    container = _next_container(containers, states, now, ceiling)
    if container is None:
        return {"status": "idle", "next_in": _next_in(states, now, connector, ceiling)}
    report = _fetch_one(connector, install, container, states[container.id], failures)
    states[container.id] = graph.get_state(container.id)
    report["next_in"] = _next_in(states, _now_ms(), connector, ceiling)
    return report


def _current_containers(connector: Connector, install: Install) -> list[Container]:
    cached = _containers.get(_cache_key(install))
    if cached and time.time() - cached[0] < CONTAINERS_REFRESH_SECONDS:
        return cached[1]
    info = connector.check_credentials(install)
    containers = connector.containers(install)
    graph.write_containers(install, containers)
    for container in containers:
        if container.restricted:
            try:
                readers = connector.readers(install, container)
            except Exception as error:
                # Fail closed: without a fresh list, nobody keeps access.
                log.warning("[connectors] readers unavailable container=%s: %s; clearing access", container.id, _error(error))
                readers = None
            graph.write_readers(install, container.id, readers)
    _containers[_cache_key(install)] = (time.time(), containers)
    graph.set_state(install.key, install, account=info.account, site=info.site, containers_at=_now_ms(), containers=len(containers), last_error=None)
    log.info("[connectors] %s containers=%d account=%s", install.key, len(containers), info.account)
    return containers


def _due_at(state: dict, now: int, ceiling: float) -> int:
    """When a container is due. One put off further than any step may be (written before waits
    were clamped, or by a clock that jumped) is due now."""
    due = int(state.get("due_at") or 0)
    return 0 if due - now > ceiling * 1000 else due


def _next_container(containers: list[Container], states: dict[str, dict], now: int, ceiling: float) -> Container | None:
    """The due container fetched least recently, whether it has pages waiting or its poll came round."""
    due = [container for container in containers if _due_at(states[container.id], now, ceiling) <= now]
    if not due:
        return None
    return min(due, key=lambda container: (int(states[container.id].get("last_fetch_at") or 0), _due_at(states[container.id], now, ceiling)))


def _next_in(states: dict[str, dict], now: int, connector: Connector, ceiling: float) -> float:
    if not states:
        return float(connector.poll_seconds)
    soonest = min(_due_at(state, now, ceiling) for state in states.values())
    return max(MORE_DELAY_SECONDS if any(state.get("has_more") for state in states.values()) else 1.0, (soonest - now) / 1000)


def _fetch_one(connector: Connector, install: Install, container: Container, state: dict, failures: int) -> dict:
    cursor = state.get("cursor")
    now = _now_ms()
    try:
        page = connector.fetch(install, container, cursor)
        # A page the writer refuses (an id outside the install) fails like a fetch, so it cannot starve the others.
        written = graph.write_page(install, page, MAX_ITEMS_PER_CONTAINER)
    except RetryLater as error:
        graph.set_state(container.id, install, due_at=now + int(error.seconds * 1000), last_error=_error(error))
        return {"status": "retry_later", "container": container.id}
    except CredentialsRejected as error:
        graph.set_state(container.id, install, due_at=now + CREDENTIALS_BACKOFF_SECONDS * 1000, last_error=_error(error))
        graph.set_state(install.key, install, last_error=_error(error), failures=failures + 1)
        _containers.pop(_cache_key(install), None)  # check the credentials again next time
        return {"status": "credentials_rejected", "container": container.id}
    except Exception as error:
        container_failures = int(state.get("failures") or 0) + 1
        graph.set_state(container.id, install, due_at=now + int(_backoff(container_failures) * 1000), last_error=_error(error), failures=container_failures)
        graph.set_state(install.key, install, last_error=_error(error), failures=failures + 1)
        log.warning("[connectors] fetch failed container=%s: %s", container.id, _error(error))
        return {"status": "failed", "container": container.id}

    # At the cap a backfill ends: paging on would only drop what it reads. Polling resumes from the
    # newest point the backfill saw (``poll_cursor``), so the kept items stay current; a connector
    # that gives none keeps its backfill cursor and pages on through older items one poll at a time.
    capped = graph.count_items(container.id) >= MAX_ITEMS_PER_CONTAINER
    more = bool(page.has_more) and not capped
    poll_cursor = page.poll_cursor if page.poll_cursor is not None else state.get("poll_cursor")
    cursor = poll_cursor if page.has_more and capped and poll_cursor is not None else page.next_cursor
    wait = max(MORE_DELAY_SECONDS if more else float(connector.poll_seconds), clamp_wait(page.retry_after or 0))
    items = int(state.get("items") or 0) + written["items"]
    graph.set_state(
        container.id,
        install,
        cursor=cursor,
        # Kept only while a backfill runs; once it ends, the cursor is the poll cursor.
        poll_cursor=poll_cursor if more else None,
        has_more=more,
        due_at=now + int(wait * 1000),
        last_fetch_at=now,
        last_error=None,
        failures=0,
        items=items,
        backfilled=bool(state.get("backfilled")) or not more,
        capped=capped,
    )
    graph.set_state(install.key, install, last_sync_at=now, last_error=None, failures=0)
    log.info(
        "[connectors] synced container=%s items=%d users=%d edges=%d deleted=%d skipped=%d more=%s",
        container.id, written["items"], written["users"], written["edges"], written["deleted"], written["skipped"], more,
    )
    return {"status": "synced", "container": container.id, **written}


def sync_now(source: str, full: bool = False, install_id: str | None = None) -> bool:
    """Make every container due and run a step on the next scheduler poll. ``full`` starts each one over.
    ``install_id`` names an install of the org in context; None is the legacy env install."""
    _connector, install = resolve(source, install_id)
    if install is None:
        return False
    with tenancy.org_context(install.org):
        _containers.pop(_cache_key(install), None)
        for state in graph.list_states(install):
            if state.get("id") == install.key:
                continue
            fields = {"due_at": 0, "failures": 0}
            if full:
                fields.update(cursor=None, poll_cursor=None, has_more=False, backfilled=False)
            graph.set_state(state["id"], install, **fields)
    schedule(source, 0, install)
    return True


def install_status(connector: Connector, install: Install) -> dict:
    """One install's sync status, read in the org in context: nothing secret, no config values."""
    state = graph.get_state(install.key)
    names = {container.id: container.name for container in (_containers.get(_cache_key(install)) or (0, []))[1]}
    containers = []
    for item in graph.list_states(install):
        if item.get("id") == install.key:
            continue
        containers.append(
            {
                "id": item["id"],
                "name": names.get(item["id"]),
                "items": graph.count_items(item["id"]),
                "backfilled": bool(item.get("backfilled")),
                "has_more": bool(item.get("has_more")),
                "capped": bool(item.get("capped")),
                "cursor": item.get("cursor"),
                "last_fetch_at": item.get("last_fetch_at"),
                "due_at": item.get("due_at"),
                "last_error": item.get("last_error"),
            }
        )
    due = scheduler.due_at(job_id(connector.source, install))
    return {
        "source": connector.source,
        "install": install.id,
        "account": state.get("account"),
        "site": state.get("site"),
        "webhooks": connector.accepts_webhooks,
        "last_sync_at": state.get("last_sync_at"),
        "last_error": state.get("last_error"),
        "failures": int(state.get("failures") or 0),
        "next_step_at": int(due * 1000) if due is not None else None,
        "containers": containers,
    }


def status() -> list[dict]:
    """What the admin status route shows (the legacy org's env installs): nothing secret, no config values."""
    with tenancy.org_context(tenancy.LEGACY_ORG_ID):
        return [install_status(connector, install) for connector, install in registry.installs()]


# --- disconnect ---------------------------------------------------------------------------


def rearm(install: Install) -> None:
    """Sync ``install`` again now: after a reconnect revives it (new credentials), a job parked in a
    credentials back-off (up to an hour) must not wait it out. Every container is due at once."""
    with tenancy.org_context(install.org):
        _containers.pop(_cache_key(install), None)
        for state in graph.list_states(install):
            fields = {"due_at": 0, "failures": 0}
            if state.get("id") == install.key:
                fields = {"failures": 0, "last_error": None}
            graph.set_state(state["id"], install, **fields)
    schedule(install.source, 0, install)


def remove_install(install_key: str, actor: str | None = None) -> bool:
    """Disconnect a stored install of the org in context: mark it removing, stop its sync, and queue the
    batched deletion of everything it wrote. False when there is no such install, or it is an env install."""
    from src.connectors import store

    stored = store.get(install_key)
    if stored is None or stored.auth == "env":
        return False
    org = tenancy.current_org_ref().id
    store.set_status(install_key, "removing")
    install = registry.drop(org, stored.source, stored.install) or Install(source=stored.source, id=stored.install, org=org, auth=stored.auth)
    cancel(install)
    _containers.pop(_cache_key(install), None)
    scheduler.schedule(0, REMOVE_JOB, {"install": install_key, "actor": actor}, job_id=f"connector:remove:{install_key}")
    return True


def delete_batch(install_key: str, limit: int = REMOVE_BATCH) -> int:
    """Delete up to ``limit`` nodes the install wrote (items, users, containers, state); how many went."""
    _, graph_ = connect()
    rows = graph_.query(
        "MATCH (n {install: $key}) WITH n LIMIT $limit DETACH DELETE n RETURN count(*)", {"key": install_key, "limit": limit}
    ).result_set
    return int(rows[0][0]) if rows else 0


def run_removal(payload: dict) -> None:
    """The removal job (in the install's org): one batch, then the next, then the install itself."""
    from src.connectors import store
    from src.domain import orgs

    install_key = (payload or {}).get("install") or ""
    stored = store.get(install_key)
    if stored is None:
        return
    if stored.status != "removing":
        log.warning("[connectors] removal of %s skipped: it is %s", install_key, stored.status)
        return
    try:
        deleted = delete_batch(install_key)
    except Exception:
        log.exception("[connectors] removal batch failed install=%s; retrying in a minute", install_key)
        scheduler.schedule(60, REMOVE_JOB, payload, job_id=f"connector:remove:{install_key}")
        return
    if deleted >= REMOVE_BATCH:
        scheduler.schedule(0, REMOVE_JOB, payload, job_id=f"connector:remove:{install_key}")
        return
    org = tenancy.current_org_ref().id
    store.delete(install_key)
    # Another install of the org may hold the same account (a duplicate made by a racing callback):
    # the account stays the org's while one does.
    if stored.external_account and not any(row.external_account == stored.external_account for row in store.list_installs()):
        orgs.release_external_account(stored.external_account, org)
    _audit(org, payload.get("actor"), "connector.disconnect", install_key, "ok")
    log.info("[connectors] removed install=%s org=%s", install_key, org)


# --- orphans: data left by an install that is no longer configured --------------------------


def _configured_keys(org: str) -> set[str]:
    from src.connectors import store

    keys = {install.key for _connector, install in registry.installs_of(org)}
    return keys | {row.id for row in store.list_installs()}


def orphans() -> list[dict]:
    """Installs of the org in context that left data in the graph but are configured nowhere (their
    env variables were removed, say). Their items still answer questions until purged."""
    org = tenancy.current_org_ref().id
    configured = _configured_keys(org)
    _, graph_ = connect()
    keys = {
        row[0]
        for label in ("ConnectorState", "Container")
        for row in graph_.query(f"MATCH (n:{label}) WHERE n.install IS NOT NULL RETURN DISTINCT n.install").result_set
    }
    found = []
    for key in sorted(keys - configured):
        state = graph.get_state(key)
        nodes = graph_.query("MATCH (n {install: $key}) RETURN count(n)", {"key": key}).result_set[0][0]
        found.append({"install": key, "source": key.split(":", 1)[0], "nodes": int(nodes), "account": state.get("account"), "last_sync_at": state.get("last_sync_at")})
    return found


def purge_orphan(install_key: str, actor: str | None = None) -> bool:
    """Queue the batched deletion of an orphaned install's data in the org in context. False when the
    install is still configured (disconnect it instead) or left nothing."""
    org = tenancy.current_org_ref().id
    if install_key in _configured_keys(org) or install_key not in {row["install"] for row in orphans()}:
        return False
    scheduler.schedule(0, PURGE_JOB, {"install": install_key, "actor": actor}, job_id=f"connector:purge:{install_key}")
    return True


def run_purge(payload: dict) -> None:
    install_key = (payload or {}).get("install") or ""
    org = tenancy.current_org_ref().id
    if not install_key or install_key in _configured_keys(org):
        log.warning("[connectors] purge of %s skipped: it is configured again", install_key)
        return
    try:
        deleted = delete_batch(install_key)
    except Exception:
        log.exception("[connectors] purge batch failed install=%s; retrying in a minute", install_key)
        scheduler.schedule(60, PURGE_JOB, payload, job_id=f"connector:purge:{install_key}")
        return
    if deleted >= REMOVE_BATCH:
        scheduler.schedule(0, PURGE_JOB, payload, job_id=f"connector:purge:{install_key}")
        return
    _audit(org, payload.get("actor"), "connector.purge", install_key, "ok")
    log.info("[connectors] purged orphaned install=%s org=%s", install_key, org)


def _audit(org: str, actor: str | None, action: str, target: str, result: str) -> None:
    """The console's audit log (control graph). It never raises."""
    from src.domain.console import audit

    audit.record(org, actor or "system", action, target, result)


# --- webhooks -----------------------------------------------------------------------------


def delivery_key(source: str, body: bytes) -> str:
    """A delivery is identified by its body: a sender's retry sends the same bytes."""
    return f"connector:delivery:{source}:{hashlib.sha256(body).hexdigest()}"


def is_duplicate(key: str) -> bool:
    return bool(kv().exists(key))


def handle_delivery(source: str, request: WebhookRequest, key: str) -> dict | None:
    """Write what a verified delivery changes. Marked seen only after it ran, so a redelivery can retry a failure."""
    connector, install = registry.get(source), registry.install_for(source)
    if connector is None or install is None:
        return None
    # Webhooks reach the legacy org's env install only.
    with tenancy.org_context(tenancy.LEGACY_ORG_ID):
        return _deliver(connector, install, source, request, key)


def _deliver(connector: Connector, install: Install, source: str, request: WebhookRequest, key: str) -> dict | None:
    try:
        page = connector.parse_webhook(install, request)
        written = graph.write_page(install, page, MAX_ITEMS_PER_CONTAINER) if page is not None else {"ignored": True}
        kv().set(key, "1", ex=DELIVERY_DEDUP_TTL_SECONDS)
    except Exception as error:
        log.warning("[connectors] %s delivery failed: %s", source, _error(error))
        return None
    log.info("[connectors] %s delivery %s", source, " ".join(f"{name}={value}" for name, value in written.items()))
    return written


scheduler.register(SYNC_JOB, run_sync)
scheduler.register(REMOVE_JOB, run_removal)
scheduler.register(PURGE_JOB, run_purge)
