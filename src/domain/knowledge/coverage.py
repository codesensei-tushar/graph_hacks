"""What a connected tool's sync covers: its projects (containers), and the ones it stopped short in.

A connector writes each project, portal or space as a ``Container`` and every item it syncs
``IN_CONTAINER`` it. A container holds at most ``MAX_ITEMS_PER_CONTAINER`` items (``connectors/sync``),
the newest; the sync marks one that reached the cap ``capped`` on its ``ConnectorState``. This module
answers, always within the asker's scopes (a container is as visible as its own ``scope``):

- ``resolve_container``: the project a question names by key or name ("open tickets in PLAT");
- ``project``: whether a project key is synced, and the span of ticket keys it holds;
- ``capped_note``: the sentence an answer adds when what it read sits in a capped container.
"""

import re

from src.domain.knowledge import resolve
from src.domain.knowledge.resolve import SCOPED, Entity

SOURCE_NAMES = {"jira": "Jira", "hubspot": "HubSpot"}
# The item each source holds, for the cap sentence.
_ITEMS = {"jira": "issues", "hubspot": "records"}
_KEY_NUMBER = re.compile(r"-(\d+)$")


def _cap() -> int:
    from src.connectors.sync import MAX_ITEMS_PER_CONTAINER  # the sync owns the cap; read late, it imports the connectors

    return MAX_ITEMS_PER_CONTAINER


def containers(graph, scopes: list[str]) -> list[tuple[str, str, str, str]]:
    """Every container the asker can read: (id, name, external_id, source)."""
    rows = graph.ro_query(
        f"""
        MATCH (c:Container)
        WHERE {SCOPED.format(var="c")}
        RETURN c.id, coalesce(c.name, c.external_id, c.id), coalesce(c.external_id, ''), coalesce(c.source, '')
        """,
        {"scopes": list(scopes)},
    ).result_set
    return [tuple(row) for row in rows]


def resolve_container(graph, text: str, scopes: list[str]) -> Entity | None:
    """The one readable container the text names by its key ("PLAT", any case) or its name ("Payments project")."""
    found = containers(graph, scopes)
    words = set(resolve.tokens(text))
    by_key = [row for row in found if row[2] and row[2].lower() in words]
    if len(by_key) == 1:
        return _entity(by_key[0])
    rows = [resolve.EntityRow(id=row[0], name=row[1], label="Container") for row in found]
    resolution = resolve.resolve_rows(text, rows, ("Container",))
    if resolution.found:
        return _entity(next(row for row in found if row[0] == resolution.entity.id))
    return None


def _entity(row: tuple) -> Entity:
    container_id, name, key, source = row
    tool = SOURCE_NAMES.get(source, source.capitalize() or "Connected")
    return Entity(container_id, f"{tool} {key}" if key else name, "Container", source)


def project(graph, key: str, scopes: list[str]) -> tuple[str, int | None, int | None] | None:
    """(tool, lowest, highest ticket number) for a readable project with this key, or None."""
    match = next((row for row in containers(graph, scopes) if row[2].upper() == key.upper()), None)
    if match is None:
        return None
    names = graph.ro_query(
        f"MATCH (t:Ticket)-[:IN_CONTAINER]->(:Container {{id: $id}}) WHERE {SCOPED.format(var='t')} RETURN t.name",
        {"id": match[0], "scopes": list(scopes)},
    ).result_set
    numbers = sorted(int(found.group(1)) for (name,) in names if (found := _KEY_NUMBER.search(str(name or ""))))
    tool = SOURCE_NAMES.get(match[3], match[3].capitalize() or "Connected")
    return (tool, numbers[0], numbers[-1]) if numbers else (tool, None, None)


def capped_note(graph, scopes: list[str], node_ids: list[str]) -> str:
    """" (Jira PAY is capped at its newest 1,000 issues.)" for each readable, capped container these nodes are in; "" otherwise."""
    ids = [node_id for node_id in dict.fromkeys(node_ids) if node_id]
    if not ids:
        return ""
    rows = graph.ro_query(
        f"""
        MATCH (n)-[:IN_CONTAINER]->(c:Container)
        WHERE n.id IN $ids AND {SCOPED.format(var="c")}
        WITH DISTINCT c
        MATCH (state:ConnectorState {{id: c.id}})
        WHERE state.capped = true
        RETURN coalesce(c.external_id, c.name, c.id), coalesce(c.source, '')
        ORDER BY coalesce(c.external_id, c.name, c.id)
        """,
        {"ids": ids, "scopes": list(scopes)},
    ).result_set
    if not rows:
        return ""
    cap = f"{_cap():,}"
    notes = [f"{SOURCE_NAMES.get(source, source.capitalize())} {key} is capped at its newest {cap} {_ITEMS.get(source, 'items')}" for key, source in rows]
    return f" ({'; '.join(notes)}.)"


def note_for(graph, scopes: list[str], path) -> str:
    """``capped_note`` for the nodes a path cites."""
    return capped_note(graph, scopes, [node for step in path for node in (step.from_id, step.to_id)])
