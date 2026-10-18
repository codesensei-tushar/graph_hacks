"""The one writer for connector data, and the reads that decide who may see it.

    (:Container {id, name, scope, source, install, restricted})
    (item:Ticket|Service|Document {id, ..., source, install, scope, synced_at})-[:IN_CONTAINER]->(:Container)
    (item)-[:ABOUT {item}]->(:Service)                   Item.about hints, resolved here
    (:ExternalUser {id, name, email, scope})-[:IS {via: 'email'}]->(:Person)
    (:ExternalUser or :Person)-[:OPENED|ASSIGNED|OWNS|WORKED_ON {item, via}]->(item)
    (item)-[:BLOCKS {item}]->(:Ticket)
    (:ExternalUser)-[:CAN_READ {install}]->(:Container)  restricted containers only
    (:PullRequest)-[:REFERENCES|CLOSES {source}]->(:Ticket)
    (:ConnectorState {id, install, scope: 'system', ...})  sync bookkeeping, nothing secret

Every id must belong to the install (``<source>:<install>:``), so nothing here can MERGE
onto or SET a property of a seeded, team-imported, Slack or GitHub node: those are only
ever the far end of an edge. Every write is a MERGE on ``id``, so a page written twice
leaves the graph as it was. Labels and relationship types come from the fixed sets in
``base.py``; every value is a parameter.
"""

import hashlib
import logging
import re
import time

from src.connectors.base import EDGE_RELS, ITEM_LABELS, ORG_INTERNAL, PERSON_RELS, STATES, Container, ExternalUser, Install, Item, Page
from src.db.falkordb_client import connect
from src.domain.knowledge import kinds, resolve
from src.domain.memory.containers import CONNECTOR_SCOPE_PREFIX

log = logging.getLogger(__name__)

SUMMARY_MAX_CHARS = 2000
# Bookkeeping nodes are in a scope no read set holds.
STATE_SCOPE = "system"
# Properties graph.py writes itself, and those other tools read with a meaning of their own
# (``number`` is how a pull request or GitHub issue is looked up by ``#12``).
RESERVED_PROPS = frozenset(
    {
        "id", "name", "title", "summary", "url", "state", "status", "source_updated_at", "external_id", "kind",
        "source", "install", "scope", "synced_at", "aliases", "paths", "number", "restricted", "email", "login",
    }
)
# Labels an edge may join; read back from the graph, so checked before they go into a query.
_EDGE_END_LABELS = ITEM_LABELS | {"ExternalUser"}
# Edge properties graph.py writes itself: which item owns the edge, where it came from, and the
# account it was written for. ``state`` is a review's, read by the GitHub tools.
RESERVED_EDGE_PROPS = frozenset({"item", "source", "install", "via", "user", "state"})
_PROP_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# Where a pull request keeps the ticket handles it names, as "<source>:<HANDLE>", per relationship.
_REFERENCE_PROPS = {"REFERENCES": "ticket_references", "CLOSES": "ticket_closes"}


def now_ms() -> int:
    return int(time.time() * 1000)


def restricted_scope(container_id: str) -> str:
    return f"{CONNECTOR_SCOPE_PREFIX}{hashlib.sha256(container_id.encode()).hexdigest()[:16]}"


def ensure_indexes() -> None:
    _, graph = connect()
    for label in (*sorted(ITEM_LABELS), "Container", "ExternalUser", "ConnectorState"):
        try:
            graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.id)")
        except Exception as error:  # already exists
            if "already" not in str(error).lower():
                raise


def _owned(install: Install, node_id: str) -> bool:
    return isinstance(node_id, str) and node_id.startswith(f"{install.key}:")


def _check(install: Install, node_id: str) -> str:
    if not _owned(install, node_id):
        raise ValueError(f"id {node_id!r} is not in install {install.key}")
    return node_id


def _props(props, reserved: frozenset[str] = RESERVED_PROPS) -> dict:
    """Extra properties a connector may set: plain names not taken, plain values."""
    kept = {}
    for name, value in (props or {}).items():
        if not _PROP_NAME.match(str(name)) or name in reserved:
            log.warning("[connectors] property %r refused", name)
            continue
        if isinstance(value, list):
            value = [item for item in value if isinstance(item, (str, int, float, bool))]
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            value = str(value)
        if value is not None:
            kept[name] = value
    return kept


