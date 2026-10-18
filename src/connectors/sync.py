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
from src.core import scheduler
from src.db.falkordb_client import kv

log = logging.getLogger(__name__)

SYNC_JOB = "runConnectorSync"
# A container keeps at most this many items; past it, new items are dropped and existing ones update.
MAX_ITEMS_PER_CONTAINER = 1000
MORE_DELAY_SECONDS = 2.0
CONTAINERS_REFRESH_SECONDS = 60 * 60
CREDENTIALS_BACKOFF_SECONDS = 60 * 60
FAILURE_BACKOFF_SECONDS = (60, 5 * 60, 15 * 60, 60 * 60)
DELIVERY_DEDUP_TTL_SECONDS = 60 * 60 * 24
ERROR_MAX_CHARS = 300

# Container objects as the connector last listed them, by install key. Rebuilt after a restart.
_containers: dict[str, tuple[float, list[Container]]] = {}


def job_id(source: str) -> str:
    return f"connector:sync:{source}"


def max_delay(source: str) -> float:
    """The longest a step may be put off: an hour, or the connector's poll interval when longer."""
    connector = registry.get(source)
    return float(max(MAX_RETRY_AFTER_SECONDS, CREDENTIALS_BACKOFF_SECONDS, *FAILURE_BACKOFF_SECONDS, connector.poll_seconds if connector else 0))


def schedule(source: str, delay: float = 0.0) -> None:
    delay = min(max(0.0, float(delay)), max_delay(source))
    scheduler.schedule(delay, SYNC_JOB, {"source": source}, job_id=job_id(source))


def schedule_all() -> list[str]:
    """Arm every configured install that has no step pending, or one put off further than any
    step may be (written before the clamp, or by a clock that jumped). Startup."""
    armed = []
    for connector, _install in registry.installs():
        due = scheduler.due_at(job_id(connector.source))
        if due is None or due - time.time() > max_delay(connector.source):
            schedule(connector.source, 0)
            armed.append(connector.source)
    return armed


def run_sync(payload: dict) -> None:
    """The scheduler job: one step, then always the next one while the install is configured."""
    source = (payload or {}).get("source") or ""
    delay = FAILURE_BACKOFF_SECONDS[0]
    try:
        delay = sync_step(source).get("next_in", delay)
    except Exception:
        log.exception("[connectors] %s sync step failed", source)
    finally:
        if registry.install_for(source) is not None:
            schedule(source, delay)


def _error(error: BaseException) -> str:
    text = str(error) if isinstance(error, ConnectorError) else f"{type(error).__name__}: {error}"
    return text[:ERROR_MAX_CHARS]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _backoff(failures: int) -> float:
    return float(FAILURE_BACKOFF_SECONDS[min(max(failures, 1), len(FAILURE_BACKOFF_SECONDS)) - 1])


def sync_step(source: str) -> dict:
    """One step for ``source``. Returns what happened and ``next_in``, the seconds until the next step."""
    connector, install = registry.get(source), registry.install_for(source)
    if connector is None or install is None:
        return {"status": "not_configured"}
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
    states = {container.id: graph.get_state(container.id) for container in containers}
    container = _next_container(containers, states, now)
    if container is None:
        return {"status": "idle", "next_in": _next_in(states, now, connector)}
    report = _fetch_one(connector, install, container, states[container.id], failures)
    states[container.id] = graph.get_state(container.id)
    report["next_in"] = _next_in(states, _now_ms(), connector)
    return report


def _current_containers(connector: Connector, install: Install) -> list[Container]:
    cached = _containers.get(install.key)
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
    _containers[install.key] = (time.time(), containers)
    graph.set_state(install.key, install, account=info.account, site=info.site, containers_at=_now_ms(), containers=len(containers), last_error=None)
    log.info("[connectors] %s containers=%d account=%s", install.key, len(containers), info.account)
    return containers


def _next_container(containers: list[Container], states: dict[str, dict], now: int) -> Container | None:
    """The due container fetched least recently, whether it has pages waiting or its poll came round."""
    due = [container for container in containers if int(states[container.id].get("due_at") or 0) <= now]
    if not due:
        return None
    return min(due, key=lambda container: (int(states[container.id].get("last_fetch_at") or 0), int(states[container.id].get("due_at") or 0)))


def _next_in(states: dict[str, dict], now: int, connector: Connector) -> float:
    if not states:
        return float(connector.poll_seconds)
    soonest = min(int(state.get("due_at") or 0) for state in states.values())
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
        _containers.pop(install.key, None)  # check the credentials again next time
        return {"status": "credentials_rejected", "container": container.id}
    except Exception as error:
        container_failures = int(state.get("failures") or 0) + 1
        graph.set_state(container.id, install, due_at=now + int(_backoff(container_failures) * 1000), last_error=_error(error), failures=container_failures)
        graph.set_state(install.key, install, last_error=_error(error), failures=failures + 1)
        log.warning("[connectors] fetch failed container=%s: %s", container.id, _error(error))
        return {"status": "failed", "container": container.id}

    # At the cap a backfill ends: paging on would only drop what it reads. The cursor is kept.
    capped = graph.count_items(container.id) >= MAX_ITEMS_PER_CONTAINER
    more = bool(page.has_more) and not capped
    wait = max(MORE_DELAY_SECONDS if more else float(connector.poll_seconds), clamp_wait(page.retry_after or 0))
    items = int(state.get("items") or 0) + written["items"]
    graph.set_state(
        container.id,
        install,
        cursor=page.next_cursor,
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


def sync_now(source: str, full: bool = False) -> bool:
    """Make every container due and run a step on the next scheduler poll. ``full`` starts each one over."""
    install = registry.install_for(source)
    if install is None:
        return False
    _containers.pop(install.key, None)
    for state in graph.list_states(install):
        if state.get("id") == install.key:
            continue
        fields = {"due_at": 0, "failures": 0}
        if full:
            fields.update(cursor=None, has_more=False, backfilled=False)
        graph.set_state(state["id"], install, **fields)
    schedule(source, 0)
    return True


def status() -> list[dict]:
    """What the admin status route shows: nothing secret, no config values."""
    rows = []
    for connector, install in registry.installs():
        state = graph.get_state(install.key)
        names = {container.id: container.name for container in (_containers.get(install.key) or (0, []))[1]}
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
        due = scheduler.due_at(job_id(connector.source))
        rows.append(
            {
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
        )
    return rows


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
