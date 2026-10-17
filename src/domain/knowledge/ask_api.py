"""One question over HTTP, answered read-only from the shared graph.

The web page and curl callers have no Slack identity, so they read what a public channel
reads: ``org_shared``. The keyword router picks the tool unless the caller asked for the
model and the route let it through (the admin token is checked there, not here). Tools
that write the graph never run from here: if the chooser or the caller names one, the
question is refused with ``ToolRefused`` and nothing is written.

The result carries the same ``A -[REL]-> B`` steps the Slack reply cites, plus the nodes
and edges of that path keyed by id, so the page can draw the traversal it is reading.
"""

from dataclasses import dataclass, field

from src.db.falkordb_client import connect
from src.domain.knowledge import agent, ask_service, catalogue
from src.domain.knowledge.dto import PathStep, ToolResult, ToolSpec

DEFAULT_SCOPES = ("org_shared",)

# Questions the page offers, per seeded company, in the order the demo asks them. Each
# exercises one tool; the last names something the graph does not have.
EXAMPLES: dict[str, list[str]] = {
    "quillfern": [
        "who owns billing, and who else has worked on it",
        "why did we move the memory layer off postgres",
        "what breaks if auth goes down",
        "how is maya connected to the invoice worker",
        "which services are load-bearing",
        "who actually works together",
        "is the billing retries decision still the last word",
        "who knows the payments gateway",
    ],
    "company-brain": [
        "who owns the memory layer, and who else has worked on it",
        "why do we store memory in FalkorDB instead of a hosted memory API",
        "what breaks if the claude api goes down",
        "how is the turn engine connected to the claude api",
        "which services are load-bearing",
        "who owns the linear integration",
    ],
}
_QUESTION_KEYS = ("name", "topic", "query", "source")


class ToolRefused(ValueError):
    """The question named a tool this surface will not run: one that writes, or one that does not exist."""


@dataclass(frozen=True)
class GraphNode:
    id: str
    name: str
    label: str

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "label": self.label}


@dataclass(frozen=True)
class GraphEdge:
    source: str
    rel: str
    target: str

    def to_dict(self) -> dict:
        return {"source": self.source, "rel": self.rel, "target": self.target}


@dataclass(frozen=True)
class ToolSummary:
    name: str
    description: str
    args: list[str]

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "args": list(self.args)}


@dataclass
class AskAnswer:
    question: str
    tool: str
    picked_by: str
    answer: str
    found: bool = True
    candidates: list[str] = field(default_factory=list)
    depth: int | None = None
    path: list[dict] = field(default_factory=list)
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    memories: list[dict] = field(default_factory=list)
    scopes: list[str] = field(default_factory=lambda: list(DEFAULT_SCOPES))

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "tool": self.tool,
            "picked_by": self.picked_by,
            "answer": self.answer,
            "found": self.found,
            "candidates": list(self.candidates),
            "depth": self.depth,
            "path": [dict(step) for step in self.path],
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "memories": [dict(memory) for memory in self.memories],
            "scopes": list(self.scopes),
        }


def read_tools() -> list[ToolSummary]:
    """The catalogue this surface can run: every tool that does not write the graph."""
    return [
        ToolSummary(spec.name, spec.description, list(spec.args_schema.get("properties", {})))
        for spec in catalogue.list_tools()
        if not spec.needs_approval
    ]


@dataclass(frozen=True)
class Examples:
    company_id: str | None
    company_name: str | None
    questions: list[str]


def examples() -> Examples:
    """The seeded company and the questions to offer for it. An unknown company gets the Quillfern set."""
    _, graph = connect()
    rows = graph.ro_query("MATCH (c:Company) RETURN c.id, c.name ORDER BY c.id LIMIT 1").result_set
    company_id, company_name = (rows[0][0], rows[0][1]) if rows else (None, None)
    return Examples(company_id, company_name, list(EXAMPLES.get(company_id or "", EXAMPLES["quillfern"])))


def answer_question(question: str, tool: str | None = None, use_model: bool = False, scopes=DEFAULT_SCOPES) -> AskAnswer:
    """Pick a read tool, run it, and shape the answer with the path it walked.

    ``tool`` names the tool outright and skips the chooser; its arguments are filled from
    the question. ``use_model`` lets the model pick (it falls back to keywords when no
    model is configured, exactly as the Slack turn does). A write tool is never run.
    """
    question = " ".join(question.split())
    read_scopes = list(scopes)
    if not question:
        return AskAnswer(question, "none", "keywords", "Ask a question about the company graph.", found=False, scopes=read_scopes)
    if tool is not None:
        spec = _read_spec(tool)
        choice = agent.ToolChoice(spec.name, _args_for(spec, question), "caller")
    else:
        choice = agent.choose_tool(question, scopes=read_scopes) if use_model else agent.choose_with_keywords(question)
        spec = _read_spec(choice.tool)
    result, choice = agent.run_choice(choice, question, read_scopes)
    return shape(question, result, choice.via, read_scopes)


def shape(question: str, result: ToolResult, picked_by: str, scopes: list[str]) -> AskAnswer:
    nodes, edges = graph_of(result.path)
    return AskAnswer(
        question=question,
        tool=result.tool,
        picked_by=picked_by,
        answer=result.answer,
        found=result.found,
        candidates=list(result.candidates),
        depth=result.depth,
        path=[step.to_dict() for step in result.path],
        nodes=nodes,
        edges=edges,
        memories=[memory.to_dict() for memory in result.memories],
        scopes=list(scopes),
    )


def graph_of(path: list[PathStep]) -> tuple[list[GraphNode], list[GraphEdge]]:
    """The nodes and edges of a path, each once, in order of first appearance.

    A step carries ids and labels when the tool knew them; a step without an id is keyed
    by its name, so two steps that cite the same name share one node.
    """
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []

    def node(node_id: str | None, name: str, label: str | None) -> str:
        key = node_id or name
        if key not in nodes:
            nodes[key] = GraphNode(key, name, label or "")
        elif label and not nodes[key].label:
            nodes[key] = GraphNode(key, nodes[key].name, label)
        return key

    for step in path:
        edge = GraphEdge(node(step.from_id, step.from_name, step.from_label), step.rel, node(step.to_id, step.to_name, step.to_label))
        if edge not in edges:
            edges.append(edge)
    return list(nodes.values()), edges


def _read_spec(name: str) -> ToolSpec:
    spec = catalogue.get_tool(name)
    if spec is None:
        raise ToolRefused(f"unknown tool {name!r}; the read-only catalogue is {', '.join(summary.name for summary in read_tools())}")
    if spec.needs_approval:
        raise ToolRefused(f"{spec.name} writes the graph; writes need approval, which happens in Slack")
    return spec


def _args_for(spec: ToolSpec, question: str) -> dict:
    """Arguments for a tool named outright, the way the keyword router fills them.

    When the router would have picked this tool anyway its arguments are used as they
    are (a depth, a limit); otherwise the question fills the tool's first text argument,
    and a second name argument is left blank for the tool to resolve from the question.
    """
    routed_tool, routed_args = ask_service.route(question)
    if routed_tool == spec.name:
        return dict(routed_args)
    properties = spec.args_schema.get("properties", {})
    args: dict = {}
    for key in _QUESTION_KEYS:
        if key in properties:
            args[key] = question
            break
    for key in spec.args_schema.get("required", []):
        if key not in args and properties.get(key, {}).get("type") == "string":
            args[key] = question if not args else ""
    return args
