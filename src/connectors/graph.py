"""The one writer for connector data, and the reads that decide who may see it.

    (:Container {id, name, scope, source, install, restricted})
    (item:Ticket|Service|Document {id, ..., changed_at_ms, content_hash, source, install, scope, synced_at})
        -[:IN_CONTAINER]->(:Container)
    (item)-[:ABOUT {item}]->(:Service)                   Item.about hints, resolved here
    (:ExternalUser {id, name, email, email_lc, email_from, account_type, scope})-[:IS {via: 'email'|'admin'}]->(:Person)
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
import json
import logging
import re
import time
from datetime import UTC, datetime

from src.connectors.base import EDGE_RELS, ITEM_LABELS, ORG_INTERNAL, PERSON_RELS, STATES, Container, ExternalUser, Install, Item, Page
from src.db.falkordb_client import connect, deferred_generation
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
# Part of every content hash: raise one when what is written for an item or an account changes, so
# the next sync writes everything once more instead of skipping it as unchanged.
ITEM_HASH_VERSION = 1
USER_HASH_VERSION = 1
# "+0000" at the end of a Jira timestamp, which ``datetime.fromisoformat`` reads only as "+00:00".
_TZ_NO_COLON = re.compile(r"([+-])(\d{2})(\d{2})$")


def now_ms() -> int:
    return int(time.time() * 1000)


def restricted_scope(container_id: str) -> str:
    return f"{CONNECTOR_SCOPE_PREFIX}{hashlib.sha256(container_id.encode()).hexdigest()[:16]}"


def ensure_indexes() -> None:
    _, graph = connect()
    wanted = [(label, "id") for label in (*sorted(ITEM_LABELS), "Container", "ExternalUser", "ConnectorState")]
    # Accounts link to people on the lower-cased email (``upsert_users``), never on a toLower() scan.
    wanted += [("ExternalUser", "email_lc"), ("Person", "email_lc")]
    for label, prop in wanted:
        try:
            graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
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
                c.install = $install, c.scope = $scope, c.restricted = $restricted, c.available = $available, c.synced_at = $now
            WITH c
            MATCH (n)-[:IN_CONTAINER]->(c)
            WHERE n.scope <> c.scope
            SET n.scope = c.scope
            """,
            {
                "id": container.id, "name": container.name, "external_id": str(container.external_id), "url": container.url,
                "kind": container.kind, "source": install.source, "install": install.key, "scope": scope,
                "restricted": bool(container.restricted), "available": container.available, "now": now_ms(),
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


def container_facts(install: Install) -> dict[str, dict]:
    """Container id -> ``{readers, available, name}`` for the install's containers, in one query. ``readers``
    is None for a company-wide container, else how many linked accounts may read it (CAN_READ);
    ``available`` is the vendor's own count when the listing gave one."""
    _, graph = connect()
    rows = graph.ro_query(
        """
        MATCH (c:Container {install: $install})
        OPTIONAL MATCH (u:ExternalUser)-[:CAN_READ]->(c)
        RETURN c.id, c.restricted, count(u), c.available, c.name
        """,
        {"install": install.key},
    ).result_set
    return {
        cid: {"readers": int(readers) if restricted else None, "available": available, "name": name}
        for cid, restricted, readers, available, name in rows
    }


def count_items(container_id: str) -> int:
    _, graph = connect()
    rows = graph.query("MATCH (n)-[:IN_CONTAINER]->(:Container {id: $id}) RETURN count(n)", {"id": container_id}).result_set
    return int(rows[0][0]) if rows else 0


# --- users ------------------------------------------------------------------------------


def upsert_user(install: Install, user: ExternalUser) -> str:
    """Write the account and link it to the one Person with its email, or to nobody (``upsert_users``)."""
    upsert_users(install, [user])
    return user.id


def upsert_users(install: Install, users: list[ExternalUser]) -> dict[str, str | None]:
    """Write the accounts and their links, in a few queries for the whole list. Returns each account's
    Person id (None when linked to nobody).

    An admin's link (``IS {via: 'admin'}``, from the console's People screen) wins and is never undone
    by a sync: the account's email link is dropped instead. Otherwise the account links to the one
    Person whose email is its email (case-insensitive, through ``email_lc``), or to nobody.

    An account whose fields and link are as stored is not written at all. When the link changes,
    OWNS and WORKED_ON move with it: from the Person while linked, from the account otherwise, so
    who_owns sees an owner as soon as the company knows them.
    """
    users = list({user.id: user for user in users}.values())
    if not users:
        return {}
    for user in users:
        _check(install, user.id)
    _, graph = connect()
    stored = {
        row[0]: (row[1], [(via, person) for via, person in row[2] if person is not None])
        for row in graph.ro_query(
            """
            UNWIND $ids AS id
            MATCH (u:ExternalUser {id: id})
            OPTIONAL MATCH (u)-[r:IS]->(p:Person)
            RETURN id, u.content_hash, collect([r.via, p.id])
            """,
            {"ids": [user.id for user in users]},
        ).result_set
    }
    emails = sorted({_email(user) for user in users} - {""})
    by_email: dict[str, list[str]] = {}
    if emails:
        _heal_person_emails(graph)
        by_email = dict(
            graph.ro_query("UNWIND $emails AS e MATCH (p:Person {email_lc: e}) RETURN e, collect(p.id)[..2]", {"emails": emails}).result_set
        )

    write, unlink, link, moves, linked_to = [], [], [], [], {}
    for user in users:
        content_hash, links = stored.get(user.id, (None, []))
        admin = [person for via, person in links if via == "admin"]
        by_mail = [person for via, person in links if via == "email"]
        if admin:
            target = admin[0]
            relink = bool(by_mail)
        else:
            matches = by_email.get(_email(user)) or []
            target = matches[0] if len(matches) == 1 else None
            relink = (by_mail[0] if by_mail else None) != target or len(by_mail) > 1
        linked_to[user.id] = target
        row = _user_row(install, user)
        if row["hash"] != content_hash:
            write.append(row)
        if relink:
            unlink.append(user.id)
            if target is not None and not admin:
                link.append([user.id, target])
            if user.id in stored:  # a new account holds no edges yet
                old = sorted({person for _via, person in links if person != target} | ({user.id} if target is not None else set()))
                moves.append({"user": user.id, "old": old, "target": target})

    now = now_ms()
    if write:
        graph.query(
            """
            UNWIND $rows AS row
            MERGE (u:ExternalUser {id: row.id})
            SET u.name = row.name, u.source = $source, u.install = $install, u.external_id = row.external_id,
                u.email = row.email, u.email_lc = row.email, u.email_from = row.email_from, u.account_type = row.account_type,
                u.scope = $scope, u.content_hash = row.hash, u.synced_at = $now
            """,
            {"rows": write, "source": install.source, "install": install.key, "scope": ORG_INTERNAL, "now": now},
        )
    if unlink:
        graph.query("UNWIND $ids AS id MATCH (:ExternalUser {id: id})-[r:IS]->(:Person) WHERE r.via = 'email' DELETE r", {"ids": unlink})
    if link:
        graph.query(
            "UNWIND $pairs AS pair MATCH (u:ExternalUser {id: pair[0]}), (p:Person {id: pair[1]}) MERGE (u)-[r:IS]->(p) SET r.via = 'email'",
            {"pairs": link},
        )
    if moves:
        _move_edges(graph, moves)
    return linked_to


def _email(user: ExternalUser) -> str:
    return (user.email or "").strip().lower()


def _user_row(install: Install, user: ExternalUser) -> dict:
    row = {
        "id": user.id, "name": user.name or user.external_id, "external_id": str(user.external_id), "email": _email(user),
        "email_from": user.email_from or "api", "account_type": user.account_type or "",
    }
    row["hash"] = _hash(USER_HASH_VERSION, install.key, row)
    return row


def _heal_person_emails(graph) -> None:
    """``Person.email_lc`` is what accounts link on (indexed). The seed, the team importer and Slack write
    ``email`` only, so a Person whose ``email_lc`` is missing or stale gets it here: one pass over Person,
    writing nothing when every one is current."""
    graph.query(
        """
        MATCH (p:Person)
        WHERE coalesce(p.email_lc, '') <> toLower(trim(coalesce(p.email, '')))
        SET p.email_lc = CASE WHEN trim(coalesce(p.email, '')) = '' THEN NULL ELSE toLower(trim(p.email)) END
        """
    )


def _move_edges(graph, moves: list[dict]) -> None:
    """OWNS and WORKED_ON an account brought (``via`` it) go to its new holder: the Person it now links to,
    else the account itself. ``old`` names the holders they may be on now. Grouped by the labels at both
    ends, so every MATCH is on an indexed id."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for move in moves:
        target = move["target"] or move["user"]
        target_label = "Person" if move["target"] else "ExternalUser"
        for old in move["old"]:
            if old == target:
                continue
            old_label = "ExternalUser" if old == move["user"] else "Person"
            groups.setdefault((old_label, target_label), []).append({"user": move["user"], "old": old, "target": target})
    for (old_label, target_label), rows in sorted(groups.items()):
        for rel in sorted(PERSON_RELS):
            graph.query(
                f"""
                UNWIND $rows AS m
                MATCH (h:{old_label} {{id: m.old}})-[r:{rel}]->(n)
                WHERE r.via = m.user
                MATCH (t:{target_label} {{id: m.target}})
                MERGE (t)-[moved:{rel}]->(n)
                SET moved.item = r.item, moved.source = r.source, moved.install = r.install, moved.via = r.via
                DELETE r
                """,
                {"rows": rows},
            )


def _move_person_edges(graph, user_id: str, person_id: str | None) -> None:
    """Every OWNS and WORKED_ON ``user_id`` brought, wherever it is now, to ``person_id`` (or back to the
    account). For a one-off relink (the console's People screen); a sync uses ``_move_edges``."""
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


# --- pages ------------------------------------------------------------------------------


def write_page(install: Install, page: Page, max_items_per_container: int | None = None) -> dict:
    """Write one page: users, then items with the edges they own, then deletions. Returns counts.

    An item whose container is not one of the install's is dropped (the containers are the
    allow-list). With ``max_items_per_container``, a new item past that many in its container
    is dropped too; items already there still update.

    Batched: the users in a few queries, the items of a label in one, the edges of one (type, labels)
    in one, whatever the page's size. An item whose content, ABOUT targets and owned edges hash to what
    is stored (``content_hash``) is not written, so a poll that found nothing new writes nothing and
    bumps no graph generation (``falkordb_client.deferred_generation``: the page's writes bump once).

    No reader sees an item without its edges: an item's node, container and ABOUT edges are one
    query; its other edges are merged next; only then are the edges it no longer has deleted.
    """
    with deferred_generation():
        return _write_page(install, page, max_items_per_container)


def _write_page(install: Install, page: Page, max_items_per_container: int | None) -> dict:
    report = {"items": 0, "users": 0, "edges": 0, "deleted": 0, "skipped": 0, "unchanged": 0}
    for item in page.items:
        _check(install, item.id)
        if item.label not in ITEM_LABELS or item.state not in STATES:
            raise ValueError(f"item {item.id}: label must be one of {sorted(ITEM_LABELS)} and state one of {STATES}")
    for edge in page.edges:
        if edge.rel not in EDGE_RELS:
            raise ValueError(f"edge {edge.rel} is not one a connector may write")
        for end in (edge.from_id, edge.to_id):
            if _check(install, end).startswith(f"{install.key}:container:"):
                raise ValueError(f"edge {edge.rel} may not touch a container: {end}")
    _, graph = connect()

    linked_to = upsert_users(install, page.users)
    report["users"] = len(linked_to)

    scopes = container_scopes(install) if page.items else {}
    stored = _stored_items(graph, page.items)
    services = _services(graph) if any(item.about for item in page.items) else []
    owned: dict[str, list] = {}
    for edge in page.edges:
        owned.setdefault(edge.to_id if _is_user(install, edge.from_id) else edge.from_id, []).append(edge)

    room: dict[str, int] = {}
    kept: list[Item] = []
    for item in page.items:
        if item.container not in scopes:
            report["skipped"] += 1
            continue
        if max_items_per_container is not None and item.id not in stored:
            if item.container not in room:
                room[item.container] = max_items_per_container - count_items(item.container)
            if room[item.container] <= 0:
                report["skipped"] += 1
                continue
            room[item.container] -= 1
        kept.append(item)

    # Who an edge from an account starts at: OWNS and WORKED_ON from its Person when it has one.
    user_ends = sorted({edge.from_id for edge in page.edges if _is_user(install, edge.from_id) and edge.rel in PERSON_RELS} - set(linked_to))
    linked_to.update(_people_of(graph, user_ends))

    rows_by_label: dict[str, list[dict]] = {}
    written: list[Item] = []
    keep: dict[str, list[str]] = {}
    for item in kept:
        about = _about(item, services)
        edges = [(edge, *_start(install, edge, linked_to)) for edge in owned.get(item.id, [])]
        row = _item_row(install, item, scopes[item.container], about, edges)
        if stored.get(item.id) == row["hash"]:
            report["unchanged"] += 1
            continue
        rows_by_label.setdefault(item.label, []).append(row)
        keep[item.id] = [f"ABOUT|{item.id}|{service}" for service in about] + [f"{edge.rel}|{start}|{edge.to_id}" for edge, _label, start in edges]
        written.append(item)
    for label, rows in sorted(rows_by_label.items()):
        _upsert_items(graph, install, label, rows)
    report["items"] = len(written)

    # The edges of written items, and edges whose item is not in this page (those are only added).
    written_ids = {item.id for item in written}
    page_ids = {item.id for item in page.items}
    to_write = [edge for edge in page.edges if (owner := _owner(install, edge)) in written_ids or owner not in page_ids]
    report["edges"] = _write_edges(graph, install, to_write, linked_to, {item.id: item.label for item in kept})
    for label in sorted({item.label for item in written}):
        _drop_stale_edges(graph, label, [{"id": item.id, "keep": keep[item.id]} for item in written if item.label == label])

    if written:
        _link_waiting_references(graph, install.source, [item for item in written if item.label == "Ticket"])
    report["deleted"] = delete_items(install, page.deleted_ids)
    return report


def _is_user(install: Install, node_id: str) -> bool:
    return node_id.startswith(f"{install.key}:user:")


def _owner(install: Install, edge) -> str:
    """The item an edge belongs to: its start, unless that is an account."""
    return edge.to_id if _is_user(install, edge.from_id) else edge.from_id


def _start(install: Install, edge, linked_to: dict[str, str | None]) -> tuple[str, str]:
    """The (label, id) an edge is written from: an account's Person for OWNS and WORKED_ON when linked."""
    if _is_user(install, edge.from_id):
        person = linked_to.get(edge.from_id) if edge.rel in PERSON_RELS else None
        return ("Person", person) if person else ("ExternalUser", edge.from_id)
    return "", edge.from_id


def _people_of(graph, user_ids: list[str]) -> dict[str, str | None]:
    if not user_ids:
        return {}
    rows = graph.ro_query(
        "UNWIND $ids AS id MATCH (u:ExternalUser {id: id}) OPTIONAL MATCH (u)-[r:IS]->(p:Person) "
        "RETURN id, collect([r.via, p.id])",
        {"ids": user_ids},
    ).result_set
    found = {}
    for user_id, links in rows:
        links = [(via, person) for via, person in links if person is not None]
        admin = [person for via, person in links if via == "admin"]
        found[user_id] = admin[0] if admin else (links[0][1] if links else None)
    return found


def _stored_items(graph, items: list[Item]) -> dict[str, str | None]:
    """Item id -> its stored ``content_hash`` (None for an item written before hashes), for the page's
    items already in the graph: one query, each label on its own index."""
    by_label: dict[str, list[str]] = {}
    for item in items:
        by_label.setdefault(item.label, []).append(item.id)
    if not by_label:
        return {}
    parts, params = [], {}
    for n, (label, ids) in enumerate(sorted(by_label.items())):
        parts.append(f"MATCH (n:{label}) WHERE n.id IN $ids{n} RETURN n.id AS id, n.content_hash AS hash")
        params[f"ids{n}"] = ids
    return dict(graph.ro_query(" UNION ALL ".join(parts), params).result_set)


def _services(graph) -> list:
    """The Services an ABOUT hint may name, as ``resolve`` matches them: one read on the Service label."""
    rows = graph.ro_query(
        """
        MATCH (n:Service)
        WHERE n.name IS NOT NULL AND coalesce(n.scope, 'org_shared') IN $scopes
        RETURN n.id, n.name, coalesce(n.aliases, ''), CASE WHEN n.external_id IS NULL THEN coalesce(n.title, '') ELSE '' END, n.kind
        """,
        {"scopes": [resolve.ORG_SHARED, ORG_INTERNAL]},
    ).result_set
    return [
        resolve.EntityRow(id=row[0], name=row[1], label="Service", aliases=row[2] or "", title=row[3] or "", kind=kinds.kind_or_default(row[4]))
        for row in rows
    ]


def _hash(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()[:32]


def changed_at_ms(updated_at: str | None) -> int | None:
    """``Item.updated_at`` (ISO 8601 as the vendor writes it: ``...Z``, ``+0000`` or ``+00:00``; or epoch
    ms) as epoch milliseconds, for ``what_changed`` to compare. None when it cannot be read."""
    text = (updated_at or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    match = _TZ_NO_COLON.search(text)
    if match:
        text = text[: match.start()] + f"{match.group(1)}{match.group(2)}:{match.group(3)}"
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return int(stamp.timestamp() * 1000)


def _item_row(install: Install, item: Item, scope: str, about: list[str], edges: list) -> dict:
    row = {
        "id": item.id, "name": item.name, "title": item.title or item.name, "summary": _clip(item.summary), "url": item.url,
        "state": item.state, "status": item.status, "updated": item.updated_at, "external_id": str(item.external_id),
        "scope": scope, "container": item.container, "props": _props(item.props), "about": about,
        "kind": kinds.normalize_kind(item.kind) if item.label == "Service" else None,
        "changed_at_ms": changed_at_ms(item.updated_at),
    }
    owned = sorted(
        [edge.rel, start_label, start, edge.to_id, sorted(_props(edge.props, RESERVED_EDGE_PROPS).items())] for edge, start_label, start in edges
    )
    row["hash"] = _hash(ITEM_HASH_VERSION, install.key, item.label, row, owned)
    return row


def _upsert_items(graph, install: Install, label: str, rows: list[dict]) -> None:
    """Every row of one label in one query: the node, its container, its ABOUT edges. One query is one
    transaction, so a reader sees each item before or after, never half written."""
    kind = "SET n.kind = coalesce(row.kind, n.kind)" if label == "Service" else ""
    graph.query(
        f"""
        UNWIND $rows AS row
        MERGE (n:{label} {{id: row.id}})
        SET n.name = row.name, n.title = row.title, n.summary = row.summary, n.url = row.url, n.state = row.state,
            n.status = row.status, n.source_updated_at = row.updated, n.changed_at_ms = row.changed_at_ms,
            n.external_id = row.external_id, n.source = $source, n.install = $install, n.scope = row.scope,
            n.content_hash = row.hash, n.synced_at = $now
        SET n += row.props
        {kind}
        WITH n, row
        OPTIONAL MATCH (n)-[old:IN_CONTAINER]->(previous:Container)
        WHERE previous.id <> row.container
        DELETE old
        WITH DISTINCT n, row
        MATCH (c:Container {{id: row.container}})
        MERGE (n)-[:IN_CONTAINER]->(c)
        WITH n, row
        UNWIND row.about AS service_id
        MATCH (s:Service {{id: service_id}})
        MERGE (n)-[a:ABOUT]->(s)
        SET a.item = row.id, a.source = $source, a.install = $install
        """,
        {"rows": rows, "source": install.source, "install": install.key, "now": now_ms()},
    )


def _write_edges(graph, install: Install, edges: list, linked_to: dict[str, str | None], item_labels: dict[str, str]) -> int:
    """MERGE the edges, one query per (type, start label, end label). Ends not in the page are looked up
    once, each label on its own index; an edge whose end is not in the graph is not written."""
    if not edges:
        return 0
    known = dict(item_labels)
    known.update({user_id: "ExternalUser" for user_id in linked_to if _is_user(install, user_id)})
    known.update({person: "Person" for person in linked_to.values() if person})
    missing = sorted({end for edge in edges for end in (edge.from_id, edge.to_id)} - set(known))
    known.update(_labels_of(graph, missing))
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for edge in edges:
        start_label, start = _start(install, edge, linked_to)
        start_label = start_label or known.get(start)
        end_label = known.get(edge.to_id)
        if start_label is None or end_label is None or not {start_label, end_label} <= _EDGE_END_LABELS | {"Person"}:
            continue
        groups.setdefault((edge.rel, start_label, end_label), []).append(
            {
                "from": start, "to": edge.to_id, "item": _owner(install, edge), "via": edge.from_id if _is_user(install, edge.from_id) else None,
                "props": _props(edge.props, RESERVED_EDGE_PROPS),
            }
        )
    written = 0
    for (rel, start_label, end_label), rows in sorted(groups.items()):
        result = graph.query(
            f"""
            UNWIND $rows AS row
            MATCH (a:{start_label} {{id: row.from}}), (b:{end_label} {{id: row.to}})
            MERGE (a)-[r:{rel}]->(b)
            SET r.item = row.item, r.source = $source, r.install = $install, r.via = row.via
            SET r += row.props
            RETURN count(r)
            """,
            {"rows": rows, "source": install.source, "install": install.key},
        ).result_set
        written += int(result[0][0]) if result else 0
    return written


def _labels_of(graph, ids: list[str]) -> dict[str, str]:
    """Id -> label for this install's ids (items or accounts), each label on its own index: one query."""
    if not ids:
        return {}
    labels = sorted(_EDGE_END_LABELS)
    query = " UNION ALL ".join(f"MATCH (n:{label}) WHERE n.id IN $ids RETURN n.id AS id, '{label}' AS label" for label in labels)
    return dict(graph.ro_query(query, {"ids": ids}).result_set)


def _drop_stale_edges(graph, label: str, rows: list[dict]) -> None:
    """Delete the edges each item owns that this page no longer carries (``keep``: ``TYPE|start|end``)."""
    graph.query(
        f"""
        UNWIND $rows AS row
        MATCH (n:{label} {{id: row.id}})-[r]-()
        WHERE r.item = row.id AND NOT (type(r) + '|' + startNode(r).id + '|' + endNode(r).id) IN row.keep
        DELETE r
        """,
        {"rows": rows},
    )


def _about(item: Item, services: list) -> list[str]:
    """Each Service a hint names outright; else the best guess over all hints, as GitHub issues get.
    Never a partial name: "store" must not make a ticket ABOUT Graph Store."""
    hints = [hint for hint in item.about if hint and hint.strip()]
    found = []
    for hint in hints:
        entity = resolve.resolve_rows(hint, services, ("Service",), partial=False).entity
        if entity is not None and entity.id != item.id and entity.id not in found:
            found.append(entity.id)
    if not found and hints:
        guess = resolve.best_match_rows(" ".join(hints), services, ("Service",))
        if guess is not None and guess.id != item.id and _names_it(" ".join(hints), guess.name):
            found.append(guess.id)
    return found


def _names_it(text: str, name: str) -> bool:
    """Whether ``text`` has every distinctive word of ``name`` ("billing" for Billing Service; "store"
    alone is not Graph Store). Words that only say what kind of thing it is ("service") do not count."""
    said = resolve.tokens(text)
    distinctive = [
        word for word in resolve.tokens(name)
        if len(word) >= 3 and word not in resolve.STOPWORDS and not any(resolve.same_word(word, head) for head in resolve.GENERIC_HEADS)
    ]
    return bool(distinctive) and all(any(resolve.same_word(word, other) for other in said) for word in distinctive)


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
    ids = list(upsert_users(install, users or []))
    _, graph = connect()
    graph.query("MATCH (u:ExternalUser)-[r:CAN_READ]->(:Container {id: $id}) WHERE NOT u.id IN $ids DELETE r", {"id": container_id, "ids": ids})
    if ids:
        graph.query(
            "UNWIND $users AS user MATCH (u:ExternalUser {id: user}), (c:Container {id: $id}) MERGE (u)-[r:CAN_READ]->(c) SET r.install = $install",
            {"users": ids, "id": container_id, "install": install.key},
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
