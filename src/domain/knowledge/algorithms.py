"""Tools that run a graph algorithm instead of a hand-rolled walk.

``blast_radius`` follows DEPENDS_ON edges to a depth and reports where the walk
stopped finding anything new, and names the main connector: the affected service the
most dependents' shortest routes run through, counted from the routes it walked.
(FalkorDB's ``algo.betweenness`` samples source nodes with a per-process seed, so its top
node changed between runs and disagreed with exact betweenness.) ``path_between``
asks FalkorDB for the shortest routes between two nodes and cites each hop in its true
direction. ``load_bearing`` ranks services by PageRank over DEPENDS_ON. People are not
ranked by PageRank anywhere: with almost no incoming edges their scores come out flat,
so ``who_knows`` weighs edges instead.
"""

import logging
import re
import uuid
from collections import defaultdict

from src.db.falkordb_client import client, connect, graph_reads_disabled
from src.domain.knowledge import fallback, kinds, resolve
from src.domain.knowledge.dto import PathStep, ToolResult
from src.domain.knowledge.resolve import Entity, join_names

log = logging.getLogger(__name__)

MAX_DEPTH = 6
MAX_PATH_LENGTH = 6
# Equal-length routes kept when the readable-only search has to walk them out itself.
MAX_ROUTES = 50
# Nodes that search may visit before it gives up and says how far it looked. Public callers
# reach it, so its cost is bounded whatever the graph holds.
MAX_SEARCHED = 500
_LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
DEFAULT_LOAD_BEARING = 5
MAX_LOAD_BEARING = 10

# Edges that carry knowledge between people, code, tickets, and decisions. Team
# membership is a weaker link, used only when nothing else connects two nodes.
KNOWLEDGE_RELS = [
    "OWNS", "WORKED_ON", "KNOWS", "DEPENDS_ON", "TOUCHES", "AUTHORED", "REVIEWED", "IS",
    "ABOUT", "CITES", "DESCRIBES", "PROPOSED", "APPROVED", "OPENED", "ASSIGNED", "CLOSES",
    "SUPERSEDES", "BASED_ON", "DECIDED_IN", "MENTIONS", "BLOCKS", "REFERENCES",
]
TEAM_RELS = ["MEMBER_OF", "RUNS", "PART_OF", "IN_REPO"]
STRONG_RELS = {"OWNS", "DEPENDS_ON", "TOUCHES"}
PATH_LABELS = ("Service", "Person", "Team", "Document", "Ticket", "Decision")

_SCOPED_PATH = "all(n IN nodes(path) WHERE coalesce(n.scope, 'org_shared') IN $scopes)"

_WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
_HOPS = re.compile(r"\b(\d+|one|two|three|four|five|six)\s+hops?\b", re.IGNORECASE)
_DEPTH = re.compile(r"\bdepth\s+(\d+)\b", re.IGNORECASE)
_DIRECT = re.compile(r"\b(?:directly|direct dependents|immediate(?:ly)?|first hop)\b", re.IGNORECASE)
# What a service needs, not what needs it: "what does the invoice worker depend on", "billing's
# dependencies", "what does search need", "what does it need to run", "what does billing require".
# "what depends on billing", "what relies on it" and "dependents" are the other way, and "what do I
# need to know about billing" asks for people.
_NEEDS = re.compile(
    r"\bwhat (?:does|do|did) .+ (?:depend|rely) on\b|\b(?:depends|relies|requires) on what\b|\brequires what\b"
    r"|\bdependenc(?:y|ies)\b|\bupstream of\b|\bneed(?:s|ed)? to run\b"
    r"|\bwhat (?:does|do) (?!i\b|we\b|you\b|they\b)[\w' ]+? (?:need|require)\b(?! to\b)",
    re.IGNORECASE,
)
# blast_radius walks DEPENDS_ON one way or the other.
DEPENDENTS = "dependents"
DEPENDENCIES = "dependencies"


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


def asks_for_dependencies(question: str) -> bool:
    """Whether a question asks what a service depends on, rather than what depends on it."""
    return bool(_NEEDS.search(question or ""))


