"""What the knowledge tools exchange.

A tool is a hand-written, parameterised graph query. It takes the arguments the agent
(or the keyword router) filled in and the asker's readable scopes, and returns a
``ToolResult``: a short answer plus the exact edges it walked, so the reply can cite
the traversal instead of a sentence written after the fact.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone


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


@dataclass(frozen=True)
class MemoryLine:
    """A memory a tool answered from, with when it was learned. Memories are dated nodes."""

    text: str
    learned_at: str
    author: str = "someone"
    scope: str = "org_shared"
    channel_id: str | None = None

    def to_dict(self) -> dict:
        return {"text": self.text, "learned_at": self.learned_at, "author": self.author, "scope": self.scope, "channel_id": self.channel_id}


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
    # Memories the answer drew on, each with the date it was learned.
    memories: list[MemoryLine] = field(default_factory=list)
    # A table for a page to draw, for tools that rank many things at once (risk_map):
    # {"kind", "columns", "rows", "scoring"}.
    grid: dict | None = None

    def add(self, step: PathStep) -> None:
        """Append an edge unless the path already has it."""
        if step not in self.path:
            self.path.append(step)

    def extend(self, steps) -> None:
        for step in steps:
            self.add(step)

    def to_dict(self) -> dict:
        """The shape the Slack formatter and the approval flow already consume."""
        result = {
            "tool": self.tool,
            "answer": self.answer,
            "path": [step.to_dict() for step in self.path],
            "found": self.found,
            "candidates": list(self.candidates),
        }
        if self.depth is not None:
            result["depth"] = self.depth
        if self.memories:
            result["memories"] = [memory.to_dict() for memory in self.memories]
        if self.grid is not None:
            result["grid"] = self.grid
        return result


@dataclass
class TicketCreated:
    """What ``ingest_ticket`` wrote: the new ticket and the edge linking it to its service."""

    id: str
    # None when nothing in the graph matched: the ticket exists but has no ABOUT edge.
    about_name: str | None
    path: list[PathStep] = field(default_factory=list)


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
    # For tools that need approval: one line saying what ``run`` will write, computed from the
    # same arguments before the asker approves, so the card shows the resolved target.
    preview: Callable[[dict, list[str]], str] | None = None


def day_from(ms, ts=None) -> str:
    """The UTC day of a millisecond epoch, else of a Slack message ts, else "". Memories are dated nodes."""
    seconds = None
    if isinstance(ms, (int, float)) and not isinstance(ms, bool) and ms > 0:
        seconds = ms / 1000
    elif ts:
        try:
            seconds = float(ts)
        except (TypeError, ValueError):
            seconds = None
    if not seconds or seconds <= 0:
        return ""
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d")
