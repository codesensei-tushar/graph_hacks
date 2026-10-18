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
3. words in what people wrote or said, when the question asks about a topic: ``search``.

A step returns a found ``ToolResult`` or None. A step that raises is logged and skipped.
"""

import logging
from collections.abc import Callable

from src.domain.knowledge.dto import ToolResult

log = logging.getLogger(__name__)

Step = Callable[[str, list[str]], ToolResult | None]


def _person(question: str, scopes: list[str]) -> ToolResult | None:
    # Imported here: person_profile reads decisions, which registers a catalogue tool.
    from src.domain.knowledge import person_profile

    return person_profile.profile_or_none(question, scopes)


def _search(question: str, scopes: list[str]) -> ToolResult | None:
    # Imported here: search reads decisions, which registers a catalogue tool.
    from src.domain.knowledge import search

    return search.if_topic(question, scopes)


# Person first, then explore when it lands (see the module docstring); search last.
STEPS: tuple[Step, ...] = (_person, _search)


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
