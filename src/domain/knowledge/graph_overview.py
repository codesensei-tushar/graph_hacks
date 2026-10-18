"""The real graph, for the landing page's hero: counts, and a small connected sample to draw.

Read-only and public, like the ask page: both read what a public channel reads
(``org_shared``), so the page never shows, or counts, a private channel's memories or a
private repo's pull requests. A memory counts only when it is public and not forgotten.

``graph_stats`` counts nodes by label (only the labels a reader would recognise), Service
nodes by kind, and the edges between counted nodes. ``graph_sample`` seeds from the
services the most others depend on (PageRank over ``DEPENDS_ON``, as ``load_bearing``
ranks them), takes their neighbours one hop out, and returns every edge among the chosen
nodes, in the path drawing's edge shape. Both are the same on every call for a graph:
PageRank is not sampled, and every order is fixed.
"""

from dataclasses import dataclass, field

from src.db.falkordb_client import connect
from src.domain.knowledge import companies, kinds
from src.domain.knowledge.ask_api import ANSWERS, cache_key, GraphEdge, read_tools
from src.domain.knowledge.resolve import ORG_SHARED, SCOPED

DEFAULT_SCOPES = (ORG_SHARED,)
COUNTED_LABELS = ("Person", "Team", "Service", "Document", "Ticket", "Decision", "PullRequest", "Memory", "Chat")
# The sample draws what a newcomer reads at a glance: no memory text, no pull request numbers.
SAMPLE_LABELS = ("Service", "Person", "Team", "Decision", "Ticket", "Document")
# One hop out from a seed, in the order the sample fills: who runs and owns it first, then what is about it.
SAMPLE_RELS = ("RUNS", "OWNS", "WORKED_ON", "ABOUT", "DESCRIBES", "DEPENDS_ON", "KNOWS", "MEMBER_OF", "AUTHORED", "PROPOSED", "APPROVED", "SUPERSEDES")
DEFAULT_SAMPLE = 40
MAX_SAMPLE = 60

# A node a public reader can see. Memories carry their room's scope and can be forgotten.
_VISIBLE = (
    "CASE WHEN labels({var})[0] = 'Memory' "
    "THEN {var}.scope IN $scopes AND NOT coalesce({var}.is_forgotten, false) "
    "ELSE coalesce({var}.scope, 'org_shared') IN $scopes END"
)


@dataclass(frozen=True)
class GraphStats:
    nodes: int
    edges: int
    labels: dict[str, int]
    kinds: dict[str, int]
    rels: int
    tools: int
    company: str | None = None
    company_name: str | None = None

    def to_dict(self) -> dict:
        result = {}
        if self.company:
            result["company"] = self.company
            result["company_name"] = self.company_name or self.company
        result.update(nodes=self.nodes, edges=self.edges, labels=dict(self.labels), kinds=dict(self.kinds), rels=self.rels, tools=self.tools)
        return result


@dataclass(frozen=True)
class SampleNode:
    id: str
    name: str
    label: str
    # A Service's kind; None for every other label.
    kind: str | None = None

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "label": self.label, "kind": self.kind}


@dataclass
class GraphSample:
    nodes: list[SampleNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"nodes": [node.to_dict() for node in self.nodes], "edges": [edge.to_dict() for edge in self.edges]}


