"""Tools that answer from memories: dated nodes written from chat, tied to the graph by MENTIONS edges.

``related_memories`` starts from the node a question names and collects the memories
that mention it, then the memories that mention something one hop away. ``recall``
searches memory text for a free-form question. Both filter on the asker's read set in
every branch and return each memory with the day it was learned.
"""

from src.db.falkordb_client import connect
from src.domain.knowledge import resolve
from src.domain.knowledge.dto import MemoryLine, PathStep, ToolResult, day_from
from src.domain.knowledge.resolve import join_names
from src.domain.memory.search_brain import search_brain

MENTIONABLE = ("Service", "Document", "Ticket", "Decision")
DEFAULT_K = 5
MAX_K = 10
SNIPPET = 48

# A memory nobody forgot. Shared with what_changed.
LIVE = "NOT coalesce(m.is_forgotten, false)"
_LIVE = LIVE  # the old name, for code written before what_changed shared it


def related_memories(question: str, scopes: list[str], k=DEFAULT_K) -> ToolResult:
    _, graph = connect()
    resolution = resolve.resolve_name(question, MENTIONABLE, scopes, graph=graph)
    if not resolution.found:
        kind = "service, document, ticket, or decision"
        return ToolResult(tool="related_memories", answer=resolve.describe_miss(resolution, kind), found=False, candidates=resolution.candidate_names())
    target = resolution.entity
    limit = _clamp(k)

    rows = graph.ro_query(
        f"""
        MATCH (m:Memory)-[:MENTIONS]->(e {{id: $id}})
        WHERE m.scope IN $scopes AND {LIVE}
        OPTIONAL MATCH (a:SlackUser)-[:SAID]->(m)
        RETURN m.id AS id, m.text AS text, m.scope AS scope, m.channel_id AS channel_id, m.created_at AS learned_at,
               0 AS hops, e.id AS via_id, e.name AS via, labels(e)[0] AS via_label, '' AS bridge, true AS forward,
               coalesce(a.name, a.id) AS author
        UNION
        MATCH (e {{id: $id}})-[r]-(n)
        WHERE (n:Service OR n:Document OR n:Ticket OR n:Decision) AND {resolve.SCOPED.format(var="n")}
        MATCH (m:Memory)-[:MENTIONS]->(n)
        WHERE m.scope IN $scopes AND {LIVE} AND NOT (m)-[:MENTIONS]->(e)
        OPTIONAL MATCH (a:SlackUser)-[:SAID]->(m)
        RETURN m.id AS id, m.text AS text, m.scope AS scope, m.channel_id AS channel_id, m.created_at AS learned_at,
               1 AS hops, n.id AS via_id, n.name AS via, labels(n)[0] AS via_label, type(r) AS bridge, startNode(r) = e AS forward,
               coalesce(a.name, a.id) AS author
        ORDER BY hops, learned_at DESC
        LIMIT $k
        """,
        {"id": target.id, "scopes": scopes, "k": limit},
    ).result_set

    result = ToolResult(tool="related_memories", answer="", candidates=resolution.candidate_names())
    seen: set[str] = set()
    direct = 0
    via_names: list[str] = []
    for memory_id, text, scope, channel_id, learned_ms, hops, via_id, via, via_label, bridge, forward, author in rows:
        if memory_id in seen:
            continue
        if len(result.memories) >= limit:
            # The UNION's LIMIT is not reliable across both branches; the cap is enforced here.
            break
        seen.add(memory_id)
        result.memories.append(MemoryLine(text=text or "", learned_at=day_from(learned_ms, None), author=author or "someone", scope=scope, channel_id=channel_id))
        snippet = _snippet(text)
        if hops == 0:
            direct += 1
        else:
            if via not in via_names:
                via_names.append(via)
            if forward:
                result.add(PathStep(target.name, bridge, via, from_id=target.id, to_id=via_id, from_label=target.label, to_label=via_label))
            else:
                result.add(PathStep(via, bridge, target.name, from_id=via_id, to_id=target.id, from_label=via_label, to_label=target.label))
        result.add(PathStep(snippet, "MENTIONS", via, from_id=memory_id, to_id=via_id, from_label="Memory", to_label=via_label))

    total = len(result.memories)
    if not total:
        result.answer = f"No memory you can read mentions {target.name} or anything one hop from it."
        return result
    parts = [f"{total} memor{'y' if total == 1 else 'ies'} you can read touch {target.name}: {direct} mention{'s' if direct == 1 else ''} it directly"]
    if via_names:
        parts.append(f", {total - direct} via {join_names(via_names)}")
    latest = max((memory.learned_at for memory in result.memories if memory.learned_at), default="")
    result.answer = "".join(parts) + "." + (f" Most recent: {latest}." if latest else "")
    return result


def recall(query: str, scopes: list[str], k=DEFAULT_K) -> ToolResult:
    """Memories whose text matches the question, across the asker's read set. Dated lines, no path."""
    limit = _clamp(k)
    found = search_brain(query, None, container_tags_override=scopes)
    rows = (found.get("results") or [])[:limit]
    result = ToolResult(tool="recall", answer="")
    if not rows:
        result.answer = f"Nothing in the memories I can read mentions “{resolve.core(query) or query}”."
        return result
    for row in rows:
        result.memories.append(
            MemoryLine(
                text=row.get("memory") or "",
                learned_at=day_from(row.get("created_at"), row.get("ts")) or day_from(row.get("updated_at")),
                author=row.get("author") or "someone",
                scope=row.get("container_tag") or "org_shared",
                channel_id=row.get("channel_id"),
            )
        )
    latest = max((memory.learned_at for memory in result.memories if memory.learned_at), default="")
    result.answer = f"{len(rows)} memor{'y' if len(rows) == 1 else 'ies'} match." + (f" Most recent: {latest}." if latest else "")
    return result


def _clamp(k) -> int:
    try:
        return max(1, min(MAX_K, int(k)))
    except (TypeError, ValueError):
        return DEFAULT_K


def _snippet(text: str | None) -> str:
    flat = " ".join((text or "").split())
    return f"“{flat if len(flat) <= SNIPPET else flat[: SNIPPET - 1] + '…'}”"
