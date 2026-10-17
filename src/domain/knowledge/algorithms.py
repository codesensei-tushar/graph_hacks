"""Tools that run a graph algorithm instead of a hand-rolled walk.

``blast_radius`` follows DEPENDS_ON edges to a depth and reports where the walk
stopped finding anything new; betweenness names the main connector. ``path_between``
asks FalkorDB for the shortest routes between two nodes and cites each hop in its true
direction. ``load_bearing`` ranks services by PageRank over DEPENDS_ON. People are not
ranked by PageRank anywhere: with almost no incoming edges their scores come out flat,
so ``who_knows`` weighs edges instead.
"""

import logging
import re

from src.db.falkordb_client import connect
from src.domain.knowledge import resolve
from src.domain.knowledge.dto import PathStep, ToolResult
from src.domain.knowledge.resolve import Entity, join_names

log = logging.getLogger(__name__)

MAX_DEPTH = 6
MAX_PATH_LENGTH = 6
DEFAULT_LOAD_BEARING = 5
MAX_LOAD_BEARING = 10

# Edges that carry knowledge between people, code, tickets, and decisions. Team
# membership is a weaker link, used only when nothing else connects two nodes.
KNOWLEDGE_RELS = [
    "OWNS", "WORKED_ON", "KNOWS", "DEPENDS_ON", "TOUCHES", "AUTHORED", "REVIEWED", "IS",
    "ABOUT", "CITES", "DESCRIBES", "PROPOSED", "APPROVED", "OPENED", "ASSIGNED", "CLOSES",
    "SUPERSEDES", "BASED_ON", "DECIDED_IN", "MENTIONS",
]
TEAM_RELS = ["MEMBER_OF", "RUNS", "PART_OF", "IN_REPO"]
STRONG_RELS = {"OWNS", "DEPENDS_ON", "TOUCHES"}
PATH_LABELS = ("Service", "Person", "Team", "Document", "Ticket", "Decision")

_SCOPED_PATH = "all(n IN nodes(path) WHERE coalesce(n.scope, 'org_shared') IN $scopes)"

_WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
_HOPS = re.compile(r"\b(\d+|one|two|three|four|five|six)\s+hops?\b", re.IGNORECASE)
_DEPTH = re.compile(r"\bdepth\s+(\d+)\b", re.IGNORECASE)
_DIRECT = re.compile(r"\b(?:directly|direct dependents|immediate(?:ly)?|first hop)\b", re.IGNORECASE)


def clamp_depth(value) -> int | None:
    """An int in 1..MAX_DEPTH, or None for "follow everything". Only this int is ever built into a query."""
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, min(MAX_DEPTH, number))


def depth_in(question: str) -> int | None:
    """The hop count a question asks for ("two hops out", "depth 3", "directly"), or None."""
    if match := _HOPS.search(question):
        token = match.group(1).lower()
        return clamp_depth(_WORD_NUMBERS.get(token, token))
    if match := _DEPTH.search(question):
        return clamp_depth(match.group(1))
    if _DIRECT.search(question):
        return 1
    return None


# --- blast radius ------------------------------------------------------------------------


