"""The read-only tool catalogue as MCP tools. Pure mapping: no transport, no auth.

One MCP tool per ``ToolSpec`` that does not need approval: same name, same description,
the ``args_schema`` as the input schema verbatim. Calling one runs ``spec.run`` with the
``org_shared`` read scope (an MCP caller has no Slack identity to widen it from) and
returns the same answer, path, ``found`` and ``candidates`` a Slack asker gets, plus
``next``: calls worth making about the services the answer pointed at, so a client can
chain search, then what changed or what to read, without guessing a name. Tools that
write the graph are never listed: the server is read-only. A string argument longer than
``ask_api.MAX_QUESTION_CHARS`` is refused, as the ask page refuses a long question. A tool whose graph
query runs past FalkorDB's timeout is a ``ToolError`` saying so (``tool_guard.TIMEOUT_REPLY``).
"""

from collections.abc import Sequence
from dataclasses import dataclass

from src.domain.knowledge import catalogue, tool_guard
from src.domain.knowledge.ask_api import MAX_QUESTION_CHARS
from src.domain.knowledge.dto import ToolSpec

ORG_SHARED = "org_shared"
DEFAULT_SCOPES: tuple[str, ...] = (ORG_SHARED,)


# What a found answer points at, and which listed tools to ask about it next.
_POINTS_AT = frozenset({"ABOUT", "DESCRIBES", "TOUCHES", "MENTIONS", "OWNS", "RUNS", "DEPENDS_ON"})
_NEXT = {
    "search": ("explore", "what_changed", "reading_list", "who_knows"),
    "explore": ("what_changed", "reading_list", "who_knows"),
    "find": ("explore", "what_changed"),
    "neighbors": ("explore", "what_changed"),
    "who_knows": ("what_changed", "reading_list"),
    "who_owns": ("what_changed", "reading_list"),
    "reading_list": ("what_changed", "who_knows"),
    "what_changed": ("who_knows", "reading_list"),
    "blast_radius": ("what_changed", "who_knows"),
    "risk_map": ("reading_list", "who_knows"),
}
_WHY = {
    "explore": "everything linked to it",
    "what_changed": "what changed around it lately",
    "reading_list": "what to read before changing it",
    "who_knows": "who to ask about it",
}
MAX_NEXT_THINGS = 2


def next_calls(tool: str, result) -> list[dict]:
    """Calls worth making next: for the services the answer's path points at most, the tools that follow this one."""
    listed = set(tool_names())
    follow = [name for name in _NEXT.get(tool, ()) if name in listed]
    if not follow or not result.found:
        return []
    counts: dict[str, int] = {}
    for step in result.path:
        if step.rel in _POINTS_AT and step.to_label == "Service":
            counts[step.to_name] = counts.get(step.to_name, 0) + 1
    things = sorted(counts, key=lambda name: -counts[name])[:MAX_NEXT_THINGS]
    return [{"tool": name, "arguments": {"name": thing}, "why": f"{_WHY[name]} ({thing})"} for thing in things for name in follow]


# What the server tells a client before any call. The chain names only tools that are listed.
_BASE = (
    "Read-only tools over one company's knowledge graph. Every answer cites the edges it walked. "
    "Callers read the company-wide scope; nothing here writes the graph."
)


def instructions() -> str:
    """How the tools chain: find a name when the words name nothing, then ask about that name."""
    names = set(tool_names())
    parts = [_BASE, "Names resolve exactly or are refused with the nearest matches, so work from names the graph gave you."]
    if "search" in names:
        parts.append("When the question's words name no service, person or team, start with search: each hit says which service it is about.")
    if "explore" in names:
        parts.append("Pass a name you found to explore for its neighbourhood.")
    asks = [
        (tool, what)
        for tool, what in (
            ("who_knows", "who_knows for who to ask"),
            ("reading_list", "reading_list for what to read before changing it"),
            ("what_changed", "what_changed for what changed around it and upstream lately"),
        )
        if tool in names
    ]
    if asks:
        listed = [what for _tool, what in asks]
        parts.append("Then ask about that name: " + (", ".join(listed[:-1]) + " and " + listed[-1] if len(listed) > 1 else listed[0]) + ".")
    if "what_changed" in names:
        parts.append("what_changed with no name covers the whole company; give it days for the window and only for one kind.")
    return " ".join(parts)


class ToolError(ValueError):
    """The caller named a tool that is not offered, or arguments its schema rejects."""


@dataclass(frozen=True)
class McpTool:
    name: str
    description: str
    input_schema: dict


def read_only_specs() -> list[ToolSpec]:
    return [spec for spec in catalogue.list_tools() if not spec.needs_approval]


def read_only_tools() -> list[McpTool]:
    return [McpTool(spec.name, spec.description, spec.args_schema) for spec in read_only_specs()]


def tool_names() -> list[str]:
    return [spec.name for spec in read_only_specs()]


def _check_arguments(spec: ToolSpec, arguments: dict) -> None:
    too_long = sorted(key for key, value in arguments.items() if isinstance(value, str) and len(value) > MAX_QUESTION_CHARS)
    if too_long:
        raise ToolError(f"{spec.name}: {', '.join(too_long)} is limited to {MAX_QUESTION_CHARS} characters")
    schema = spec.args_schema or {}
    missing = [key for key in schema.get("required", []) if key not in arguments]
    if missing:
        raise ToolError(f"{spec.name} needs {', '.join(missing)}")
    if schema.get("additionalProperties") is False:
        unknown = [key for key in arguments if key not in schema.get("properties", {})]
        if unknown:
            raise ToolError(f"{spec.name} does not take {', '.join(unknown)}")


def call_tool(name: str, arguments: dict | None, scopes: Sequence[str] = DEFAULT_SCOPES) -> dict:
    """Run one read-only tool and return what an MCP caller gets as structured content."""
    spec = catalogue.get_tool(name)
    if spec is None or spec.needs_approval:
        raise ToolError(f"no tool named {name!r}; the tools are {', '.join(tool_names())}")
    arguments = dict(arguments or {})
    _check_arguments(spec, arguments)
    try:
        with tool_guard.guard("mcp", f"{spec.name} {arguments}"):
            result = spec.run(arguments, list(scopes))
    except tool_guard.ToolTimedOut as error:
        raise ToolError(str(error)) from error
    return {**result.to_dict(), "found": result.found, "candidates": list(result.candidates), "next": next_calls(spec.name, result)}