def clamp_sample(value) -> int:
    """An int in 1..MAX_SAMPLE; anything that is not a number is the default."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SAMPLE
    return max(1, min(MAX_SAMPLE, number))


def public_stats() -> GraphStats:
    return ANSWERS.get_or_compute(cache_key("graph_stats"), graph_stats)


def public_sample(limit=DEFAULT_SAMPLE) -> GraphSample:
    size = clamp_sample(limit)
    return ANSWERS.get_or_compute(cache_key("graph_sample", size), lambda: graph_sample(size))


def graph_stats(scopes=DEFAULT_SCOPES) -> GraphStats:
    _, graph = connect()
    params = {"labels": list(COUNTED_LABELS), "scopes": list(scopes)}
    by_label = dict(
        graph.ro_query(
            f"""
            MATCH (n)
            WHERE labels(n)[0] IN $labels AND {_VISIBLE.format(var="n")}
            RETURN labels(n)[0], count(n)
            """,
            params,
        ).result_set
    )
    by_kind = graph.ro_query(
        f"""
        MATCH (s:Service)
        WHERE {SCOPED.format(var="s")}
        RETURN {kinds.KIND_OF.format(var="s")}, count(s)
        """,
        params,
    ).result_set
    by_rel = graph.ro_query(
        f"""
        MATCH (a)-[r]->(b)
        WHERE labels(a)[0] IN $labels AND labels(b)[0] IN $labels AND {_VISIBLE.format(var="a")} AND {_VISIBLE.format(var="b")}
        RETURN type(r), count(r)
        """,
        params,
    ).result_set
    company_id, company_name = companies.primary_company(graph)
    labels = {label: int(by_label[label]) for label in COUNTED_LABELS if by_label.get(label)}
    kind_counts: dict[str, int] = {}
    for kind, count in by_kind:
        kind = kinds.kind_or_default(kind)
        kind_counts[kind] = kind_counts.get(kind, 0) + int(count)
    return GraphStats(
        nodes=sum(labels.values()),
        edges=sum(int(count) for _rel, count in by_rel),
        labels=labels,
        kinds={kind: kind_counts[kind] for kind in kinds.SERVICE_KINDS if kind in kind_counts},
        rels=len(by_rel),
        tools=len(read_tools()),
        company=company_id,
        company_name=company_name,
    )


def graph_sample(limit=DEFAULT_SAMPLE, scopes=DEFAULT_SCOPES) -> GraphSample:
    """A connected neighbourhood around the most depended-on services, at most ``limit`` nodes."""
    size = clamp_sample(limit)
    _, graph = connect()
    params = {"labels": list(SAMPLE_LABELS), "rels": list(SAMPLE_RELS), "scopes": list(scopes)}
    seeds = graph.ro_query(
        f"""
        CALL algo.pageRank('Service', 'DEPENDS_ON') YIELD node, score
        WITH node, score
        WHERE {SCOPED.format(var="node")}
        RETURN node.id, node.name, node.kind
        ORDER BY score DESC, node.name
        """,
        params,
    ).result_set
    if not seeds:
        return GraphSample()
    rank = {service_id: index for index, (service_id, _name, _kind) in enumerate(seeds)}
    neighbours = graph.ro_query(
        f"""
        MATCH (s:Service)-[r]-(n)
        WHERE s.id IN $seeds AND {SCOPED.format(var="s")}
          AND type(r) IN $rels AND labels(n)[0] IN $labels AND {SCOPED.format(var="n")}
        RETURN DISTINCT s.id, type(r), n.id, n.name, labels(n)[0], n.kind
        """,
        {**params, "seeds": list(rank)},
    ).result_set
    around: dict[str, list[tuple]] = {}
    for seed_id, rel, node_id, name, label, kind in neighbours:
        around.setdefault(seed_id, []).append((SAMPLE_RELS.index(rel), SAMPLE_LABELS.index(label), name or node_id, node_id, label, kind))

    chosen: dict[str, SampleNode] = {}
    for service_id, name, kind in seeds:
        if len(chosen) >= size:
            break
        chosen.setdefault(service_id, SampleNode(service_id, name, "Service", kinds.kind_or_default(kind)))
        for _rel, _label, name, node_id, label, kind in sorted(around.get(service_id, []), key=lambda row: row[:4]):
            if len(chosen) >= size:
                break
            chosen.setdefault(node_id, SampleNode(node_id, name, label, kinds.kind_or_default(kind) if label == "Service" else None))

    rows = graph.ro_query(
        f"""
        MATCH (a)-[r]->(b)
        WHERE a.id IN $ids AND b.id IN $ids AND type(r) IN $rels
          AND labels(a)[0] IN $labels AND labels(b)[0] IN $labels AND {SCOPED.format(var="a")} AND {SCOPED.format(var="b")}
        RETURN a.id, type(r), b.id
        ORDER BY a.id, type(r), b.id
        """,
        {**params, "ids": list(chosen)},
    ).result_set
    edges = list(dict.fromkeys(GraphEdge(source, rel, target) for source, rel, target in rows))
    # Drawn as one picture: a seed with nothing around it in the sample is left out.
    linked = {end for edge in edges for end in (edge.source, edge.target)}
    nodes = [node for node in chosen.values() if node.id in linked] if edges else list(chosen.values())[:1]
    return GraphSample(nodes, edges)
