"""Choosing a tool: one model call picks from the catalogue, or the keyword router does.

The model never writes a query. It sees the catalogue (tool names, descriptions, and
argument schemas) and the question, and returns ``{tool, args}``. The choice is checked
against the registry and the tool's own schema before anything runs. If no model is
configured, the call fails, or the choice is invalid, the keyword router in
``ask_service`` picks the tool instead, so every path works with no key.

A tool that writes the graph is accepted from the model only when the question itself
asks for a write. Thread text goes into the prompt, so a planted message must not be
able to steer a turn into filing something; the approval card is the second guard.

A follow-up in a thread ("and two hops out", "who should look at it") names nothing
by itself. When a question resolves to nothing and offers no candidates, the subject
comes from the thread: the most recent node a person named, with the tool and depth
the last reply used. A refusal that does name candidates is never overridden.

This module knows nothing about Slack beyond the shape of a thread message. The turn
that renders a reply lives in ``src/domain/slack/agent_turn.py``.
"""

import json
import logging
import re
from dataclasses import dataclass

from src.domain.knowledge import algorithms, ask_service, catalogue, resolve
from src.domain.knowledge.dto import ToolResult, ToolSpec
from src.domain.knowledge.resolve import Entity
from src.integrations.llm.model import ModelRequest, ModelUnavailable, generate_object

log = logging.getLogger(__name__)

MODEL_PROFILE = "fast"
THREAD_CONTEXT_MESSAGES = 6
THREAD_MESSAGE_CHARS = 300
THREAD_NAMED_LIMIT = 5
THREAD_LABELS = ("Service", "Document", "Ticket", "Decision")

_TOOL_IN_REPLY = re.compile(r"Tool `(\w+)`")
_DEPTH_IN_REPLY = re.compile(r"depth (\d+)")
_DEEPER = re.compile(r"\b(?:deeper|further out|more hops|next hop|one more hop|keep going)\b", re.IGNORECASE)

SYSTEM_PROMPT = (
    "You route questions about a company to one tool from a fixed catalogue. You never write queries "
    "and never answer the question yourself.\n"
    "Pick exactly one tool. Fill its arguments with words copied from the question, or from the thread "
    "when the question refers back to it. Do not invent names. Keep each argument short: the thing "
    "named, not the whole sentence.\n"
    "When unsure which tool fits, pick who_knows."
)

# The question itself has to ask for a write before a writing tool may be chosen.
WRITE_INTENT = re.compile(
    r"^(?:ticket|file ticket|new ticket)\s*[:\-]"
    r"|\b(?:file|create|open|record|log|note|remember|decided|decide|decision|we're moving|we are moving|going with)\b",
    re.IGNORECASE,
)


class InvalidToolChoice(ValueError):
    """The model named a tool the catalogue lacks, or arguments the tool's schema rejects."""


@dataclass(frozen=True)
class ToolChoice:
    tool: str
    args: dict
    # "model" when the model picked it, "keywords" when the router did, "thread" when a
    # follow-up took its subject from the thread.
    via: str


@dataclass(frozen=True)
class ThreadSubject:
    entity: Entity
    # The tool and depth the last reply in the thread used, read from its footer and path header.
    tool: str | None = None
    depth: int | None = None


def catalogue_schema(specs: list[ToolSpec] | None = None) -> dict:
    """The JSON schema the model's reply must match: one tool name and that tool's arguments."""
    specs = catalogue.list_tools() if specs is None else specs
    properties: dict = {}
    for spec in specs:
        for key, schema in spec.args_schema.get("properties", {}).items():
            properties.setdefault(key, schema)
    return {
        "type": "object",
        "properties": {
            "tool": {"type": "string", "enum": [spec.name for spec in specs]},
            "args": {"type": "object", "properties": properties, "additionalProperties": False},
        },
        "required": ["tool", "args"],
        "additionalProperties": False,
    }