def _clip(text: str | None, limit: int = SUMMARY_MAX_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- containers -------------------------------------------------------------------------


def write_containers(install: Install, containers: list[Container]) -> list[str]:
    """Write the install's containers; remove any it had that are not in the list, with their items."""
    _, graph = connect()
    ids = []
    for container in containers:
        _check(install, container.id)
        scope = restricted_scope(container.id) if container.restricted else ORG_INTERNAL
        graph.query(
            """
            MERGE (c:Container {id: $id})
            SET c.name = $name, c.external_id = $external_id, c.url = $url, c.kind = $kind, c.source = $source,
                c.install = $install, c.scope = $scope, c.restricted = $restricted, c.synced_at = $now
            WITH c
            MATCH (n)-[:IN_CONTAINER]->(c)
            WHERE n.scope <> c.scope
            SET n.scope = c.scope
            """,
            {
                "id": container.id, "name": container.name, "external_id": str(container.external_id), "url": container.url,
                "kind": container.kind, "source": install.source, "install": install.key, "scope": scope,
                "restricted": bool(container.restricted), "now": now_ms(),
            },
        )
        ids.append(container.id)
    stale = graph.query("MATCH (c:Container {install: $install}) WHERE NOT c.id IN $ids RETURN c.id", {"install": install.key, "ids": ids}).result_set
    for (container_id,) in stale:
        remove_container(install, container_id)
    return ids


def remove_container(install: Install, container_id: str) -> int:
    """The container left the allow-list or the source: drop it, everything in it, and its state."""
    _check(install, container_id)
    _, graph = connect()
    removed = graph.query("MATCH (n)-[:IN_CONTAINER]->(:Container {id: $id}) DETACH DELETE n", {"id": container_id}).nodes_deleted
    graph.query("MATCH (c:Container {id: $id}) DETACH DELETE c", {"id": container_id})
    graph.query("MATCH (s:ConnectorState {id: $id}) DELETE s", {"id": container_id})
    log.info("[connectors] removed container=%s items=%d", container_id, removed)
    return int(removed)


def container_scopes(install: Install) -> dict[str, str]:
    """Container id -> scope, for the install's containers in the graph."""
    _, graph = connect()
    rows = graph.query("MATCH (c:Container {install: $install}) RETURN c.id, c.scope", {"install": install.key}).result_set
    return dict(rows)


def count_items(container_id: str) -> int:
    _, graph = connect()
    rows = graph.query("MATCH (n)-[:IN_CONTAINER]->(:Container {id: $id}) RETURN count(n)", {"id": container_id}).result_set
    return int(rows[0][0]) if rows else 0


# --- users ------------------------------------------------------------------------------


def upsert_user(install: Install, user: ExternalUser) -> str:
    """Write the account and link it to the one Person with its email, or to nobody.

    OWNS and WORKED_ON edges move with the link: from the Person while linked, from the
    account otherwise, so who_owns sees an owner as soon as the company knows them.
    """
    _check(install, user.id)
    email = (user.email or "").strip().lower()
    _, graph = connect()
    graph.query(
        """
        MERGE (u:ExternalUser {id: $id})
        SET u.name = $name, u.source = $source, u.install = $install, u.external_id = $external_id,
            u.email = $email, u.scope = $scope, u.synced_at = $now
        """,
        {
            "id": user.id, "name": user.name or user.external_id, "source": install.source, "install": install.key,
            "external_id": str(user.external_id), "email": email, "scope": ORG_INTERNAL, "now": now_ms(),
        },
    )
    matches = graph.query("MATCH (p:Person) WHERE $email <> '' AND toLower(p.email) = $email RETURN p.id LIMIT 2", {"email": email}).result_set
    person = matches[0][0] if len(matches) == 1 else None
    current = graph.query("MATCH (:ExternalUser {id: $id})-[r:IS]->(p:Person) WHERE r.via = 'email' RETURN p.id", {"id": user.id}).result_set
    linked = current[0][0] if current else None
    if linked == person:
        return user.id
    graph.query("MATCH (:ExternalUser {id: $id})-[r:IS]->(:Person) WHERE r.via = 'email' DELETE r", {"id": user.id})
    if person is not None:
        graph.query("MATCH (u:ExternalUser {id: $id}), (p:Person {id: $person}) MERGE (u)-[r:IS]->(p) SET r.via = 'email'", {"id": user.id, "person": person})
    _move_person_edges(graph, user.id, person)
    return user.id


def _move_person_edges(graph, user_id: str, person_id: str | None) -> None:
    target_label, target = ("Person", person_id) if person_id else ("ExternalUser", user_id)
    for rel in sorted(PERSON_RELS):
        graph.query(
            f"""
            MATCH (holder)-[r:{rel}]->(n)
            WHERE r.via = $user AND holder.id <> $target
            MATCH (t:{target_label} {{id: $target}})
            MERGE (t)-[moved:{rel}]->(n)
            SET moved.item = r.item, moved.source = r.source, moved.install = r.install, moved.via = r.via
            DELETE r
            """,
            {"user": user_id, "target": target},
        )


def _person_of(graph, user_id: str) -> str | None:
    rows = graph.query("MATCH (:ExternalUser {id: $id})-[:IS]->(p:Person) RETURN p.id LIMIT 1", {"id": user_id}).result_set
    return rows[0][0] if rows else None


# --- pages ------------------------------------------------------------------------------


def write_page(install: Install, page: Page, max_items_per_container: int | None = None) -> dict:
    """Write one page: users, then items with the edges they own, then deletions. Returns counts.

    An item whose container is not one of the install's is dropped (the containers are the
    allow-list). With ``max_items_per_container``, a new item past that many in its container
    is dropped too; items already there still update.
    """
    _, graph = connect()
    report = {"items": 0, "users": 0, "edges": 0, "deleted": 0, "skipped": 0}
    for user in page.users:
        upsert_user(install, user)
        report["users"] += 1

    scopes = container_scopes(install)
    existing = set()
    if page.items:
        existing = {row[0] for row in graph.query("MATCH (n) WHERE n.id IN $ids RETURN n.id", {"ids": [item.id for item in page.items]}).result_set}
    room: dict[str, int] = {}
    services = resolve.load_rows(graph, ("Service",), [resolve.ORG_SHARED, ORG_INTERNAL])
    written: set[str] = set()
    for item in page.items:
        _check(install, item.id)
        if item.label not in ITEM_LABELS or item.state not in STATES:
            raise ValueError(f"item {item.id}: label must be one of {sorted(ITEM_LABELS)} and state one of {STATES}")
        if item.container not in scopes:
            report["skipped"] += 1
            continue
        if max_items_per_container is not None and item.id not in existing:
            if item.container not in room:
                room[item.container] = max_items_per_container - count_items(item.container)
            if room[item.container] <= 0:
                report["skipped"] += 1
                continue
            room[item.container] -= 1
        _upsert_item(graph, install, item, scopes[item.container], services)
        written.add(item.id)
        report["items"] += 1

    item_ids = {item.id for item in page.items}
    for edge in page.edges:
        if edge.rel not in EDGE_RELS:
            raise ValueError(f"edge {edge.rel} is not one a connector may write")
        for end in (edge.from_id, edge.to_id):
            if _check(install, end).startswith(f"{install.key}:container:"):
                raise ValueError(f"edge {edge.rel} may not touch a container: {end}")
    # The label of every end, read once: a MATCH without a label scans every node.
    ends = sorted({end for edge in page.edges for end in (edge.from_id, edge.to_id)})
    label_of = dict(graph.query("MATCH (n) WHERE n.id IN $ids RETURN n.id, labels(n)[0]", {"ids": ends}).result_set) if ends else {}
    for edge in page.edges:
        owner = edge.to_id if _is_user(install, edge.from_id) else edge.from_id
        if owner in item_ids and owner not in written:
            continue  # its item was dropped
        if edge.from_id in label_of and edge.to_id in label_of and _write_edge(graph, install, edge, owner, label_of):
            report["edges"] += 1

    if written:
        _link_waiting_references(graph, install.source, [item for item in page.items if item.id in written and item.label == "Ticket"])
    report["deleted"] = delete_items(install, page.deleted_ids)
    return report


def _is_user(install: Install, node_id: str) -> bool:
    return node_id.startswith(f"{install.key}:user:")


def _upsert_item(graph, install: Install, item: Item, scope: str, services: list) -> None:
    label = item.label  # one of ITEM_LABELS, checked by the caller
    kind = kinds.normalize_kind(item.kind) if label == "Service" else None
    graph.query(
        f"""
        MERGE (n:{label} {{id: $id}})
        SET n.name = $name, n.title = $title, n.summary = $summary, n.url = $url, n.state = $state, n.status = $status,
            n.source_updated_at = $updated, n.external_id = $external_id, n.source = $source, n.install = $install,
            n.scope = $scope, n.synced_at = $now
        SET n += $props
        """,
        {
            "id": item.id, "name": item.name, "title": item.title or item.name, "summary": _clip(item.summary), "url": item.url,
            "state": item.state, "status": item.status, "updated": item.updated_at, "external_id": str(item.external_id),
            "source": install.source, "install": install.key, "scope": scope, "now": now_ms(), "props": _props(item.props),
        },
    )
    if kind is not None:
        graph.query("MATCH (n:Service {id: $id}) SET n.kind = $kind", {"id": item.id, "kind": kind})
    graph.query(f"MATCH (:{label} {{id: $id}})-[old:IN_CONTAINER]->(c:Container) WHERE c.id <> $container DELETE old", {"id": item.id, "container": item.container})
    graph.query(f"MATCH (n:{label} {{id: $id}}), (c:Container {{id: $container}}) MERGE (n)-[:IN_CONTAINER]->(c)", {"id": item.id, "container": item.container})
    # The edges this item owns are replaced by the ones this page carries.
    graph.query(f"MATCH (n:{label} {{id: $id}})-[r]-() WHERE r.item = $id DELETE r", {"id": item.id})
    for service_id in _about(item, services):
        graph.query(
            f"""
            MATCH (n:{label} {{id: $id}}), (s:Service {{id: $service}})
            MERGE (n)-[r:ABOUT]->(s)
            SET r.item = $id, r.source = $source, r.install = $install
            """,
            {"id": item.id, "service": service_id, "source": install.source, "install": install.key},
        )


def _about(item: Item, services: list) -> list[str]:
    """Each Service a hint names outright; else the best guess over all hints, as GitHub issues get."""
    hints = [hint for hint in item.about if hint and hint.strip()]
    found = []
    for hint in hints:
        entity = resolve.resolve_rows(hint, services, ("Service",)).entity
        if entity is not None and entity.id != item.id and entity.id not in found:
            found.append(entity.id)
    if not found and hints:
        guess = resolve.best_match_rows(" ".join(hints), services, ("Service",))
        if guess is not None and guess.id != item.id:
            found.append(guess.id)
    return found


def _write_edge(graph, install: Install, edge, owner: str, label_of: dict[str, str]) -> bool:
    rel = edge.rel  # one of EDGE_RELS, checked by the caller
    start_label, end_label = label_of[edge.from_id], label_of[edge.to_id]
    if not {start_label, end_label} <= _EDGE_END_LABELS:
        return False
    start, via = edge.from_id, None
    if start_label == "ExternalUser":
        via = edge.from_id
        if rel in PERSON_RELS and (person := _person_of(graph, edge.from_id)):
            start_label, start = "Person", person
    rows = graph.query(
        f"""
        MATCH (a:{start_label} {{id: $from}}), (b:{end_label} {{id: $to}})
        MERGE (a)-[r:{rel}]->(b)
        SET r.item = $item, r.source = $source, r.install = $install, r.via = $via
        SET r += $props
        RETURN count(r)
        """,
        {"from": start, "to": edge.to_id, "item": owner, "source": install.source, "install": install.key, "via": via, "props": _props(edge.props, RESERVED_EDGE_PROPS)},
    ).result_set
    return bool(rows and rows[0][0])


def delete_items(install: Install, ids: list[str]) -> int:
    ids = [_check(install, node_id) for node_id in ids or []]
    if not ids:
        return 0
    _, graph = connect()
    deleted = 0
    for label in sorted(ITEM_LABELS):
        deleted += graph.query(f"MATCH (n:{label}) WHERE n.id IN $ids DETACH DELETE n", {"ids": ids}).nodes_deleted
    return int(deleted)


# --- who may read a restricted container --------------------------------------------------


def write_readers(install: Install, container_id: str, users: list[ExternalUser] | None) -> int:
    """Replace the container's CAN_READ edges. None (readers unknown) leaves nobody: fails closed."""
    _check(install, container_id)
    ids = [upsert_user(install, user) for user in users or []]
    _, graph = connect()
    graph.query("MATCH (u:ExternalUser)-[r:CAN_READ]->(:Container {id: $id}) WHERE NOT u.id IN $ids DELETE r", {"id": container_id, "ids": ids})
    for user_id in ids:
        graph.query(
            "MATCH (u:ExternalUser {id: $user}), (c:Container {id: $id}) MERGE (u)-[r:CAN_READ]->(c) SET r.install = $install",
            {"user": user_id, "id": container_id, "install": install.key},
        )
    return len(ids)


def readable_container_scopes_for_slack_user(slack_user_id: str | None) -> list[str]:
    """Restricted container scopes this Slack user may read: linked account x reader. Fails closed."""
    if not (slack_user_id or "").strip():
        return []
    try:
        _, graph = connect()
        rows = graph.query(
            f"""
            MATCH (:SlackUser {{id: $user}})-[:IS]->(:Person)<-[:IS]-(:ExternalUser)-[:CAN_READ]->(c:Container)
            WHERE c.restricted = true AND c.scope STARTS WITH '{CONNECTOR_SCOPE_PREFIX}'
            RETURN DISTINCT c.scope
            """,
            {"user": slack_user_id},
        ).result_set
    except Exception:
        log.exception("[connectors] readable container lookup failed user=%s", slack_user_id)
        return []
    return [row[0] for row in rows]


def container_names(scopes: list[str]) -> dict[str, str]:
    """Restricted scope -> the container's name, for citing a scope back to the asker."""
    wanted = [scope for scope in scopes if scope.startswith(CONNECTOR_SCOPE_PREFIX)]
    if not wanted:
        return {}
    _, graph = connect()
    rows = graph.query("MATCH (c:Container) WHERE c.scope IN $scopes RETURN c.scope, c.name", {"scopes": wanted}).result_set
    return dict(rows)


# --- pull requests that name tickets ------------------------------------------------------


def link_references(pull_request_id: str, handles: list[str], source: str, rel: str = "REFERENCES") -> int:
    """``(:PullRequest)-[:REFERENCES|CLOSES]->(:Ticket)`` for the ``source`` tickets ``handles`` name
    (``["PAY-12"]``), replacing that pull request's earlier ones of that source and relationship.

    The handles are kept on the pull request, so a ticket synced later links back when it is
    written (``write_page``). Returns how many tickets are linked now.
    """
    if rel not in _REFERENCE_PROPS:
        raise ValueError(f"rel must be one of {sorted(_REFERENCE_PROPS)}")
    prop = _REFERENCE_PROPS[rel]
    wanted = sorted({handle.strip().upper() for handle in handles if handle and handle.strip()})
    _, graph = connect()
    rows = graph.query(f"MATCH (p:PullRequest {{id: $id}}) RETURN coalesce(p.{prop}, [])", {"id": pull_request_id}).result_set
    if not rows:
        return 0
    kept = [ref for ref in rows[0][0] if not str(ref).startswith(f"{source}:")]
    graph.query(f"MATCH (p:PullRequest {{id: $id}}) SET p.{prop} = $refs", {"id": pull_request_id, "refs": kept + [f"{source}:{handle}" for handle in wanted]})
    graph.query(
        f"MATCH (:PullRequest {{id: $id}})-[r:{rel}]->(t:Ticket) WHERE t.source = $source AND NOT toUpper(t.name) IN $handles DELETE r",
        {"id": pull_request_id, "source": source, "handles": wanted},
    )
    linked = graph.query(
        f"""
        MATCH (p:PullRequest {{id: $id}}), (t:Ticket)
        WHERE t.source = $source AND toUpper(t.name) IN $handles
        MERGE (p)-[r:{rel}]->(t)
        SET r.source = $source
        RETURN count(DISTINCT t)
        """,
        {"id": pull_request_id, "source": source, "handles": wanted},
    ).result_set
    return int(linked[0][0]) if linked else 0


def _link_waiting_references(graph, source: str, tickets: list[Item]) -> None:
    """Pull requests that named these tickets before they were synced."""
    if not tickets:
        return
    pairs = [[f"{source}:{ticket.name.strip().upper()}", ticket.id] for ticket in tickets]
    for rel, prop in sorted(_REFERENCE_PROPS.items()):
        graph.query(
            f"""
            UNWIND $pairs AS pair
            MATCH (p:PullRequest)
            WHERE pair[0] IN coalesce(p.{prop}, [])
            MATCH (t:Ticket {{id: pair[1]}})
            MERGE (p)-[r:{rel}]->(t)
            SET r.source = $source
            """,
            {"pairs": pairs, "source": source},
        )


# --- sync bookkeeping ---------------------------------------------------------------------


def get_state(state_id: str) -> dict:
    _, graph = connect()
    rows = graph.query("MATCH (s:ConnectorState {id: $id}) RETURN properties(s)", {"id": state_id}).result_set
    return dict(rows[0][0]) if rows else {}


def set_state(state_id: str, install: Install, **fields) -> None:
    """Merge ``fields`` into the state node. A None value removes the property."""
    _, graph = connect()
    graph.query(
        "MERGE (s:ConnectorState {id: $id}) SET s.install = $install, s.source = $source, s.scope = $scope SET s += $fields",
        {"id": state_id, "install": install.key, "source": install.source, "scope": STATE_SCOPE, "fields": fields},
    )


def list_states(install: Install) -> list[dict]:
    _, graph = connect()
    rows = graph.query("MATCH (s:ConnectorState {install: $install}) RETURN properties(s) ORDER BY s.id", {"install": install.key}).result_set
    return [dict(row[0]) for row in rows]