def blast_radius(question: str, scopes: list[str], depth=None) -> ToolResult:
    """What stops working if a service goes down, following DEPENDS_ON for ``depth`` hops."""
    _, graph = connect()
    resolution = resolve.resolve_name(question, ("Service",), scopes, graph=graph)
    if not resolution.found:
        return ToolResult(tool="blast_radius", answer=resolve.describe_miss(resolution, "service"), found=False, candidates=resolution.candidate_names())
    target = resolution.entity
    wanted = clamp_depth(depth)

    # Always walk to the ceiling so the reply can say where the walk stopped finding anything new.
    rows = graph.ro_query(
        f"""
        MATCH path = (dependent:Service)-[:DEPENDS_ON*1..{MAX_DEPTH}]->(service:Service {{id: $id}})
        RETURN dependent.id, dependent.name, length(path), [n IN nodes(path) | n.id], [n IN nodes(path) | n.name]
        ORDER BY length(path), dependent.name
        """,
        {"id": target.id},
    ).result_set
    # Every shortest route to each dependent: the first row sets the length, equal-length rows add their edges.
    shortest: dict[str, list[tuple[str, int, list[str], list[str]]]] = {}
    for dependent_id, dependent_name, length, ids, names in rows:
        routes = shortest.setdefault(dependent_id, [])
        if not routes or routes[0][1] == length:
            routes.append((dependent_name, length, ids, names))

    saturation = max((routes[0][1] for routes in shortest.values()), default=0)
    reached = wanted if wanted is not None else saturation
    result = ToolResult(tool="blast_radius", answer="", candidates=resolution.candidate_names(), depth=max(reached, 1))
    if not shortest:
        result.answer = f"Nothing in the graph depends on {target.name}."
        return result

    affected = {dep_id: routes for dep_id, routes in shortest.items() if routes[0][1] <= reached}
    for routes in affected.values():
        for _name, _length, ids, names in routes:
            for index in range(len(ids) - 1):
                result.add(PathStep(names[index], "DEPENDS_ON", names[index + 1], from_id=ids[index], to_id=ids[index + 1], from_label="Service", to_label="Service"))

    by_hop: dict[int, list[str]] = {}
    for routes in affected.values():
        name, length, _ids, _names = routes[0]
        by_hop.setdefault(length, []).append(name)
    listed = ", ".join(f"{join_names(sorted(names))} ({hop} hop{'s' if hop > 1 else ''})" for hop, names in sorted(by_hop.items()))
    parts = [f"If {target.name} goes down, these stop with it: {listed}."]
    beyond = [routes[0] for routes in shortest.values() if routes[0][1] > reached]
    if beyond:
        further = join_names([f"{name} at depth {length}" for name, length, _ids, _names in sorted(beyond, key=lambda row: (row[1], row[0]))])
        parts.append(f"{len(beyond)} more break{'s' if len(beyond) == 1 else ''} further out ({further}); ask with more hops.")
    else:
        parts.append(f"Depth {reached} reached {len(affected)} service{'s' if len(affected) != 1 else ''}; nothing new past depth {saturation}.")
    connector = _main_connector(graph, list(affected))
    if connector is not None:
        name, score = connector
        parts.append(f"Among them, {name} is the connector: {score:g} shortest dependency paths run through it.")
    result.answer = " ".join(parts)
    return result


def _main_connector(graph, service_ids: list[str]) -> tuple[str, float] | None:
    """The top connector by betweenness, or None. Runs over the whole service graph; a timeout drops the sentence, not the answer."""
    try:
        rows = _betweenness(graph, service_ids)
    except Exception as error:  # noqa: BLE001  the sentence is optional; the traversal above is the answer
        log.warning("[blast_radius] betweenness skipped: %s", error)
        return None
    return (rows[0][0], float(rows[0][1])) if rows else None


def _betweenness(graph, service_ids: list[str]) -> list:
    return graph.ro_query(
        """
        CALL algo.betweenness({nodeLabels: ['Service'], relationshipTypes: ['DEPENDS_ON']}) YIELD node, score
        WHERE node.id IN $ids AND score > 0
        RETURN node.name, score
        ORDER BY score DESC, node.name
        LIMIT 1
        """,
        {"ids": service_ids},
    ).result_set


# --- shortest paths -----------------------------------------------------------------------


def path_between(source: str, target: str, scopes: list[str]) -> ToolResult:
    """Why two nodes are connected: the shortest route between them, each hop cited in its true direction."""
    _, graph = connect()
    ends = _ends(graph, source, target, scopes)
    if isinstance(ends, ToolResult):
        return ends
    start, end = ends
    if start == end:
        return ToolResult(tool="path_between", answer=f"{start.name} is one node; name two things to find the route between them.")

    rows = _shortest_paths(graph, start, end, KNOWLEDGE_RELS, scopes)
    if not rows:
        rows = _shortest_paths(graph, start, end, KNOWLEDGE_RELS + TEAM_RELS, scopes)
    if not rows:
        return ToolResult(
            tool="path_between",
            answer=f"No path between {start.name} and {end.name} within {MAX_PATH_LENGTH} hops in what you can read.",
        )

    hops_list = [hops for hops, _length in rows]
    best = max(hops_list, key=lambda hops: sum(1 for hop in hops if hop[3] in STRONG_RELS))
    result = ToolResult(tool="path_between", answer="", depth=len(best))
    for from_id, from_name, from_label, rel, to_id, to_name, to_label, forward in best:
        if forward:
            result.add(PathStep(from_name, rel, to_name, from_id=from_id, to_id=to_id, from_label=from_label, to_label=to_label))
        else:
            result.add(PathStep(to_name, rel, from_name, from_id=to_id, to_id=from_id, from_label=to_label, to_label=from_label))
    chain = " → ".join([best[0][1], *[hop[5] for hop in best]])
    others = len(rows) - 1
    answer = f"{start.name} reaches {end.name} in {len(best)} hop{'s' if len(best) > 1 else ''}: {chain}."
    if others:
        answer += f" {others} other route{'s' if others > 1 else ''} of the same length."
    result.answer = answer
    return result