def describe_catalogue(specs: list[ToolSpec]) -> str:
    lines = []
    for spec in specs:
        note = " Writes the graph; the asker approves first." if spec.needs_approval else ""
        lines.append(f"- {spec.name}: {spec.description}{note}\n  args: {json.dumps(spec.args_schema.get('properties', {}))}")
    return "\n".join(lines)


def thread_context(thread: list[dict] | None, limit: int = THREAD_CONTEXT_MESSAGES) -> str:
    lines = []
    for message in (thread or [])[-limit:]:
        text = " ".join(str(message.get("text") or "").split())
        if not text:
            continue
        speaker = "app" if message.get("bot_id") or message.get("app_id") else (message.get("user") or "someone")
        lines.append(f"{speaker}: {text[:THREAD_MESSAGE_CHARS]}")
    return "\n".join(lines)


def write_intent(question: str) -> bool:
    return bool(WRITE_INTENT.search(question or ""))


def _is_bot(message: dict, bot_ids: frozenset[str]) -> bool:
    return bool(message.get("bot_id") or message.get("app_id") or message.get("user") in bot_ids)


def thread_entities(thread: list[dict] | None, scopes: list[str], bot_ids: frozenset[str] = frozenset(), skip_text: str = "") -> list[Entity]:
    """Nodes people named in the thread, most recent first. Bot replies are skipped: they name many things at once."""
    named: list[Entity] = []
    for message in reversed(thread or []):
        text = str(message.get("text") or "")
        if not text or (skip_text and skip_text in text) or _is_bot(message, bot_ids):
            continue
        found = resolve.resolve_name(text, THREAD_LABELS, scopes)
        if found.found and found.entity not in named:
            named.append(found.entity)
        if len(named) == THREAD_NAMED_LIMIT:
            break
    return named


def thread_subject(thread: list[dict] | None, scopes: list[str], bot_ids: frozenset[str] = frozenset(), skip_text: str = "") -> ThreadSubject | None:
    named = thread_entities(thread, scopes, bot_ids, skip_text)
    if not named:
        return None
    tool = depth = None
    for message in reversed(thread or []):
        if _is_bot(message, bot_ids):
            text = str(message.get("text") or "")
            if match := _TOOL_IN_REPLY.search(text):
                tool = match.group(1)
            if match := _DEPTH_IN_REPLY.search(text):
                depth = int(match.group(1))
            break
    return ThreadSubject(named[0], tool, depth)


def follow_up_choice(question: str, choice: ToolChoice, subject: ThreadSubject) -> ToolChoice | None:
    """The same question asked about the thread's subject. Depth words, or a thread about blast radius, mean blast_radius."""
    depth = algorithms.depth_in(question)
    if depth is None and _DEEPER.search(question):
        depth = algorithms.clamp_depth((subject.depth or 1) + 1)
    if depth is not None or (subject.tool == "blast_radius" and choice.tool in ("who_knows", "who_owns")):
        return ToolChoice("blast_radius", {"name": subject.entity.name, "depth": depth or algorithms.clamp_depth((subject.depth or 1) + 1)}, "thread")
    spec = catalogue.get_tool(choice.tool)
    properties = spec.args_schema.get("properties", {}) if spec else {}
    for key in ("name", "topic", "query"):
        if key in properties:
            return ToolChoice(choice.tool, {**choice.args, key: subject.entity.name}, "thread")
    return None


def run_choice(
    choice: ToolChoice, question: str, scopes: list[str], thread: list[dict] | None = None, bot_ids: frozenset[str] = frozenset()
) -> tuple[ToolResult, ToolChoice]:
    """Run the chosen tool. A question that names nothing is retried about the thread's subject."""
    result = catalogue.get_tool(choice.tool).run(choice.args, scopes)
    if result.found or result.candidates or not thread:
        return result, choice
    subject = thread_subject(thread, scopes, bot_ids, skip_text=question)
    if subject is None:
        return result, choice
    retry = follow_up_choice(question, choice, subject)
    if retry is None:
        return result, choice
    log.info("[agent] %r named nothing; asking %s about %s from the thread", question, retry.tool, subject.entity.name)
    retried = catalogue.get_tool(retry.tool).run(retry.args, scopes)
    return (retried, retry) if retried.found else (result, choice)


