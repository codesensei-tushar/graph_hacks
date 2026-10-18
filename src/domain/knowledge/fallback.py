"""Before a tool says "nothing matches": the other ways the graph can still answer.

A specific tool refuses when the question does not name the kind of thing it takes:
``who_knows`` takes a service, document or ticket; ``blast_radius`` takes a service;
``onboarding_brief`` takes a team. The graph may still know what was asked. Each refusal
point hands its refusal to ``before_refusing``, which tries ``STEPS`` in order and returns
the first answer found, or the refusal unchanged. A name that resolves to nothing, like
"the payments gateway", is still refused word for word.

The order is the contract, so the tools that plug in here never compete:

1. a person the question names: ``person_profile``;
2. any other node a name resolves to: ``explore``, its neighbourhood;
3. words in what people wrote or said, when the question asks about a topic: ``search``;
4. what was said in chat that matches the question: ``recall`` ("when does the refund batch run"
   after "remember: the refund batch runs at 02:00 UTC").

A step returns a found ``ToolResult`` or None. A step that raises is logged and skipped.
"""

import logging
import re
from dataclasses import replace
from collections.abc import Callable

from src.domain.knowledge.dto import ToolResult

log = logging.getLogger(__name__)

Step = Callable[[str, list[str]], ToolResult | None]


def _person(question: str, scopes: list[str]) -> ToolResult | None:
    # Imported here: person_profile reads decisions, which registers a catalogue tool.
    from src.domain.knowledge import person_profile

    return person_profile.profile_or_none(question, scopes)


def _explore(question: str, scopes: list[str]) -> ToolResult | None:
    # Imported here: explore marks replaced decisions, and decisions registers a catalogue tool.
    from src.domain.knowledge import explore

    return explore.before_refusing(question, scopes)


def _search(question: str, scopes: list[str]) -> ToolResult | None:
    # Imported here: search reads decisions, which registers a catalogue tool.
    from src.domain.knowledge import search

    return search.if_topic(question, scopes)


def _recall(question: str, scopes: list[str]) -> ToolResult | None:
    """What was said in chat that matches the question's words: "when does the refund batch run" after
    "remember: the refund batch runs at 02:00 UTC". Only a memory the asker can read and that matches."""
    from src.domain.knowledge import memories

    from src.domain.knowledge import resolve

    # Not ``memories.recall`` itself: the tool that fell back owns these queries on /ops/metrics.
    found = memories.recalled(question, scopes, memories.FALLBACK_K)
    # Every word the question names must be in the memory: a loose match would answer a question
    # about something else ("who owns Larkspur" with a note about the memory layer).
    words = [word for word in resolve.core(question).split() if len(word) >= 3 and word not in _ASKING]
    if not words:
        return None
    found.memories = [memory for memory in found.memories if all(_said(word, memory.text) for word in words)]
    if not found.memories:
        return None
    found.memories = [replace(memory, author=_speaker(memory.author)) for memory in found.memories]
    first = found.memories[0]
    when = f", {first.learned_at}" if first.learned_at else ""
    found.answer = f"From what was said in chat: “{first.text}” ({first.author}{when})." + (
        f" {len(found.memories) - 1} more below." if len(found.memories) > 1 else ""
    )
    return found


# Question words the resolver keeps that say what is asked, not about what.
_ASKING = frozenset({"when", "does", "time", "run", "runs", "happen", "happens", "often", "much", "many", "long"})


_SLACK_ID = re.compile(r"^[UW][A-Z0-9]{2,}$")


def _speaker(author: str) -> str:
    """Who said it, by name: a Slack user id ("U0123") is shown as their person, or their Slack name."""
    if not _SLACK_ID.match(author or ""):
        return author or "someone"
    from src.db.falkordb_client import connect

    _, graph = connect()
    rows = graph.ro_query(
        "MATCH (u:SlackUser {id: $id}) OPTIONAL MATCH (u)-[:IS]->(p:Person) RETURN coalesce(p.name, u.real_name, u.name)", {"id": author}
    ).result_set
    return rows[0][0] if rows and rows[0][0] else "someone"


def _said(word: str, text: str) -> bool:
    """The word, or its plural or singular, is a word of the text: "batches" for "batch". Never a misspelling."""
    from src.domain.knowledge import resolve

    return any(resolve.same_word(word, token) for token in resolve.tokens(text))


# Person first, then explore (see the module docstring); search, then what was said in chat, last.
STEPS: tuple[Step, ...] = (_person, _explore, _search, _recall)


def before_refusing(question: str, scopes: list[str], miss: ToolResult) -> ToolResult:
    """The first step that answers, or ``miss`` as it was."""
    for step in STEPS:
        try:
            answer = step(question, scopes)
        except Exception:
            log.exception("[fallback] %s failed for %r; trying the next step", getattr(step, "__name__", step), question)
            continue
        if answer is not None and answer.found:
            return answer
    return miss
