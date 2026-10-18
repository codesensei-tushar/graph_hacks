"""A question in two parts gets both answers: "what if auth breaks and who to contact".

``ask_service.split_compound`` finds the parts. The agent answers the first as it was chosen,
then the rest. A rest that names nothing ("who to contact", "who owns it", "what depends on
it") is asked about what the first part was about, the way a thread follow-up is, so it
becomes "who to contact about Auth Service". The two answers come back as one: both texts in
order, the edges of both, both tool names. The rest is offered as the next question only when
it cannot be answered.
"""

import re

from src.domain.knowledge import resolve
from src.domain.knowledge.dto import ToolResult
from src.domain.knowledge.resolve import Entity

# "who to contact", "who do I ping", "who can we ask": the people who know it, never what breaks.
CONTACT = re.compile(
    r"^\s*who (?:to|do (?:i|we)|should (?:i|we)|can (?:i|we)) (?:contact|ping|ask|talk to|reach out to|call|escalate to)\b", re.IGNORECASE
)
# A thing the first part named: "who owns it", "what breaks if that goes down".
_THING = re.compile(r"\b(?:it|its|that|this)\b", re.IGNORECASE)
_ABOUT = re.compile(r"\b(?:about|on|for|with)\b", re.IGNORECASE)
SEPARATOR = "\n\n"


def names_nothing(rest: str, scopes: list[str], labels: tuple[str, ...]) -> bool:
    """Whether the rest needs the first part's subject: it points back at it, or names no node, not even a near one."""
    if _THING.search(rest):
        return True
    resolution = resolve.resolve_name(rest, labels, scopes)
    return not resolution.found and not resolution.candidates


def about(rest: str, subject: Entity) -> str:
    """The rest as it was answered: "who owns it" about Auth Service is "who owns Auth Service",
    "who to contact" is "who to contact about Auth Service", "who to ping about the outage" names it after."""
    rest = rest.strip().rstrip("?.! ")
    said, count = _THING.subn(subject.name, rest, count=1)
    if count:
        return said
    return f"{rest} ({subject.name})" if _ABOUT.search(rest) else f"{rest} about {subject.name}"


def combine(first: tuple[str, ToolResult], rest: tuple[str, ToolResult]) -> ToolResult:
    """One answer for both parts: the texts in order, a blank line between, the union of their edges."""
    parts = [first, rest]
    results = [result for _question, result in parts]
    combined = ToolResult(
        tool=" + ".join(dict.fromkeys(result.tool for result in results)),
        answer=SEPARATOR.join(result.answer for result in results),
        found=any(result.found for result in results),
        depth=next((result.depth for result in results if result.depth is not None), None),
        grid=next((result.grid for result in results if result.grid is not None), None),
    )
    for result in results:
        combined.extend(result.path)
        combined.candidates += [name for name in result.candidates if name not in combined.candidates]
        combined.memories += [memory for memory in result.memories if memory not in combined.memories]
    combined.parts = [{"question": question, "tool": result.tool, "answer": result.answer, "found": result.found} for question, result in parts]
    return combined


def offer(rest: str, result: ToolResult) -> ToolResult:
    """A rest that cannot be answered is offered as the next question, after an answer to the first part."""
    if result.found:
        result.answer = f"{result.answer} You also asked “{rest.rstrip('?')}”: ask it on its own for that answer."
    return result