# --- blast radius ------------------------------------------------------------------------


def blast_radius(question: str, scopes: list[str], depth=None, direction=None) -> ToolResult:
    """What stops working if a service goes down, following DEPENDS_ON for ``depth`` hops.

    Services and systems that depend on it are listed together by hop, in the same words
    whatever else hangs off it; a process, product or playbook gets its own clause with its
    own verb ("the Renewals process stalls (1 hop)").

    ``direction="dependencies"`` walks the other way: what the service itself depends on. A
    question that asks that ("what does the invoice worker depend on") walks that way even when
    no direction is passed, so it never gets the dependents.
    """
    if direction not in (DEPENDENTS, DEPENDENCIES):
        direction = DEPENDENCIES if asks_for_dependencies(question) else DEPENDENTS
    _, graph = connect()
    resolution = resolve.resolve_name(question, ("Service",), scopes, graph=graph)
    if not resolution.found:
        miss = ToolResult(tool="blast_radius", answer=resolve.describe_miss(resolution, "service"), found=False, candidates=resolution.candidate_names())
        return fallback.before_refusing(question, scopes, miss)
    target = resolution.entity
    wanted = clamp_depth(depth)
    subject = kinds.subject(target.name, target.kind)
    if kinds.kind_or_default(target.kind) in kinds.NOT_DEPENDABLE:
        either = "nothing depends on it" if direction == DEPENDENTS else "it depends on nothing"
        return ToolResult(
            tool="blast_radius",
            answer=f"{target.name} is an {target.kind}: {either}. Ask who owns it or who knows it instead.",
            candidates=resolution.candidate_names(),
        )
    if direction == DEPENDENCIES:
        return _dependencies(graph, target, resolution, wanted, scopes)

    # Always walk to the ceiling so the reply can say where the walk stopped finding anything new.
    # Every node on a route must be readable, as for any path: a route through a service the
    # asker cannot see is not shown, and neither is what lies beyond it.
    rows = graph.ro_query(
        f"""
        MATCH path = (dependent:Service)-[:DEPENDS_ON*1..{MAX_DEPTH}]->(service:Service {{id: $id}})
        WHERE {_SCOPED_PATH} AND all(n IN nodes(path) WHERE NOT {kinds.KIND_OF.format(var="n")} IN $excluded)
        RETURN dependent.id, dependent.name, length(path), [n IN nodes(path) | n.id], [n IN nodes(path) | n.name],
               {kinds.KIND_OF.format(var="dependent")}
        ORDER BY length(path), dependent.name
        """,
        {"id": target.id, "excluded": sorted(kinds.NOT_DEPENDABLE), "scopes": scopes},
    ).result_set
    # Every shortest route to each dependent: the first row sets the length, equal-length rows add their edges.
    shortest: dict[str, list[tuple[str, int, list[str], list[str]]]] = {}
    kind_of: dict[str, str] = {}
    for dependent_id, dependent_name, length, ids, names, dependent_kind in rows:
        kind_of[dependent_id] = dependent_kind
        routes = shortest.setdefault(dependent_id, [])
        if not routes or routes[0][1] == length:
            routes.append((dependent_name, length, ids, names))

    saturation = max((routes[0][1] for routes in shortest.values()), default=0)
    reached = wanted if wanted is not None else saturation
    result = ToolResult(tool="blast_radius", answer="", candidates=resolution.candidate_names(), depth=max(reached, 1))
    if not shortest:
        result.answer = f"Nothing in the graph depends on {subject}."
        return result

    affected = {dep_id: routes for dep_id, routes in shortest.items() if routes[0][1] <= reached}
    for routes in affected.values():
        for _name, _length, ids, names in routes:
            for index in range(len(ids) - 1):
                result.add(PathStep(names[index], "DEPENDS_ON", names[index + 1], from_id=ids[index], to_id=ids[index + 1], from_label="Service", to_label="Service"))

    stops: dict[int, list[str]] = {}
    clauses: list[tuple[int, str, str]] = []
    for dep_id, routes in affected.items():
        name, length, _ids, _names = routes[0]
        kind = kind_of[dep_id]
        if kinds.kind_or_default(kind) in kinds.GOES_DOWN:
            stops.setdefault(length, []).append(kinds.listed(name, kind))
        else:
            clauses.append((length, name, f"{kinds.subject(name, kind)} {kinds.down(kind)} ({length} hop{'s' if length > 1 else ''})"))
    lead = f"If {subject} {kinds.down(target.kind)}"
    parts = []
    if stops:
        listed = ", ".join(f"{join_names(sorted(names))} ({hop} hop{'s' if hop > 1 else ''})" for hop, names in sorted(stops.items()))
        parts.append(f"{lead}, these stop with it: {listed}.")
    if clauses:
        effects = join_names([clause for _length, _name, clause in sorted(clauses)])
        parts.append(f"{kinds.capitalized(effects)}." if stops else f"{lead}, {effects}.")
    beyond = [(kinds.listed(routes[0][0], kind_of[dep_id]), routes[0][1], kind_of[dep_id]) for dep_id, routes in shortest.items() if routes[0][1] > reached]
    if beyond:
        further = join_names([f"{name} at depth {length}" for name, length, _kind in sorted(beyond, key=lambda row: (row[1], row[0]))])
        count = len(beyond)
        if all(kinds.kind_or_default(kind) in kinds.GOES_DOWN for _name, _length, kind in beyond):
            parts.append(f"{count} more break{'s' if count == 1 else ''} further out ({further}); ask with more hops.")
        else:
            parts.append(f"{count} more depend{'s' if count == 1 else ''} on it further out ({further}); ask with more hops.")
    elif clauses:
        count = len(affected)
        parts.append(f"Depth {reached} reached {count} thing{'s' if count != 1 else ''} that depend{'s' if count == 1 else ''} on it; nothing new past depth {saturation}.")
    else:
        parts.append(f"Depth {reached} reached {len(affected)} service{'s' if len(affected) != 1 else ''}; nothing new past depth {saturation}.")
    connectors = main_connectors(shortest, affected)
    if connectors is not None:
        connector_ids, through = connectors
        named = join_names([kinds.subject(affected[node_id][0][0], kind_of[node_id]) for node_id in connector_ids])
        reach = f"{through} of the {len(shortest)} reach{'es' if through == 1 else ''} {subject} through"
        if len(connector_ids) == 1:
            parts.append(f"Among them, {named} is the main connector: {reach} it.")
        else:
            parts.append(f"Among them, {named} are the main connectors: {reach} each.")
    result.answer = " ".join(parts)
    return result


