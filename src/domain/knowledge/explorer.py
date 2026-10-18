"""What the explorer page at ``/explore`` reads: one node's neighbourhood, or one relationship walked from it.

Thin over the general tools: ``explore`` and ``neighbors`` answer, and this module shapes
what they return for a page to draw and walk. A view carries the node in focus, the nodes
and edges of the cited path keyed by id (``ask_api.graph_of``, so the drawing is the one
the ask page draws), the relationship types a reader can follow from the focus with
``neighbors``, and, when nothing matches, the nearest nodes with their ids so the page can
offer them. ``starters`` names a few well-connected nodes to begin from.

A node is named by id (what the page links with: ``/explore?node=billing-service``) or by
name (what a reader types); ``explore.resolve_node`` resolves both, within the public
scope only, like every other public read. Views are cached for ``ask_api.CACHE_TTL_SECONDS``
per graph, so a walk back along the breadcrumb is answered from memory.
"""

from dataclasses import dataclass, field

from src.db import falkordb_client
from src.db.falkordb_client import connect
from src.domain.knowledge import ask_api, explore, resolve
from src.domain.knowledge.ask_api import ANSWERS, GraphEdge, GraphNode, QuestionTooLong
from src.domain.knowledge.dto import ToolResult
from src.domain.knowledge.kinds import kind_or_default
from src.domain.knowledge.resolve import ORG_SHARED, SCOPED, Entity, Resolution

DEFAULT_SCOPES = (ORG_SHARED,)
MAX_NODE_CHARS = ask_api.MAX_QUESTION_CHARS
# Nodes a walk may start from, two of each label, the best connected first.
STARTER_LABELS = ("Service", "Team", "Person", "Decision")
STARTERS_PER_LABEL = 2

_DEGREES = f"""
MATCH (n)
WHERE labels(n)[0] IN $labels AND n.name IS NOT NULL AND {SCOPED.format(var="n")}
RETURN n.id, n.name, labels(n)[0], n.kind,
       size([(n)-[r]-(m) WHERE NOT type(r) IN $hidden AND {SCOPED.format(var="m")} | m.id]) AS degree
ORDER BY degree DESC, n.name
"""


@dataclass(frozen=True)
class Follow:
    """A relationship type the reader can walk from the focus with neighbors, and how many it reaches in one hop."""

    rel: str
    direction: str
    phrase: str
    count: int

    def to_dict(self) -> dict:
        return {"rel": self.rel, "direction": self.direction, "phrase": self.phrase, "count": self.count}


@dataclass
class ExplorerView:
    tool: str
    query: str
    found: bool
    answer: str
    focus: GraphNode | None = None
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    path: list[dict] = field(default_factory=list)
    # On a miss: the nearest nodes, each {id, name, label, via}, so the page can link them by id.
    candidates: list[dict] = field(default_factory=list)
    follows: list[Follow] = field(default_factory=list)
    memories: list[dict] = field(default_factory=list)
    depth: int | None = None
    relation: str | None = None
    direction: str | None = None

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "query": self.query,
            "found": self.found,
            "answer": self.answer,
            "focus": self.focus.to_dict() if self.focus else None,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "path": [dict(step) for step in self.path],
            "candidates": [dict(candidate) for candidate in self.candidates],
            "follows": [follow.to_dict() for follow in self.follows],
            "memories": [dict(memory) for memory in self.memories],
            "depth": self.depth,
            "relation": self.relation,
            "direction": self.direction,
            "scopes": list(DEFAULT_SCOPES),
        }


# --- public, cached --------------------------------------------------------------------------


def public_explore(node: str, depth=1) -> ExplorerView:
    text = _clean(node)
    wanted = _clamp(depth, explore.MAX_EXPLORE_DEPTH)
    return ANSWERS.get_or_compute(("explorer", falkordb_client.graph_name(), text, wanted), lambda: explore_view(text, wanted))


def public_neighbors(node: str, relation: str, direction: str | None = None, depth=1) -> ExplorerView:
    text = _clean(node)
    rel = (relation or "").strip()[:40]
    way = (direction or explore.BOTH).strip().lower()[:10]
    hops = _clamp(depth, explore.MAX_NEIGHBOR_DEPTH)
    key = ("explorer_neighbors", falkordb_client.graph_name(), text, rel, way, hops)
    return ANSWERS.get_or_compute(key, lambda: neighbors_view(text, rel, way, hops))


def public_starters() -> list[GraphNode]:
    return ANSWERS.get_or_compute(("explorer_starters", falkordb_client.graph_name()), starters)


# --- views -----------------------------------------------------------------------------------


def explore_view(node: str, depth: int = 1, scopes=DEFAULT_SCOPES) -> ExplorerView:
    """``explore`` around the node ``node`` names, shaped for the page."""
    if not node:
        return ExplorerView(tool=explore.EXPLORE, query=node, found=False, answer="Type the name of a service, person, team, document, ticket or decision.")
    _, graph = connect()
    resolution = explore.resolve_node(graph, node, list(scopes))
    if not resolution.found:
        return _missed(explore.EXPLORE, node, resolution)
    result = explore.explore_node(graph, resolution.entity, list(scopes), depth, resolution.candidate_names())
    return _shaped(node, resolution.entity, result, scopes)


