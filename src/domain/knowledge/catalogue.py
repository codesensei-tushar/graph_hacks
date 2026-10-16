"""The fixed tool catalogue the agent chooses from.

The agent never writes a query. Each tool here is a parameterised traversal written by
hand; the model only picks a tool and fills in its arguments. ``register`` lets other
modules add tools (a decision writer, for example) without editing this file.
"""

from src.domain.knowledge import ask_service
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
    return ask_service.decision_trace(args["topic"], scopes)


def _run_blast_radius(args: dict, scopes: list[str]) -> ToolResult:
    return ask_service.blast_radius(args["name"], scopes)


def _run_ingest_ticket(args: dict, scopes: list[str]) -> ToolResult:
    created = ask_service.ingest_ticket(args["title"], args.get("about") or "", args["scope"])
    follow = ask_service.who_knows(created.about_name, scopes)
    return ToolResult(
        tool="ingest_ticket",
        answer=f"Filed *{created.id}* about {created.about_name}. {follow.answer}",
        path=created.path + follow.path,
    )


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
            "Everyone connected to a service, document, or ticket: its owner, and the people who worked on it, "
            "wrote or reviewed pull requests touching it, filed tickets about it, or are marked as knowing it."
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
        name="blast_radius",
        description="What stops working if a service goes down: the services that depend on it, following DEPENDS_ON edges.",
        args_schema=_schema({"name": _NAME}, ["name"]),
        run=_run_blast_radius,
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
    )
)
