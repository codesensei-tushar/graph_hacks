"""What the knowledge tools exchange.

A tool is a hand-written, parameterised graph query. It takes the arguments the agent
(or the keyword router) filled in and the asker's readable scopes, and returns a
``ToolResult``: a short answer plus the exact edges it walked, so the reply can cite
the traversal instead of a sentence written after the fact.
"""

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PathStep:
    """One edge a tool walked. Names are what the reply shows; ids and labels are kept when known."""

    from_name: str
    rel: str
    to_name: str
    from_id: str | None = None
    to_id: str | None = None
    from_label: str | None = None
    to_label: str | None = None

    def to_dict(self) -> dict:
        return {"from": self.from_name, "rel": self.rel, "to": self.to_name}

    def cite(self) -> str:
        return f"{self.from_name} -[{self.rel}]-> {self.to_name}"


@dataclass
class ToolResult:
    tool: str
    answer: str
    path: list[PathStep] = field(default_factory=list)
    # False when the thing the question named is not in the graph, or not visible to the
    # asker. ``candidates`` then holds the nearest names so the reply can offer them.
    found: bool = True
    candidates: list[str] = field(default_factory=list)
    # Traversal depth the tool used, for tools that have one (blast radius, paths).
    depth: int | None = None

    def add(self, step: PathStep) -> None:
        """Append an edge unless the path already has it."""
        if step not in self.path:
            self.path.append(step)

    def extend(self, steps) -> None:
        for step in steps:
            self.add(step)

    def to_dict(self) -> dict:
        """The shape the Slack formatter and the approval flow already consume."""
        result = {"tool": self.tool, "answer": self.answer, "path": [step.to_dict() for step in self.path]}
        if self.depth is not None:
            result["depth"] = self.depth
        return result


ToolRunner = Callable[[dict, list[str]], ToolResult]


@dataclass(frozen=True)
class ToolSpec:
    """One entry in the catalogue the agent picks from.

    ``args_schema`` is the JSON schema of ``args``; the model fills it in, the agent
    validates it, and ``run(args, scopes)`` executes the query. ``scopes`` is the asker's
    read set and every query filters on it. Tools that write the graph set
    ``needs_approval``; they run only after the asker approves, with the approved write
    scope passed as ``args["scope"]``.
    """

    name: str
    description: str
    args_schema: dict
    run: ToolRunner
    needs_approval: bool = False
