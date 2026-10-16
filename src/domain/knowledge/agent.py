"""The agent loop: one model call picks a tool, the tool runs, the reply cites the path.

The model never writes a query. It sees the catalogue (tool names, descriptions, and
argument schemas) and the question, and returns ``{tool, args}``. The choice is checked
against the registry and the tool's own schema before anything runs. If no model is
configured, the call fails, or the choice is invalid, the keyword router in
``ask_service`` picks the tool instead, so every path works with no key.

Tools that write the graph never run here. The turn suspends with a ``TurnApproval``
and ``execute_approved_action`` runs the tool after the asker approves, with the
approved write scope, the asker, the channel, and the message time added to the
arguments the model filled in.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from src.domain.knowledge import ask_service, catalogue
from src.domain.knowledge.dto import MemoryLine, ToolResult, ToolSpec
from src.domain.memory.search_brain import search_brain
from src.domain.memory.writeback import SHARED_TEAM_BRAIN_CONTAINER_TAG, scope_label, slack_memory_container_tag
from src.domain.slack import format
from src.domain.slack.compute import TurnApproval, TurnOutcome, TurnRequest, channel_names
from src.integrations.llm.model import ModelRequest, ModelUnavailable, generate_object

log = logging.getLogger(__name__)

MODEL_PROFILE = "fast"
THREAD_CONTEXT_MESSAGES = 6
THREAD_MESSAGE_CHARS = 300
MEMORY_LINES = 3

SYSTEM_PROMPT = (
    "You route questions about a company to one tool from a fixed catalogue. You never write queries "
    "and never answer the question yourself.\n"
    "Pick exactly one tool. Fill its arguments with words copied from the question, or from the thread "
    "when the question refers back to it. Do not invent names. Keep each argument short: the thing "
    "named, not the whole sentence.\n"
    "When unsure which tool fits, pick who_knows."
)


class InvalidToolChoice(ValueError):
    """The model named a tool the catalogue lacks, or arguments the tool's schema rejects."""


@dataclass(frozen=True)
class ToolChoice:
    tool: str
    args: dict
    # "model" when the model picked it, "keywords" when the router did.
    via: str


# --- choosing ---------------------------------------------------------------------------


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


def choose_tool(question: str, thread: list[dict] | None = None, specs: list[ToolSpec] | None = None) -> ToolChoice:
    """One structured model call, or the keyword router when the model is unavailable or wrong."""
    specs = catalogue.list_tools() if specs is None else specs
    try:
        return _choose_with_model(question, thread, specs)
    except ModelUnavailable as error:
        log.info("[agent] model unavailable (%s); keyword router", error)
    except InvalidToolChoice as error:
        log.warning("[agent] model choice rejected (%s); keyword router", error)
    return _choose_with_keywords(question)


def _choose_with_model(question: str, thread: list[dict] | None, specs: list[ToolSpec]) -> ToolChoice:
    prompt = "Tools:\n" + describe_catalogue(specs)
    context = thread_context(thread)
    if context:
        prompt += f"\n\nThread so far:\n{context}"
    prompt += f"\n\nQuestion: {question}\n\nReply with the tool and its arguments."
    reply = generate_object(
        ModelRequest(profile=MODEL_PROFILE, system=SYSTEM_PROMPT, prompt=prompt, schema=catalogue_schema(specs), max_tokens=400)
    )
    name = reply.get("tool")
    spec = next((candidate for candidate in specs if candidate.name == name), None)
    if spec is None:
        raise InvalidToolChoice(f"unknown tool {name!r}")
    return ToolChoice(spec.name, validate_args(spec, reply.get("args")), via="model")


def _choose_with_keywords(question: str) -> ToolChoice:
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


# --- the turn ----------------------------------------------------------------------------


def answer_turn(request: TurnRequest) -> TurnOutcome:
    """The ``TurnAgent`` installed through ``register_turn_agent``."""
    if request.passive_investigation:
        # Investigating unprompted would need a judgement call; the graph tools only answer questions.
        return TurnOutcome(silent_conclusion=True)
    question = " ".join([request.question, *request.live_updates]).strip()
    choice = choose_tool(question, request.thread)
    spec = catalogue.get_tool(choice.tool)
    if spec.needs_approval:
        return suspend_for_approval(request, spec, choice)
    result = spec.run(choice.args, request.read_tags)
    memories = result.memories or recall_memories(question, request)
    return TurnOutcome(reply=render_reply(result, choice, memories, request.read_tags, channel_names(request.read_tags)))


def suspend_for_approval(request: TurnRequest, spec: ToolSpec, choice: ToolChoice) -> TurnOutcome:
    container = slack_memory_container_tag(request.memory_scope) or SHARED_TEAM_BRAIN_CONTAINER_TAG
    names = channel_names(request.read_tags + [container])
    tool_input = {**choice.args, "asker_slack_user_id": request.asker_slack_user_id, "channel_id": request.channel}
    decided_at = _epoch_seconds(getattr(request.event, "ts", None))
    if decided_at is not None:
        tool_input["decided_at"] = decided_at
    return TurnOutcome(
        status="suspended",
        approval=TurnApproval(
            tool_name=spec.name,
            tool_input=tool_input,
            summary=approval_summary(spec, choice.args, request.read_tags, scope_label(container, names)),
            container_tag=container,
        ),
    )


def approval_summary(spec: ToolSpec, args: dict, scopes: list[str], visible_to: str) -> str:
    """What the card says. A tool's preview shows the resolved target, so the asker approves a known write."""
    if spec.preview is not None:
        return f"{spec.preview(args, scopes)}. Visible to {visible_to}."
    what = spec.description.split(". ")[0].rstrip(".")
    shown = ", ".join(f"{key}: *{value}*" for key, value in args.items() if value not in (None, "", []))
    return f"{what}. {shown}. Visible to {visible_to}."


def recall_memories(question: str, request: TurnRequest) -> list[MemoryLine]:
    found = search_brain(question, request.memory_scope, trace_id=request.trace_id)
    return [memory_line(row) for row in (found.get("results") or [])[:MEMORY_LINES]]


def memory_line(row: dict) -> MemoryLine:
    metadata = row.get("metadata") or {}
    return MemoryLine(
        text=row.get("memory") or row.get("content") or row.get("text") or "",
        learned_at=learned_at(row.get("updated_at"), row.get("ts")),
        author=row.get("author") or metadata.get("author_name") or metadata.get("author_id") or "someone",
        scope=row.get("container_tag") or row.get("scope") or SHARED_TEAM_BRAIN_CONTAINER_TAG,
        channel_id=metadata.get("slack_channel_id") or row.get("channel_id"),
    )


def learned_at(updated_at_ms, ts) -> str:
    """The day a memory was saved, from its ms timestamp or the Slack message ts."""
    seconds = None
    if isinstance(updated_at_ms, (int, float)) and not isinstance(updated_at_ms, bool) and updated_at_ms > 0:
        seconds = updated_at_ms / 1000
    else:
        seconds = _epoch_seconds(ts)
    if not seconds:
        return ""
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d")


def render_reply(result: ToolResult, choice: ToolChoice, memories: list[MemoryLine], read_tags: list[str], names: dict[str, str]) -> str:
    return format.format_answer(result.to_dict(), [memory.to_dict() for memory in memories], read_tags, names, chosen_by=choice.via)


def _epoch_seconds(ts) -> int | None:
    try:
        seconds = int(float(ts))
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None
