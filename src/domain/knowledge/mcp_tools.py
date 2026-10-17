"""The read-only tool catalogue as MCP tools. Pure mapping: no transport, no auth.

One MCP tool per ``ToolSpec`` that does not need approval: same name, same description,
the ``args_schema`` as the input schema verbatim. Calling one runs ``spec.run`` with the
``org_shared`` read scope (an MCP caller has no Slack identity to widen it from) and
returns the same answer, path, ``found`` and ``candidates`` a Slack asker gets. Tools
that write the graph are never listed: the server is read-only.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from src.domain.knowledge import catalogue
from src.domain.knowledge.dto import ToolSpec

ORG_SHARED = "org_shared"
DEFAULT_SCOPES: tuple[str, ...] = (ORG_SHARED,)


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
    result = spec.run(arguments, list(scopes))
    return {**result.to_dict(), "found": result.found, "candidates": list(result.candidates)}