def neighbors_view(node: str, relation: str, direction: str, depth: int = 1, scopes=DEFAULT_SCOPES) -> ExplorerView:
    """``neighbors`` from the node ``node`` names along ``relation``, shaped for the page."""
    _, graph = connect()
    resolution = explore.resolve_node(graph, node, list(scopes)) if node else Resolution(query=node)
    if not resolution.found:
        return _missed(explore.NEIGHBORS, node, resolution)
    result = explore.neighbors(resolution.entity.id, relation, list(scopes), direction, depth)
    view = _shaped(node, resolution.entity, result, scopes)
    if result.found:
        view.relation, view.direction = relation.upper(), direction
    return view


def starters(scopes=DEFAULT_SCOPES) -> list[GraphNode]:
    """A few nodes to start a walk from: the best connected of each label in ``STARTER_LABELS``."""
    _, graph = connect()
    rows = graph.ro_query(_DEGREES, {"labels": list(STARTER_LABELS), "scopes": list(scopes), "hidden": list(explore.HIDDEN_RELS)}).result_set
    picked: dict[str, list[GraphNode]] = {label: [] for label in STARTER_LABELS}
    for node_id, name, label, kind, degree in rows:
        if degree and len(picked[label]) < STARTERS_PER_LABEL:
            picked[label].append(GraphNode(node_id, name, label, kind_or_default(kind) if label == "Service" else None))
    return [node for label in STARTER_LABELS for node in picked[label]]


# --- shaping ---------------------------------------------------------------------------------


def _shaped(query: str, focus: Entity, result: ToolResult, scopes) -> ExplorerView:
    nodes, edges = ask_api.graph_of(result.path, ask_api.service_kinds(result.path, scopes))
    centre = next((node for node in nodes if node.id == focus.id), None) or GraphNode(focus.id, focus.name, focus.label, focus.kind)
    if centre not in nodes:
        nodes = [centre, *nodes]
    return ExplorerView(
        tool=result.tool,
        query=query,
        found=result.found,
        answer=result.answer,
        focus=centre,
        nodes=nodes,
        edges=edges,
        path=[step.to_dict() for step in result.path],
        follows=follows(focus.id, result),
        memories=[memory.to_dict() for memory in result.memories],
        depth=result.depth,
    )


def follows(focus_id: str, result: ToolResult) -> list[Follow]:
    """The relationship types the cited edges show at the focus, each way, that neighbors may follow."""
    reached: dict[tuple[str, str], set[str]] = {}
    labels: dict[tuple[str, str], set[str]] = {}
    for step in result.path:
        if step.rel not in explore.RELATIONS or focus_id not in (step.from_id, step.to_id) or step.from_id == step.to_id:
            continue
        outgoing = step.from_id == focus_id
        key = (step.rel, explore.OUT if outgoing else explore.IN)
        reached.setdefault(key, set()).add(step.to_id if outgoing else step.from_id)
        labels.setdefault(key, set()).add((step.to_label if outgoing else step.from_label) or "")
    order = {rel: index for index, rel in enumerate(explore.RELATIONS)}
    return [
        Follow(rel, way, _follow_phrase(rel, way == explore.OUT, labels[(rel, way)]), len(ids))
        for (rel, way), ids in sorted(reached.items(), key=lambda item: (order[item[0][0]], item[0][1] != explore.OUT))
    ]


def _follow_phrase(rel: str, outgoing: bool, labels: set[str]) -> str:
    """explore's words for the group, "tickets and decisions about it" when ABOUT brings in more than one label."""
    if len(labels) > 1 and (rel, outgoing) == ("ABOUT", False):
        return " and ".join(sorted(explore.plural(label) for label in labels)) + " about it"
    return explore.phrase(rel, outgoing, next(iter(labels))).rstrip(":")


def _missed(tool: str, query: str, resolution: Resolution) -> ExplorerView:
    kind = "service, person, team, document, ticket, decision, or pull request"
    return ExplorerView(
        tool=tool,
        query=query,
        found=False,
        answer=resolve.describe_miss(resolution, kind),
        candidates=[
            {"id": candidate.entity.id, "name": candidate.entity.name, "label": candidate.entity.label, "via": candidate.via}
            for candidate in resolution.candidates
        ],
    )


def _clean(node) -> str:
    text = " ".join(str(node or "").split())
    if len(text) > MAX_NODE_CHARS:
        raise QuestionTooLong(f"a node name is limited to {MAX_NODE_CHARS} characters; this one has {len(text)}")
    return text


def _clamp(value, ceiling: int) -> int:
    try:
        return max(1, min(ceiling, int(value)))
    except (TypeError, ValueError):
        return 1
