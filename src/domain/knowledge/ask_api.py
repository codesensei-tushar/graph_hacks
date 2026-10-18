"""One question over HTTP, answered read-only from the shared graph.

The web page and curl callers have no Slack identity, so they read what a public channel
reads: ``org_shared``. The keyword router picks the tool unless the caller asked for the
model and the route let it through (the admin token is checked there, not here). Tools
that write the graph never run from here: if the chooser or the caller names one, the
question is refused with ``ToolRefused`` and nothing is written.

The result carries the same ``A -[REL]-> B`` steps the Slack reply cites, plus the nodes
and edges of that path keyed by id, so the page can draw the traversal it is reading.

The surface is public, so it is protected here rather than in the route: a question longer
than ``MAX_QUESTION_CHARS`` is refused with ``QuestionTooLong``; keyword answers and the
proof report are kept for ``CACHE_TTL_SECONDS`` per graph, so a chip clicked twice walks the
graph once; and ``admit`` allows each client ``RATE_LIMIT`` calls per ``RATE_WINDOW_SECONDS``.

The page offers an example chip only when it answers with a path on the graph in use, so a chip whose
data the live graph lacks stays hidden until a seed refresh brings it back.
"""

import logging
import math
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from src.db import falkordb_client
from src.db.falkordb_client import connect
from src.domain.knowledge import agent, ask_service, catalogue, kinds
from src.domain.knowledge.dto import PathStep, ToolResult, ToolSpec
from src.domain.knowledge.proof import ProofReport, run_proof

log = logging.getLogger(__name__)

DEFAULT_SCOPES = ("org_shared",)

# Questions the page offers, per seeded company, in the order the demo asks them. Each
# exercises one tool; the last names something the graph does not have. ``examples()``
# offers only the ones that answer with a path on the graph in use.
EXAMPLES: dict[str, list[str]] = {
    "quillfern": [
        "who owns billing, and who else has worked on it",
        "why did we move the memory layer off postgres",
        "what breaks if auth goes down",
        "how is maya connected to the invoice worker",
        "which services are load-bearing",
        "who actually works together",
        "is the billing retries decision still the last word",
        "who should know if postgres changes",
        "what have we said about billing",
        "what are our riskiest services",
        "what should I read before touching the invoice worker",
        "who knows about refunds",
        "who knows the payments gateway",
    ],
    "company-brain": [
        "who owns the memory layer, and who else has worked on it",
        "why do we store memory in FalkorDB instead of a hosted memory API",
        "what breaks if the claude api goes down",
        "how is the turn engine connected to the claude api",
        "which services are load-bearing",
        "is the vector recall decision still the last word",
        "who owns the linear integration",
    ],
}

# The numbered script under the ask box. Slack-only moves stay in the list so a judge
# sees where a write waits for approval; the page itself only runs the read questions.
DEMO: dict[str, list[dict[str, str]]] = {
    "quillfern": [
        {"prompt": "who owns billing, and who else has worked on it", "expect": "The owner, then everyone linked by past work, pull requests and reviews."},
        {"prompt": "why did we move the memory layer off postgres", "expect": "The decision, who proposed and approved it, the RFC and the ticket it is based on, and the decision it superseded."},
        {"prompt": "In Slack: decision: move billing retries to the invoice worker, then Approve", "expect": "The card names what the write will replace. The new decision supersedes the old one; asking why again shows both, with dates."},
        {"prompt": "what breaks if auth goes down, then and two hops out", "expect": "Dependents grouped by hop, the main connectors (Billing and Notification tie, 1 of the 5 routes each), and the route to the invoice worker two hops out."},
        {"prompt": "In Slack: ticket: billing exports time out, Approve, then who should look at it", "expect": "The ticket is in the graph and the next answer ranks the people connected to that service."},
        {"prompt": "who actually works together", "expect": "Label propagation. Arjun, Lena and Tomas share Billing even though Tomas is on Platform; Kofi and Maya share the memory layer. Sara knows Billing and stays outside."},
        {"prompt": "is the billing retries decision still the last word", "expect": "It cites the runbook and TKT-102, and names pull requests that touched Billing and were not cited."},
        {"prompt": "who knows the payments gateway", "expect": "Not in the graph. The answer says so and names the nearest matches. It never guesses."},
        {"prompt": "The same question from two DMs", "expect": "Only the teammate whose linked GitHub account can read a private repo sees that repo's pull requests."},
    ],
    "company-brain": [
        {"prompt": "who owns the memory layer, and who else has worked on it", "expect": "The owner, then pull requests that touched the memory layer, with the path."},
        {"prompt": "why do we store memory in FalkorDB instead of a hosted memory API", "expect": "The decision, who proposed it, and the documents and chat it is based on."},
        {"prompt": "why did we cut vector recall", "expect": "The standing decision, the vector-index decision it superseded, and both dates."},
        {"prompt": "what breaks if the claude api goes down", "expect": "Dependents grouped by hop, and the main connector: LLM gateway, 4 of the 5 reach Claude API through it."},
        {"prompt": "is the vector recall decision still the last word", "expect": "The standing recall decision, what it cites, and any ticket or pull request on that service it does not cite."},
        {"prompt": "who owns the linear integration", "expect": "Not in the graph. The answer says so and names the nearest matches. It never guesses."},
    ],
}
_QUESTION_KEYS = ("name", "topic", "query", "text", "source")