def _dependencies(graph, target: Entity, resolution, wanted: int | None, scopes: list[str]) -> ToolResult:
    """What a service depends on: DEPENDS_ON followed forward, nearest first, each at its shortest hop count.

    Grouped by hop in the words blast_radius lists services in: "Invoice Worker depends on
    Billing Service (1 hop) and, through it, Postgres (2 hops)". Every node on a route must be
    readable and no account is ever on one, as for any path.
    """
    rows = graph.ro_query(
        f"""
        MATCH path = (service:Service {{id: $id}})-[:DEPENDS_ON*1..{MAX_DEPTH}]->(dependency:Service)
        WHERE {_SCOPED_PATH} AND all(n IN nodes(path) WHERE NOT {kinds.KIND_OF.format(var="n")} IN $excluded)
        RETURN dependency.id, dependency.name, length(path), [n IN nodes(path) | n.id], [n IN nodes(path) | n.name],
               {kinds.KIND_OF.format(var="dependency")}
        ORDER BY length(path), dependency.name
        """,
        {"id": target.id, "excluded": sorted(kinds.NOT_DEPENDABLE), "scopes": scopes},
    ).result_set
    shortest: dict[str, list[tuple[str, int, list[str], list[str]]]] = {}
    kind_of: dict[str, str] = {}
    for dependency_id, dependency_name, length, ids, names, dependency_kind in rows:
        kind_of[dependency_id] = dependency_kind
        routes = shortest.setdefault(dependency_id, [])
        if not routes or routes[0][1] == length:
            routes.append((dependency_name, length, ids, names))

    subject = kinds.capitalized(kinds.subject(target.name, target.kind))
    saturation = max((routes[0][1] for routes in shortest.values()), default=0)
    reached = wanted if wanted is not None else saturation
    result = ToolResult(tool="blast_radius", answer="", candidates=resolution.candidate_names(), depth=max(reached, 1))
    if not shortest:
        result.answer = f"{subject} depends on nothing else in the graph."
        return result

    needed = {node_id: routes for node_id, routes in shortest.items() if routes[0][1] <= reached}
    for routes in needed.values():
        # Equal-length routes come back in any order; cited by name, the path is the same on every run.
        for _name, _length, ids, names in sorted(routes, key=lambda route: route[3]):
            for index in range(len(ids) - 1):
                result.add(PathStep(names[index], "DEPENDS_ON", names[index + 1], from_id=ids[index], to_id=ids[index + 1], from_label="Service", to_label="Service"))

    by_hop: dict[int, list[tuple[str, str]]] = {}
    for node_id, routes in needed.items():
        by_hop.setdefault(routes[0][1], []).append((routes[0][0], kind_of[node_id]))
    groups = [
        f"{join_names([kinds.subject(name, kind) for name, kind in sorted(things)])} ({hop} hop{'s' if hop > 1 else ''})"
        for hop, things in sorted(by_hop.items())
    ]
    nearest = by_hop[min(by_hop)]
    if len(groups) == 1:
        parts = [f"{subject} depends on {groups[0]}."]
    else:
        parts = [f"{subject} depends on {groups[0]} and, through {'it' if len(nearest) == 1 else 'them'}, {', then '.join(groups[1:])}."]
    beyond = sorted((routes[0][1], kinds.listed(routes[0][0], kind_of[node_id])) for node_id, routes in shortest.items() if routes[0][1] > reached)
    if beyond:
        further = join_names([f"{name} at depth {length}" for length, name in beyond])
        parts.append(f"It needs {len(beyond)} more further down ({further}); ask with more hops.")
    else:
        noun = "service" if all(kinds.kind_or_default(kind_of[node_id]) in kinds.GOES_DOWN for node_id in needed) else "thing"
        parts.append(f"Depth {reached} reached {len(needed)} {noun}{'s' if len(needed) != 1 else ''}; nothing new past depth {saturation}.")
    result.answer = " ".join(parts)
    return result


