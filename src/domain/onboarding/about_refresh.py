"""Re-resolve what synced items are ABOUT once the org's Services exist.

A connector item's ``(item)-[:ABOUT]->(:Service)`` edges are resolved from its ``about`` hints
when ``connectors/graph.py`` writes it (Jira: the issue's components, else its title). A
company that connects Jira before it imports its teams has no Services yet, so every ticket
is about nothing until the next change to it. After a team import or ``onboard team:`` writes
Services, ``schedule()`` queues this job in the org, and it walks every connector item in
batches and resolves its hints again, against the same Services and with the same rule
(``graph._about``), so the edges are the ones a fresh sync would write.

The hints are read from the item: ``about_hints`` when graph.py stored them, else what the
mapper made them from (a Jira ticket's stored ``components``, else its ``title``). An item
whose resolved set is unchanged is not touched. Only ABOUT edges the item owns
(``r.item = item.id``) are replaced; nothing else is written.
"""

import logging
from types import SimpleNamespace

from src.connectors import graph as connector_graph
from src.connectors.base import ITEM_LABELS, ORG_INTERNAL
from src.core import scheduler
from src.db.falkordb_client import connect
from src.domain.knowledge import resolve

log = logging.getLogger(__name__)

JOB = "onboardingAboutRefresh"
JOB_ID = "onboarding:about-refresh"
BATCH = 200
DELAY_SECONDS = 2.0
_LABELS = tuple(sorted(ITEM_LABELS))


def schedule(delay_seconds: float = DELAY_SECONDS) -> None:
    """Queue a full pass in the org in context. A pass already queued is replaced, so a burst of
    imports runs one pass, from the start, after the last one."""
    scheduler.schedule(delay_seconds, JOB, {"label": _LABELS[0], "after": ""}, job_id=JOB_ID)


def hints(about_hints, components, title, source) -> list[str]:
    """An item's hints: the stored ones, else the Jira mapper's (components, else the title)."""
    if about_hints is not None:
        return [str(hint) for hint in about_hints if str(hint or "").strip()]
    if source == "jira":
        found = [str(part) for part in components or [] if str(part or "").strip()]
        return found or ([title] if (title or "").strip() else [])
    return []


def refresh_batch(label: str, after: str, batch: int = BATCH) -> tuple[int, str | None]:
    """Re-resolve up to ``batch`` items of ``label`` with ids after ``after``. Returns (items
    changed, the last id read, or None when the label is done)."""
    if label not in ITEM_LABELS:
        raise ValueError(f"label must be one of {_LABELS}")
    _, graph = connect()
    rows = graph.query(
        f"""
        MATCH (n:{label})
        WHERE n.install IS NOT NULL AND n.external_id IS NOT NULL AND n.id > $after
        RETURN n.id, n.about_hints, n.components, n.title, n.source, n.install
        ORDER BY n.id LIMIT $batch
        """,
        {"after": after, "batch": batch},
    ).result_set
    if not rows:
        return 0, None
    services = resolve.load_rows(graph, ("Service",), [resolve.ORG_SHARED, ORG_INTERNAL])
    ids = [row[0] for row in rows]
    current: dict[str, set[str]] = {}
    for item_id, service_id in graph.query(
        f"MATCH (n:{label})-[r:ABOUT]->(s:Service) WHERE n.id IN $ids AND r.item = n.id RETURN n.id, s.id", {"ids": ids}
    ).result_set:
        current.setdefault(item_id, set()).add(service_id)
    changed = 0
    for item_id, about_hints, components, title, source, install in rows:
        found = hints(about_hints, components, title, source)
        wanted = connector_graph._about(SimpleNamespace(id=item_id, about=tuple(found)), services) if found else []
        if set(wanted) == current.get(item_id, set()):
            continue
        graph.query(f"MATCH (n:{label} {{id: $id}})-[r:ABOUT]->(:Service) WHERE r.item = $id DELETE r", {"id": item_id})
        if wanted:
            graph.query(
                f"""
                MATCH (n:{label} {{id: $id}})
                UNWIND $services AS service
                MATCH (s:Service {{id: service}})
                MERGE (n)-[r:ABOUT]->(s)
                SET r.item = $id, r.source = $source, r.install = $install
                """,
                {"id": item_id, "services": wanted, "source": source, "install": install},
            )
        changed += 1
    return changed, (ids[-1] if len(rows) == batch else None)


def run(payload: dict) -> None:
    """The scheduler job: one batch, then the next one queued (the next label when this one is done)."""
    label = payload.get("label") if payload.get("label") in ITEM_LABELS else _LABELS[0]
    changed, last = refresh_batch(label, str(payload.get("after") or ""), BATCH)
    if changed:
        log.info("[onboarding] ABOUT re-resolved label=%s items=%d", label, changed)
    if last is not None:
        scheduler.schedule(0, JOB, {"label": label, "after": last}, job_id=JOB_ID)
    elif label != _LABELS[-1]:
        scheduler.schedule(0, JOB, {"label": _LABELS[_LABELS.index(label) + 1], "after": ""}, job_id=JOB_ID)


def refresh_all() -> int:
    """Every batch, now, in the org in context. For tests and the operator; the app uses the job."""
    total = 0
    for label in _LABELS:
        after: str | None = ""
        while after is not None:
            changed, after = refresh_batch(label, after)
            total += changed
    return total


scheduler.register(JOB, run)