# The Slack commands that write the graph. Typed here, they get the read that answers the
# same need, and a notice that the write itself happens in Slack behind an Approve card.
_WRITE_COMMAND = re.compile(r"^(ticket|decision|remember)\s*:\s*(.*)$", re.IGNORECASE)
_WRITE_READS = {
    "ticket": (
        "who_knows",
        "Filing a ticket writes the graph, so it happens in Slack, after an Approve card names the service it will link to. "
        "Here is who would look at it:",
    ),
    "decision": (
        "decision_trace",
        "Recording a decision writes the graph, so it happens in Slack, after an Approve card names the decision it replaces. "
        "Here is what stands now:",
    ),
    "remember": ("recall", "Saving a memory writes the graph, so it happens in Slack. Here is what the memories already say:"),
}


class ToolRefused(ValueError):
    """The question named a tool this surface will not run: one that writes, or one that does not exist."""


# Limits for the public surface. One uvicorn worker serves the app, so in-process state is
# shared by every request.
MAX_QUESTION_CHARS = 300
CACHE_TTL_SECONDS = 60.0
CACHE_MAX_ENTRIES = 256
RATE_LIMIT = 30
RATE_WINDOW_SECONDS = 60.0


class QuestionTooLong(ValueError):
    """The question is longer than this surface answers."""


class TtlCache:
    """Values kept for ``ttl`` seconds, at most ``max_entries`` of them, the oldest dropped first.

    A failed computation is not cached. Cached values are shared, so callers treat them as read-only.
    """

    def __init__(self, ttl: float, max_entries: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self.clock = clock
        self._items: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get_or_compute(self, key, compute: Callable[[], object]):
        with self._lock:
            hit = self._items.get(key)
            if hit is not None and hit[0] > self.clock():
                return hit[1]
        value = compute()
        with self._lock:
            self._items[key] = (self.clock() + self.ttl, value)
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)
        return value

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    # Whole seconds until the client's window resets; 0 when the call is allowed.
    retry_after: int = 0


class RateLimiter:
    """At most ``limit`` calls per client in each fixed window of ``window`` seconds."""

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic, max_clients: int = 4096) -> None:
        self.limit = limit
        self.window = window
        self.clock = clock
        self.max_clients = max_clients
        self._windows: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def check(self, client: str) -> RateDecision:
        now = self.clock()
        with self._lock:
            start, count = self._windows.get(client, (now, 0))
            if now - start >= self.window:
                start, count = now, 0
            if count >= self.limit:
                return RateDecision(False, max(1, math.ceil(start + self.window - now)))
            self._windows[client] = (start, count + 1)
            if len(self._windows) > self.max_clients:
                self._windows = {key: value for key, value in self._windows.items() if now - value[0] < self.window}
            return RateDecision(True)

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()


ANSWERS = TtlCache(CACHE_TTL_SECONDS, CACHE_MAX_ENTRIES)
LIMITER = RateLimiter(RATE_LIMIT, RATE_WINDOW_SECONDS)


def admit(client: str | None) -> RateDecision:
    """Whether this client may make another public call now. Clients without an address share one budget."""
    return LIMITER.check(client or "unknown")


def public_proof() -> ProofReport:
    """The proof report for the graph in use, computed at most once per cache period."""
    return ANSWERS.get_or_compute(("proof", falkordb_client.graph_name()), run_proof)


@dataclass(frozen=True)
class GraphNode:
    id: str
    name: str
    label: str
    # A Service's kind (service, system, process, ...), as the reply words it; None for every other label.
    kind: str | None = None

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "label": self.label, "kind": self.kind}


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
    # Set when the question was a Slack write command: where the write happens instead.
    notice: str = ""
    # The table a ranking tool returns (risk_map); None for every other tool.
    grid: dict | None = None

    def to_dict(self) -> dict:
        return {
            "notice": self.notice,
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
            "grid": self.grid,
        }


def read_tools() -> list[ToolSummary]:
    """The catalogue this surface can run: every tool that does not write the graph."""
    return [
        ToolSummary(spec.name, spec.description, list(spec.args_schema.get("properties", {})))
        for spec in catalogue.list_tools()
        if not spec.needs_approval
    ]


def write_tools() -> list[str]:
    """The tools that write the graph. They run only in Slack, after an Approve card; the page names them."""
    return [spec.name for spec in catalogue.list_tools() if spec.needs_approval]


@dataclass(frozen=True)
class Examples:
    company_id: str | None
    company_name: str | None
    questions: list[str]


def demo_steps(company_id: str | None) -> list[dict[str, str]]:
    """The demo script for the company in the graph. An unknown company gets the Quillfern script."""
    return [dict(step) for step in DEMO.get(company_id or "", DEMO["quillfern"])]