def _ends(graph, source: str, target: str, scopes: list[str]):
    if not target or target.strip().lower() == source.strip().lower():
        # The keyword router passes the whole question: both names are in it, in order.
        named = resolve.resolve_all(source, PATH_LABELS, scopes, graph=graph)
        if len(named) < 2:
            resolution = resolve.resolve_name(source, PATH_LABELS, scopes, graph=graph)
            answer = "Name two things in the graph to find the route between them."
            if not resolution.found:
                answer = resolve.describe_miss(resolution, "service, person, team, document, ticket, or decision")
            elif resolution.entity is not None:
                answer = f"I found {resolution.entity.name}; name a second thing to find the route between them."
            return ToolResult(tool="path_between", answer=answer, found=resolution.found, candidates=resolution.candidate_names())
        return named[0], named[1]
    ends = []
    for text in (source, target):
        resolution = resolve.resolve_name(text, PATH_LABELS, scopes, graph=graph)
        if not resolution.found:
            kind = "service, person, team, document, ticket, or decision"
            return ToolResult(tool="path_between", answer=resolve.describe_miss(resolution, kind), found=False, candidates=resolution.candidate_names())
        ends.append(resolution.entity)
    return ends[0], ends[1]


def _shortest_paths(graph, start: Entity, end: Entity, rels: list[str], scopes: list[str]) -> list[tuple[list, int]]:
    rows = graph.ro_query(
        f"""
        MATCH (a {{id: $a}}), (b {{id: $b}})
        CALL algo.SPpaths({{sourceNode: a, targetNode: b, relTypes: $rels, relDirection: 'both', maxLen: {MAX_PATH_LENGTH}, pathCount: 0}})
        YIELD path
        WHERE {_SCOPED_PATH}
        RETURN [i IN range(0, length(path) - 1) | [
                    nodes(path)[i].id, nodes(path)[i].name, labels(nodes(path)[i])[0],
                    type(relationships(path)[i]),
                    nodes(path)[i + 1].id, nodes(path)[i + 1].name, labels(nodes(path)[i + 1])[0],
                    startNode(relationships(path)[i]) = nodes(path)[i]
               ]] AS hops,
               length(path)
        """,
        {"a": start.id, "b": end.id, "rels": rels, "scopes": scopes},
    ).result_set
    return [(hops, length) for hops, length in rows]


# --- PageRank ------------------------------------------------------------------------------


def load_bearing(limit=DEFAULT_LOAD_BEARING) -> ToolResult:
    """The services the rest of the company leans on: PageRank over DEPENDS_ON."""
    try:
        count = max(1, min(MAX_LOAD_BEARING, int(limit)))
    except (TypeError, ValueError):
        count = DEFAULT_LOAD_BEARING
    _, graph = connect()
    ranked = graph.ro_query(
        """
        CALL algo.pageRank('Service', 'DEPENDS_ON') YIELD node, score
        RETURN node.id, node.name, score
        ORDER BY score DESC, node.name
        LIMIT $limit
        """,
        {"limit": count},
    ).result_set
    result = ToolResult(tool="load_bearing", answer="")
    if not ranked:
        result.answer = "The graph has no services with dependencies yet."
        return result

    sentences = []
    for index, (service_id, name, score) in enumerate(ranked):
        dependents = graph.ro_query(
            f"""
            MATCH (dependent:Service)-[:DEPENDS_ON*1..{MAX_DEPTH}]->(service:Service {{id: $id}})
            RETURN count(DISTINCT dependent)
            """,
            {"id": service_id},
        ).result_set[0][0]
        direct = graph.ro_query(
            """
            MATCH (dependent:Service)-[:DEPENDS_ON]->(service:Service {id: $id})
            RETURN dependent.id, dependent.name
            ORDER BY dependent.name
            """,
            {"id": service_id},
        ).result_set
        for dependent_id, dependent_name in direct:
            result.add(PathStep(dependent_name, "DEPENDS_ON", name, from_id=dependent_id, to_id=service_id, from_label="Service", to_label="Service"))
        if index == 0:
            sentences.append(
                f"{name} carries the most weight (PageRank {score:.2f}): {dependents} service{'s' if dependents != 1 else ''} "
                f"depend{'s' if dependents == 1 else ''} on it directly or through others."
            )
        else:
            sentences.append(f"{name} ({score:.2f}, {dependents})")
    answer = sentences[0]
    if len(sentences) > 1:
        answer += " Then " + join_names(sentences[1:]) + "."
    result.answer = answer
    return result