def main_connectors(shortest: dict[str, list[tuple]], affected: dict[str, list[tuple]]) -> tuple[list[str], int] | None:
    """The affected nodes the most dependents reach the failed one through, and how many, or None.

    Counted from the shortest routes the walk found, out to saturation: a dependent counts
    once for each affected node on any of its shortest routes. Whole numbers, the same on
    every run. Nodes tied for the most are all returned, by name.
    """
    through: dict[str, int] = {}
    for routes in shortest.values():
        # A route's ids run from the dependent to the failed node; the ones between are on its way.
        on_the_way = {node_id for _name, _length, ids, _names in routes for node_id in ids[1:-1]}
        for node_id in on_the_way & affected.keys():
            through[node_id] = through.get(node_id, 0) + 1
    if not through:
        return None
    most = max(through.values())
    return sorted((node_id for node_id, count in through.items() if count == most), key=lambda node_id: affected[node_id][0][0]), most


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

    rows, searched = _shortest_paths(graph, start, end, KNOWLEDGE_RELS, scopes)
    if not rows:
        rows, wider = _shortest_paths(graph, start, end, KNOWLEDGE_RELS + TEAM_RELS, scopes)
        searched = min(searched, wider)
    if not rows:
        return ToolResult(
            tool="path_between",
            answer=f"No path between {start.name} and {end.name} within {searched} hop{'s' if searched != 1 else ''} in what you can read.",
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


def _node(var: str, entity: Entity) -> str:
    """``(a:Label {id: $a})``: a labelled match uses the id index. The label is the resolver's, checked anyway."""
    label = f":{entity.label}" if _LABEL.match(entity.label or "") else ""
    return f"({var}{label} {{id: ${var}}})"


def _shortest_paths(graph, start: Entity, end: Entity, rels: list[str], scopes: list[str]) -> tuple[list[tuple[list, int]], int]:
    """The shortest routes the asker may read, and how many hops were searched when there are none."""
    rows = graph.ro_query(
        f"""
        MATCH {_node("a", start)}, {_node("b", end)}
        CALL algo.SPpaths({{sourceNode: a, targetNode: b, relTypes: $rels, relDirection: 'both', maxLen: {MAX_PATH_LENGTH}, pathCount: 0}})
        YIELD path
        RETURN [i IN range(0, length(path) - 1) | [
                    nodes(path)[i].id, nodes(path)[i].name, labels(nodes(path)[i])[0],
                    type(relationships(path)[i]),
                    nodes(path)[i + 1].id, nodes(path)[i + 1].name, labels(nodes(path)[i + 1])[0],
                    startNode(relationships(path)[i]) = nodes(path)[i]
               ]] AS hops,
               length(path), {_SCOPED_PATH}
        """,
        {"a": start.id, "b": end.id, "rels": rels, "scopes": scopes},
    ).result_set
    if not rows:
        return [], MAX_PATH_LENGTH  # no route at all, readable or not
    readable = [(hops, length) for hops, length, ok in rows if ok]
    if readable:
        # Some shortest route is readable, so the readable ones are the shortest the asker can have.
        return readable, MAX_PATH_LENGTH
    # Every shortest route runs through something hidden (a Jira ticket, a private repo). Those
    # must not hide a longer route the asker may see, nor hint that a shortcut exists.
    return _readable_shortest_paths(graph, start, end, rels, scopes)


def _readable_shortest_paths(graph, start: Entity, end: Entity, rels: list[str], scopes: list[str]) -> tuple[list[tuple[list, int]], int]:
    """Every shortest route (at most ``MAX_ROUTES``) through nodes the asker may read, by a
    breadth-first search one hop at a time, in both edge directions, up to ``MAX_PATH_LENGTH``.

    Each hop looks nodes up by label, so the id indexes are used, and the search stops once it
    has seen ``MAX_SEARCHED`` nodes. With no route it returns how many hops it covered in full.
    """
    seen = {start.id: (0, start.name, start.label)}
    parents: dict[str, list[tuple[str, str, bool]]] = {}
    frontier = [start.id]
    searched = 0
    for depth in range(1, MAX_PATH_LENGTH + 1):
        by_label: dict[str, list[str]] = defaultdict(list)
        for node_id in frontier:
            by_label[seen[node_id][2]].append(node_id)
        frontier = []
        for label, ids in sorted(by_label.items()):
            if not _LABEL.match(label or ""):
                continue
            rows = graph.ro_query(
                f"""
                MATCH (n:{label})-[r]-(m)
                WHERE n.id IN $ids AND type(r) IN $rels AND {resolve.SCOPED.format(var="m")}
                RETURN n.id, type(r), startNode(r) = n, m.id, m.name, labels(m)[0]
                """,
                {"ids": ids, "rels": rels, "scopes": scopes},
            ).result_set
            for from_id, rel, forward, to_id, to_name, to_label in rows:
                if to_id is None:
                    continue
                if to_id not in seen:
                    seen[to_id] = (depth, to_name, to_label)
                    frontier.append(to_id)
                if seen[to_id][0] == depth and (from_id, rel, forward) not in parents.setdefault(to_id, []):
                    parents[to_id].append((from_id, rel, forward))
        searched = depth
        if end.id in seen or not frontier:
            break
        if len(seen) > MAX_SEARCHED:
            log.info("[path_between] readable search stopped at %d nodes, %d hops", len(seen), depth)
            break
    if end.id not in seen:
        return [], searched if frontier else MAX_PATH_LENGTH
    routes: list[list] = []

    def walk(node_id: str, tail: list) -> None:
        if len(routes) >= MAX_ROUTES:
            return
        if node_id == start.id:
            routes.append(tail[::-1])
            return
        for from_id, rel, forward in parents.get(node_id, []):
            walk(from_id, [*tail, (from_id, rel, forward, node_id)])

    walk(end.id, [])
    hops = [
        [[a, seen[a][1], seen[a][2], rel, b, seen[b][1], seen[b][2], forward] for a, rel, forward, b in route]
        for route in routes
    ]
    return [(route, len(route)) for route in hops], MAX_PATH_LENGTH


# --- PageRank ------------------------------------------------------------------------------


def pagerank_by_id(graph) -> dict[str, float]:
    """Every Service's PageRank over DEPENDS_ON, by id: the same call ``load_bearing`` ranks by,
    over the same unfiltered nodes, so the scores agree. FalkorDB's scores shift with everything
    else in the graph, so compare them within one graph, never across seeds."""
    rows = graph.ro_query("CALL algo.pageRank('Service', 'DEPENDS_ON') YIELD node, score RETURN node.id, score").result_set
    return {service_id: score for service_id, score in rows}


def load_bearing(limit=DEFAULT_LOAD_BEARING, scopes: list[str] | None = None) -> ToolResult:
    """The services the rest of the company leans on: PageRank over DEPENDS_ON.

    Only services the asker may read are ranked, and the dependents counted for each are the ones
    reached over readable routes. No scopes means the company-wide ``org_shared`` only.
    """
    try:
        count = max(1, min(MAX_LOAD_BEARING, int(limit)))
    except (TypeError, ValueError):
        count = DEFAULT_LOAD_BEARING
    scopes = list(scopes) if scopes else [resolve.ORG_SHARED]
    _, graph = connect()
    ranked = graph.ro_query(
        f"""
        CALL algo.pageRank('Service', 'DEPENDS_ON') YIELD node, score
        WITH node, score
        WHERE NOT {kinds.KIND_OF.format(var="node")} IN $excluded AND {resolve.SCOPED.format(var="node")}
        RETURN node.id, node.name, score
        ORDER BY score DESC, node.name
        LIMIT $limit
        """,
        {"limit": count, "excluded": sorted(kinds.NOT_DEPENDABLE), "scopes": scopes},
    ).result_set
    result = ToolResult(tool="load_bearing", answer="")
    if not ranked:
        result.answer = "The graph has no services with dependencies yet."
        return result

    sentences = []
    for index, (service_id, name, score) in enumerate(ranked):
        dependents = graph.ro_query(
            f"""
            MATCH path = (dependent:Service)-[:DEPENDS_ON*1..{MAX_DEPTH}]->(service:Service {{id: $id}})
            WHERE {_SCOPED_PATH}
            RETURN count(DISTINCT dependent)
            """,
            {"id": service_id, "scopes": scopes},
        ).result_set[0][0]
        direct = graph.ro_query(
            f"""
            MATCH (dependent:Service)-[:DEPENDS_ON]->(service:Service {{id: $id}})
            WHERE {resolve.SCOPED.format(var="dependent")}
            RETURN dependent.id, dependent.name
            ORDER BY dependent.name
            """,
            {"id": service_id, "scopes": scopes},
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


# --- label propagation --------------------------------------------------------------------

# People are not linked to each other. Knowing a service is a weak bridge: include it and
# label propagation returns one community for the whole component. Owning a service or
# having worked on it is the de facto team. Each such service with two or more people
# becomes a hub on a throwaway graph; the call below is deleted with that graph.
_STRONG = {"OWNS", "WORKED_ON"}


def working_groups(scopes: list[str]) -> ToolResult:
    """Who clusters together from hands-on work, and where that splits from the org chart.

    ``algo.labelPropagation`` runs on a throwaway graph: one hub per service that two or
    more people own or worked on, linked both ways. ``KNOWS`` is left out of that graph,
    because it glues every team into one community. The company graph is only read. The
    reply cites the real work edges and ``MEMBER_OF``.
    """
    _, graph = connect()
    work = graph.ro_query(
        """
        MATCH (person:Person)-[rel:OWNS|WORKED_ON|KNOWS]->(service:Service)
        WHERE coalesce(service.scope, 'org_shared') IN $scopes
        RETURN person.id, person.name, type(rel), service.id, service.name
        ORDER BY person.name, service.name, type(rel)
        """,
        {"scopes": scopes},
    ).result_set
    teams = graph.ro_query(
        """
        MATCH (person:Person)-[:MEMBER_OF]->(team:Team)
        RETURN person.id, team.id, team.name
        ORDER BY team.name
        """,
        {},
    ).result_set
    result = ToolResult(tool="working_groups", answer="")
    if not work:
        result.answer = "The graph has no shared work between people yet."
        return result

    by_person: dict[str, dict] = {}
    by_service: dict[str, dict] = {}
    for person_id, person_name, rel, service_id, service_name in work:
        person = by_person.setdefault(person_id, {"name": person_name, "edges": []})
        person["edges"].append((rel, service_id, service_name))
        if rel not in _STRONG:
            continue
        service = by_service.setdefault(service_id, {"name": service_name, "people": {}})
        service["people"].setdefault(person_id, rel)
    team_of = {person_id: (team_id, team_name) for person_id, team_id, team_name in teams}
    hubs = {service_id: service["people"] for service_id, service in by_service.items() if len(service["people"]) >= 2}
    communities = _propagate_hubs(sorted(by_person), hubs)
    if not communities:
        result.answer = "The graph has no shared work between people yet."
        return result

    clusters: dict[int, list[str]] = defaultdict(list)
    for person_id, community in communities.items():
        clusters[community].append(person_id)
    ordered = sorted(clusters.values(), key=lambda members: [by_person[person]["name"] for person in members])
    grouped = [members for members in ordered if len(members) > 1]
    alone = [members[0] for members in ordered if len(members) == 1]

    count = len(grouped)
    sentences = [
        f"Label propagation over who owns or worked on the same service finds {count} work cluster{'s' if count != 1 else ''}."
    ]
    crossed = 0
    for members in grouped:
        names = [by_person[person]["name"] for person in sorted(members, key=lambda person: by_person[person]["name"])]
        sentences.append(f"{join_names(names)}.")
        org = _org_chart_sentence(members, by_person, team_of)
        sentences.append(org)
        if org.startswith("The org chart splits"):
            crossed += 1
        sentences.extend(_shared_services(members, by_person, by_service))
        _cite_cluster(result, members, by_person, by_service, team_of)
    for person in alone:
        team = team_of.get(person)
        where = f" They are on {team[1]}, and" if team else ""
        sentences.append(f"{by_person[person]['name']} is on their own.{where} no service they own or worked on is shared.")
        _cite_person(result, person, by_person, team_of)
    sentences.extend(_knows_outside(by_person, by_service, communities, result))
    if crossed:
        verb = "crosses" if crossed == 1 else "cross"
        sentences.append(f"{crossed} of those clusters {verb} the org chart.")
    result.answer = " ".join(sentences)
    return result


def _org_chart_sentence(members: list[str], by_person: dict, team_of: dict) -> str:
    grouped: dict[str, list[str]] = defaultdict(list)
    for person in members:
        team = team_of.get(person)
        grouped[team[1] if team else "no team"].append(by_person[person]["name"])
    parts = []
    for team, names in sorted(grouped.items()):
        names = sorted(names)
        verb = "is" if len(names) == 1 else "are"
        parts.append(f"{join_names(names)} {verb} on {team}")
    if len(grouped) == 1:
        return f"That matches the org chart: {parts[0]}."
    return f"The org chart splits them: {join_names(parts)}."


def _shared_services(members: list[str], by_person: dict, by_service: dict) -> list[str]:
    member_set = set(members)
    sentences = []
    for _service_id, service in sorted(by_service.items(), key=lambda item: item[1]["name"]):
        here = sorted((person for person in service["people"] if person in member_set), key=lambda person: by_person[person]["name"])
        if len(here) < 2:
            continue
        names = [by_person[person]["name"] for person in here]
        sentences.append(f"Inside the cluster, {join_names(names)} share {service['name']}.")
    return sentences


def _knows_outside(by_person: dict, by_service: dict, communities: dict, result: ToolResult) -> list[str]:
    """Someone who only knows a service a cluster shares stays outside. Cite that KNOWS edge."""
    sentences = []
    for person_id, person in sorted(by_person.items(), key=lambda item: item[1]["name"]):
        for rel, service_id, service_name in person["edges"]:
            if rel != "KNOWS" or service_id not in by_service:
                continue
            sharers = by_service[service_id]["people"]
            if len(sharers) < 2 or person_id in sharers:
                continue
            if any(communities.get(other) == communities.get(person_id) for other in sharers):
                continue
            sentences.append(
                f"{person['name']} knows {service_name} and stays outside that cluster: knowing a service is not shared work."
            )
            result.add(PathStep(person["name"], "KNOWS", service_name, from_id=person_id, to_id=service_id, from_label="Person", to_label="Service"))
    return sentences


def _cite_cluster(result: ToolResult, members: list[str], by_person: dict, by_service: dict, team_of: dict) -> None:
    member_set = set(members)
    for person in members:
        _cite_membership(result, person, by_person, team_of)
        for rel, service_id, service_name in by_person[person]["edges"]:
            if rel not in _STRONG or service_id not in by_service:
                continue
            others = [other for other in by_service[service_id]["people"] if other != person and other in member_set]
            if others:
                result.add(PathStep(by_person[person]["name"], rel, service_name, from_id=person, to_id=service_id, from_label="Person", to_label="Service"))


def _cite_person(result: ToolResult, person: str, by_person: dict, team_of: dict) -> None:
    _cite_membership(result, person, by_person, team_of)
    for rel, service_id, service_name in by_person[person]["edges"]:
        if rel in _STRONG:
            result.add(PathStep(by_person[person]["name"], rel, service_name, from_id=person, to_id=service_id, from_label="Person", to_label="Service"))


def _cite_membership(result: ToolResult, person: str, by_person: dict, team_of: dict) -> None:
    team = team_of.get(person)
    if team is not None:
        result.add(PathStep(by_person[person]["name"], "MEMBER_OF", team[1], from_id=person, to_id=team[0], from_label="Person", to_label="Team"))


def _propagate_hubs(people: list[str], hubs: dict[str, dict]) -> dict[str, int]:
    """Community id per person. The scratch graph is deleted before this returns.

    A person–person edge of two nodes does not settle under label propagation (the labels
    swap each iteration). A hub both of them point at does. The call is skipped when no
    service is shared, because FalkorDB rejects a relationship type that is not in the graph.
    """
    if graph_reads_disabled.get() or not hubs:
        return {person: index for index, person in enumerate(people)}
    scratch = client().select_graph(f"brain_lp_{uuid.uuid4().hex}")
    try:
        for person in people:
            scratch.query("CREATE (:Person {id: $id})", {"id": person})
        for service_id, members in sorted(hubs.items()):
            scratch.query("CREATE (:Work {id: $id})", {"id": service_id})
            for person in sorted(members):
                scratch.query(
                    "MATCH (p:Person {id: $p}), (w:Work {id: $w}) CREATE (p)-[:AT]->(w), (w)-[:AT]->(p)",
                    {"p": person, "w": service_id},
                )
        rows = scratch.ro_query(
            """
            CALL algo.labelPropagation({nodeLabels: ['Person', 'Work'], relationshipTypes: ['AT']})
            YIELD node, communityId
            RETURN labels(node)[0], node.id, communityId
            """
        ).result_set
        return {node_id: int(community) for label, node_id, community in rows if label == "Person"}
    finally:
        try:
            scratch.delete()
        except Exception as error:  # noqa: BLE001  the scratch graph must not outlive the call
            log.warning("[working_groups] scratch graph cleanup failed: %s", error)