def examples() -> Examples:
    """The seeded company and the questions to offer for it. An unknown company gets the Quillfern set.

    Chips are offered only when they answer: every question but the last is asked here and
    dropped unless it is ``found`` and walks a path, so a graph missing a chip's data never
    offers it. The path matters for the memory tools, which find the thing named and still
    answer "no memory mentions it" on a graph with no memories. The last is the deliberate
    refusal and is always kept, unasked, as the last. Asking goes
    through ``answer_question``'s cache, so each chip runs its tool at most once per cache
    period and the click that follows is already answered.
    """
    _, graph = connect()
    rows = graph.ro_query("MATCH (c:Company) RETURN c.id, c.name ORDER BY c.id LIMIT 1").result_set
    company_id, company_name = (rows[0][0], rows[0][1]) if rows else (None, None)
    *chips, refusal = EXAMPLES.get(company_id or "", EXAMPLES["quillfern"])
    return Examples(company_id, company_name, [question for question in chips if _answers(question)] + [refusal])


def _answers(question: str) -> bool:
    """Whether an example chip answers with a path on the graph in use. A chip that raises is dropped and logged."""
    try:
        answer = answer_question(question)
        return answer.found and bool(answer.path)
    except Exception as error:
        log.warning("[ask] example chip dropped (%s): %s", error, question)
        return False


def answer_question(question: str, tool: str | None = None, use_model: bool = False, scopes=DEFAULT_SCOPES) -> AskAnswer:
    """Pick a read tool, run it, and shape the answer with the path it walked.

    ``tool`` names the tool outright and skips the chooser; its arguments are filled from
    the question. ``use_model`` lets the model pick (it falls back to keywords when no
    model is configured, exactly as the Slack turn does). A write tool is never run.
    """
    question = " ".join(question.split())
    read_scopes = list(scopes)
    if len(question) > MAX_QUESTION_CHARS:
        raise QuestionTooLong(f"questions are limited to {MAX_QUESTION_CHARS} characters; this one has {len(question)}")
    if not question:
        return AskAnswer(question, "none", "keywords", "Ask a question about the company graph.", found=False, scopes=read_scopes)
    if use_model:
        # Admin only, and a model's choice can differ between calls: never cached.
        return _answer(question, tool, True, read_scopes)
    key = (falkordb_client.graph_name(), question, tool, tuple(read_scopes))
    return ANSWERS.get_or_compute(key, lambda: _answer(question, tool, False, read_scopes))


def _answer(question: str, tool: str | None, use_model: bool, read_scopes: list[str]) -> AskAnswer:
    command = _WRITE_COMMAND.match(question) if tool is None else None
    if command and command.group(2).strip():
        read_tool, notice = _WRITE_READS[command.group(1).lower()]
        answer = _answer(command.group(2).strip(), read_tool, False, read_scopes)
        answer.question, answer.notice = question, notice
        return answer
    if tool is not None:
        spec = _read_spec(tool)
        choice = agent.ToolChoice(spec.name, _args_for(spec, question), "caller")
    else:
        choice = agent.choose_tool(question, scopes=read_scopes) if use_model else agent.choose_with_keywords(question)
        spec = _read_spec(choice.tool)
    result, choice = agent.run_choice(choice, question, read_scopes)
    return shape(question, result, choice.via, read_scopes)


def shape(question: str, result: ToolResult, picked_by: str, scopes: list[str]) -> AskAnswer:
    nodes, edges = graph_of(result.path, service_kinds(result.path, scopes))
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
        grid=result.grid,
    )


def service_kinds(path: list[PathStep], scopes) -> dict[str, str]:
    """The kind of each Service the path names by id, read the way the reply wording reads it.

    No query runs when the path names no Service by id.
    """
    ids = sorted(
        {node_id for step in path for node_id, label in ((step.from_id, step.from_label), (step.to_id, step.to_label)) if node_id and label == "Service"}
    )
    if not ids:
        return {}
    _, graph = connect()
    rows = graph.ro_query(
        "MATCH (s:Service) WHERE s.id IN $ids AND coalesce(s.scope, 'org_shared') IN $scopes RETURN s.id, s.kind",
        {"ids": ids, "scopes": list(scopes)},
    ).result_set
    return {service_id: kinds.kind_or_default(kind) for service_id, kind in rows}


def graph_of(path: list[PathStep], kinds_by_id: dict[str, str] | None = None) -> tuple[list[GraphNode], list[GraphEdge]]:
    """The nodes and edges of a path, each once, in order of first appearance.

    A step carries ids and labels when the tool knew them; a step without an id is keyed
    by its name, so two steps that cite the same name share one node. ``kinds_by_id``
    maps a Service's id to its kind, so the drawing can say process or playbook.
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
    known = kinds_by_id or {}
    drawn = [GraphNode(n.id, n.name, n.label, known.get(n.id)) if n.label == "Service" and n.id in known else n for n in nodes.values()]
    return drawn, edges


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