def choose_tool(
    question: str,
    thread: list[dict] | None = None,
    specs: list[ToolSpec] | None = None,
    scopes: list[str] | None = None,
    bot_ids: frozenset[str] = frozenset(),
) -> ToolChoice:
    """One structured model call, or the keyword router when the model is unavailable or wrong."""
    specs = catalogue.list_tools() if specs is None else specs
    named = thread_entities(thread, scopes, bot_ids, skip_text=question) if thread and scopes else []
    try:
        choice = _choose_with_model(question, thread, specs, named)
        spec = next(candidate for candidate in specs if candidate.name == choice.tool)
        if spec.needs_approval and not write_intent(question):
            raise InvalidToolChoice(f"{spec.name} writes the graph but the question does not ask for a write")
        return choice
    except ModelUnavailable as error:
        log.info("[agent] model unavailable (%s); keyword router", error)
    except InvalidToolChoice as error:
        log.warning("[agent] model choice rejected (%s); keyword router", error)
    return choose_with_keywords(question)


def _choose_with_model(question: str, thread: list[dict] | None, specs: list[ToolSpec], named: list[Entity] | None = None) -> ToolChoice:
    prompt = "Tools:\n" + describe_catalogue(specs)
    context = thread_context(thread)
    if context:
        prompt += f"\n\nThread so far:\n{context}"
    if named:
        # Real names from the graph, so the model copies one instead of inventing one.
        prompt += "\n\nThings named so far in this thread: " + ", ".join(f"{entity.name} ({entity.label})" for entity in named)
    prompt += f"\n\nQuestion: {question}\n\nReply with the tool and its arguments."
    reply = generate_object(
        ModelRequest(profile=MODEL_PROFILE, system=SYSTEM_PROMPT, prompt=prompt, schema=catalogue_schema(specs), max_tokens=400)
    )
    name = reply.get("tool")
    spec = next((candidate for candidate in specs if candidate.name == name), None)
    if spec is None:
        raise InvalidToolChoice(f"unknown tool {name!r}")
    return ToolChoice(spec.name, validate_args(spec, reply.get("args")), via="model")


def choose_with_keywords(question: str) -> ToolChoice:
    tool, args = ask_service.route(question)
    if catalogue.get_tool(tool) is None:
        tool, args = "who_knows", {"name": question}
    return ToolChoice(tool, args, via="keywords")


def validate_args(spec: ToolSpec, args) -> dict:
    """Keep only the arguments the tool declares, with the declared types. Required ones must be present."""
    if not isinstance(args, dict):
        raise InvalidToolChoice(f"{spec.name}: args must be an object")
    clean: dict = {}
    for key, schema in spec.args_schema.get("properties", {}).items():
        value = args.get(key)
        if value is None:
            continue
        kind = schema.get("type")
        if kind == "string":
            if not isinstance(value, str) or not value.strip():
                raise InvalidToolChoice(f"{spec.name}.{key}: expected text")
            clean[key] = value.strip()
        elif kind == "integer":
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise InvalidToolChoice(f"{spec.name}.{key}: expected an integer")
            try:
                number = int(value)
            except ValueError:
                raise InvalidToolChoice(f"{spec.name}.{key}: expected an integer") from None
            if schema.get("minimum") is not None:
                number = max(int(schema["minimum"]), number)
            if schema.get("maximum") is not None:
                number = min(int(schema["maximum"]), number)
            clean[key] = number
        elif kind == "array":
            if not isinstance(value, list):
                raise InvalidToolChoice(f"{spec.name}.{key}: expected a list")
            clean[key] = [str(item).strip() for item in value if str(item).strip()]
        else:
            clean[key] = value
    missing = [key for key in spec.args_schema.get("required", []) if key not in clean]
    if missing:
        raise InvalidToolChoice(f"{spec.name}: missing {', '.join(missing)}")
    return clean
