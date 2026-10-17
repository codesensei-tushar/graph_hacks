"""The fixed tool catalogue the agent chooses from.

The agent never writes a query. Each tool here is a parameterised traversal written by
hand; the model only picks a tool and fills in its arguments. ``register`` lets other
modules add tools (a decision writer, for example) without editing this file.
"""

from src.domain.knowledge import algorithms, ask_service, bus_factor, memories
from src.domain.knowledge.dto import ToolResult, ToolSpec

_TOOLS: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    """Add a tool, replacing any earlier tool with the same name."""
    _TOOLS[spec.name] = spec
    return spec


def list_tools() -> list[ToolSpec]:
    return list(_TOOLS.values())


def get_tool(name: str) -> ToolSpec | None:
    return _TOOLS.get(name)


def tool_names() -> list[str]:
    return list(_TOOLS)


def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


_NAME = {"type": "string", "description": "The service, document, ticket, or team the question is about, copied from the question."}


def _run_who_owns(args: dict, scopes: list[str]) -> ToolResult:
    return ask_service.who_owns(args["name"], scopes)


def _run_who_knows(args: dict, scopes: list[str]) -> ToolResult:
    return ask_service.who_knows(args["name"], scopes)


def _run_decision_trace(args: dict, scopes: list[str]) -> ToolResult:
    return decisions.decision_trace(args["topic"], scopes)


def _run_decision_pressure(args: dict, scopes: list[str]) -> ToolResult:
    return decisions.decision_pressure(args["topic"], scopes)


def _run_working_groups(_args: dict, scopes: list[str]) -> ToolResult:
    return algorithms.working_groups(scopes)


def _run_blast_radius(args: dict, scopes: list[str]) -> ToolResult:
    return algorithms.blast_radius(args["name"], scopes, args.get("depth"))


def _run_if_person_leaves(args: dict, scopes: list[str]) -> ToolResult:
    return bus_factor.if_person_leaves(args["name"], scopes)


def _run_path_between(args: dict, scopes: list[str]) -> ToolResult:
    return algorithms.path_between(args["source"], args.get("target") or "", scopes)


def _run_load_bearing(args: dict, _scopes: list[str]) -> ToolResult:
    return algorithms.load_bearing(args.get("limit", algorithms.DEFAULT_LOAD_BEARING))


def _run_related_memories(args: dict, scopes: list[str]) -> ToolResult:
    return memories.related_memories(args["name"], scopes, args.get("k", memories.DEFAULT_K))


def _run_recall(args: dict, scopes: list[str]) -> ToolResult:
    return memories.recall(args["query"], scopes, args.get("k", memories.DEFAULT_K))


def _run_ingest_ticket(args: dict, scopes: list[str]) -> ToolResult:
    title = args["title"]
    created = ask_service.ingest_ticket(title, args.get("about") or "", args["scope"])
    if created.about_name is None:
        return ToolResult(
            tool="ingest_ticket",
            answer=(
                f"Filed *{created.id}*, not linked to a service: nothing in the graph matched “{title}”. "
                f"Say `ticket: {title} about <service>` to link it."
            ),
        )
    follow = ask_service.who_knows(created.about_name, scopes)
    result = ToolResult(tool="ingest_ticket", answer=f"Filed *{created.id}* about {created.about_name}. {follow.answer}")
    result.extend(created.path)
    result.extend(follow.path)
    return result


def _preview_ingest_ticket(args: dict, _scopes: list[str]) -> str:
    title = args["title"]
    target = ask_service.ticket_target(title, args.get("about") or "")
    if target is None:
        return f"File a ticket titled *{title}* in the company graph, not linked to any service: nothing in the graph matches it"
    return f"File a ticket titled *{title}* in the company graph, linked to *{target.name}*"


