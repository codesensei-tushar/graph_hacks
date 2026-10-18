"""The questions the ask page offers, read out of its scripts the way the page builds them.

graph.js keeps the questions for a node in one ``const QUESTIONS = {...}`` literal and answer.js the
follow-ups for a tool in one ``const NEXT = {...}`` literal, one entry per line. The unit test
(each question routes to its tool) and the integration test (each is found on a fresh seed) both
read them from there, so the page and its tests cannot drift apart.
"""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "static"
_ENTRY = re.compile(r'^\s*(?:"([^"]*)"|(\w+)):\s*\[(.*)\],?\s*$')


def literal(script: str, name: str) -> dict[str, list[str]]:
    """The ``const NAME = {...}`` table in a page script, as {key: [template, ...]}."""
    text = (STATIC / script).read_text()
    body = re.search(rf"const {name} = \{{\n(.*?)\n\s*\}};", text, re.DOTALL).group(1)
    table: dict[str, list[str]] = {}
    for line in body.splitlines():
        match = _ENTRY.match(line)
        if not match:
            raise ValueError(f"{script}: {name} has a line the page tests cannot read: {line!r}")
        key = match.group(1) if match.group(1) is not None else match.group(2)
        table[key] = re.findall(r'"([^"]*)"', match.group(3))
    return table


QUESTIONS = literal("graph.js", "QUESTIONS")
NEXT = literal("answer.js", "NEXT")


def fill(template: str, name: str) -> str:
    """A template with its node's name, as graph.js fills it: {lower} lowercases a leading capital word."""
    lower = name[0].lower() + name[1:] if re.match(r"[A-Z][a-z]", name) else name
    return template.replace("{name}", name, 1).replace("{lower}", lower, 1)


def questions_for(name: str, label: str | None = None, kind: str | None = None) -> list[str]:
    """BrainGraph.questionsFor: a Service's kind can stand in for its label; no label asks "who knows"."""
    label = label or ""
    templates = (label == "Service" and kind and QUESTIONS.get(kind)) or QUESTIONS.get(label) or []
    return [fill(template, name) for template in templates]