register(
    ToolSpec(
        name="who_owns",
        description="Who owns a service (or the service a document or ticket is about), and which team runs it.",
        args_schema=_schema({"name": _NAME}, ["name"]),
        run=_run_who_owns,
    )
)
register(
    ToolSpec(
        name="who_knows",
        description=(
            "Who to ask about a service, document, or ticket: its owner, and the people who worked on it, "
            "wrote or reviewed pull requests touching it, filed tickets about it, or are marked as knowing it, "
            "ranked by weighted edges (owns 5, wrote a pull request 3, worked on, reviewed, or assigned 2, knows or opened 1)."
        ),
        args_schema=_schema({"name": _NAME}, ["name"]),
        run=_run_who_knows,
    )
)
register(
    ToolSpec(
        name="decision_trace",
        description="Why a decision was made: who proposed and approved it, and the documents, tickets, and conversations it came from.",
        args_schema=_schema(
            {"topic": {"type": "string", "description": "The decision or the thing it was about, copied from the question."}},
            ["topic"],
        ),
        run=_run_decision_trace,
    )
)
register(
    ToolSpec(
        name="decision_pressure",
        description=(
            "Whether the standing decision is still the last word: tickets and pull requests about the same "
            "service that the decision does not cite. Use when the question asks if a decision still holds, "
            "not when it asks why the decision was made."
        ),
        args_schema=_schema(
            {"topic": {"type": "string", "description": "The decision the question is checking, copied from the question."}},
            ["topic"],
        ),
        run=_run_decision_pressure,
    )
)
register(
    ToolSpec(
        name="working_groups",
        description=(
            "Who actually clusters together from shared work. Label propagation over people who share a service, "
            "and where that differs from the teams on the org chart."
        ),
        args_schema=_schema({}, []),
        run=_run_working_groups,
    )
)
register(
    ToolSpec(
        name="blast_radius",
        description=(
            "What stops working if a service goes down: the services that depend on it, following DEPENDS_ON edges "
            "for a number of hops, with the main connector: the one most of the others reach it through."
        ),
        args_schema=_schema(
            {
                "name": _NAME,
                "depth": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": algorithms.MAX_DEPTH,
                    "description": "How many dependency hops to follow when the question says (one hop, two hops out). Omit to follow all.",
                },
            },
            ["name"],
        ),
        run=_run_blast_radius,
    )
)
register(
    ToolSpec(
        name="if_person_leaves",
        description=(
            "What rests on one person alone if they leave: the things they are the only owner of or the only one "
            "who knows (services, processes, accounts, anything a team owns), the standing decisions only they "
            "proposed or approved, and the open tickets only they worked on, ranked by how much depends on each. "
            "Use when the question names a person leaving, quitting, or away, or asks what someone is the only one "
            "who knows. For a service going down, use blast_radius."
        ),
        args_schema=_schema(
            {"name": {"type": "string", "description": "The person the question is about, copied from the question."}},
            ["name"],
        ),
        run=_run_if_person_leaves,
    )
)
register(
    ToolSpec(
        name="path_between",
        description="Why two things are connected: the shortest route between two services, people, teams, documents, tickets, or decisions.",
        args_schema=_schema(
            {
                "source": {"type": "string", "description": "The first thing named, copied from the question."},
                "target": {"type": "string", "description": "The second thing named, copied from the question."},
            },
            ["source", "target"],
        ),
        run=_run_path_between,
    )
)
register(
    ToolSpec(
        name="load_bearing",
        description="Which services the company leans on most: PageRank over DEPENDS_ON, with how many services depend on each.",
        args_schema=_schema(
            {"limit": {"type": "integer", "minimum": 1, "maximum": algorithms.MAX_LOAD_BEARING, "description": "How many services to list."}},
            [],
        ),
        run=_run_load_bearing,
    )
)
register(
    ToolSpec(
        name="related_memories",
        description=(
            "What has been said in chat about a service, document, ticket, or decision: memories that mention it, "
            "then memories about things one hop away, each with the day it was learned."
        ),
        args_schema=_schema(
            {
                "name": _NAME,
                "k": {"type": "integer", "minimum": 1, "maximum": memories.MAX_K, "description": "How many memories to return."},
            },
            ["name"],
        ),
        run=_run_related_memories,
    )
)
register(
    ToolSpec(
        name="recall",
        description=(
            "Search memory text for a free-form question. Only when the question names no service, document, ticket, "
            "person, or decision; otherwise use a graph tool."
        ),
        args_schema=_schema(
            {
                "query": {"type": "string", "description": "The words to search for, copied from the question."},
                "k": {"type": "integer", "minimum": 1, "maximum": memories.MAX_K, "description": "How many memories to return."},
            },
            ["query"],
        ),
        run=_run_recall,
    )
)
register(
    ToolSpec(
        name="ingest_ticket",
        description="File a ticket in the company graph and link it to the service it is about. Writes the graph, so it needs approval.",
        args_schema=_schema(
            {
                "title": {"type": "string", "description": "The ticket title."},
                "about": {"type": "string", "description": "The service the ticket is about, if the title does not say."},
            },
            ["title"],
        ),
        run=_run_ingest_ticket,
        needs_approval=True,
        preview=_preview_ingest_ticket,
    )
)

# Registers record_decision and owns decision_trace; imported last so the catalogue above exists.
from src.domain.knowledge import decisions  # noqa: E402, F401
