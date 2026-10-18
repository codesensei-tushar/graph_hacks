"""The Slack turn that the agent answers: pick a tool, run it, cite the path.

``answer_turn`` is the ``TurnAgent`` installed through ``register_turn_agent``. Tool
choice lives in ``src/domain/knowledge/agent.py`` and knows nothing about Slack; this
module turns a ``TurnRequest`` into a ``TurnOutcome``. Tools that write the graph never
run here: the turn suspends with a ``TurnApproval`` and ``execute_approved_action`` runs
the tool after the asker approves, with the approved write scope, the asker, the
channel, and the message time (milliseconds, like every timestamp in the graph) added to
the arguments the model filled in.
"""

import re

from src.db.falkordb_client import connect
from src.domain.knowledge import catalogue, resolve
from src.domain.knowledge.agent import ToolChoice, choose_tool, run_choice
from src.domain.knowledge.dto import MemoryLine, ToolResult, ToolSpec, day_from
from src.domain.memory.search_brain import search_brain
from src.domain.memory.writeback import SHARED_TEAM_BRAIN_CONTAINER_TAG, scope_label, slack_memory_container_tag
from src.domain.slack import format
from src.domain.slack.compute import TurnApproval, TurnOutcome, TurnRequest, channel_names

MEMORY_LINES = 3
# "what did I miss", "catch me up": the asker's own things, when Slack knows who they are.
_FIRST_PERSON = re.compile(r"\b(?:i|me|my|mine)\b", re.IGNORECASE)


def answer_turn(request: TurnRequest) -> TurnOutcome:
    if request.passive_investigation:
        # Investigating unprompted would need a judgement call; the graph tools only answer questions.
        return TurnOutcome(silent_conclusion=True)
    question = " ".join([request.question, *request.live_updates]).strip()
    bot_ids = frozenset(filter(None, [getattr(request.workspace, "bot_user_id", None)]))
    choice = choose_tool(question, request.thread, scopes=request.read_tags, bot_ids=bot_ids)
    spec = catalogue.get_tool(choice.tool)
    if spec.needs_approval:
        return suspend_for_approval(request, spec, choice)
    choice = for_the_asker(choice, question, request)
    result, choice = run_choice(choice, question, request.read_tags, request.thread, bot_ids)
    # A refusal shows no memories, so it never reads like a guess.
    memories = result.memories or (recall_memories(question, request) if result.found else [])
    # One the answer already quotes (what_changed tells each in full) is not listed twice.
    memories = [memory for memory in memories if not memory.text or memory.text not in result.answer]
    return TurnOutcome(reply=render_reply(result, choice, memories, request.read_tags, channel_names(request.read_tags)))


# Tools that take a person, and the argument that names them: asked in the first person ("what team am
# I on", "what if I leave", "my open tickets"), Slack knows who the asker is and names them.
_PERSON_ARGS = {"person_profile": ("name",), "if_person_leaves": ("name",), "find": ("person", "owner", "author")}


def for_the_asker(choice: ToolChoice, question: str, request: TurnRequest) -> ToolChoice:
    """A first-person question reads around the asker's own person, when they are a person in the graph:
    ``what_changed`` about their things, and the person tools (profile, leaving, their lists) about them."""
    if not _FIRST_PERSON.search(question):
        return choice
    if choice.tool == "what_changed":
        if choice.args.get("person"):
            return choice
        person = asker_person(request.asker_slack_user_id)
        return ToolChoice(choice.tool, {**choice.args, "person": person}, choice.via) if person else choice
    keys = [key for key in _PERSON_ARGS.get(choice.tool, ()) if key in choice.args and (key == "name" or _FIRST_PERSON.search(str(choice.args[key])))]
    if not keys or (choice.tool != "find" and not _FIRST_PERSON.search(str(choice.args.get("name") or ""))):
        return choice
    if choice.tool != "find" and resolve.resolve_name(str(choice.args["name"]), ("Person",), request.read_tags).found:
        # "what if Arjun leaves, should I worry": someone else is named; the question is about them.
        return choice
    person = asker_person(request.asker_slack_user_id)
    return ToolChoice(choice.tool, {**choice.args, **{key: person for key in keys}}, choice.via) if person else choice


def asker_person(slack_user_id: str | None) -> str | None:
    """The name of the person a Slack user is, through the workspace's IS link."""
    if not slack_user_id:
        return None
    _, graph = connect()
    rows = graph.ro_query("MATCH (:SlackUser {id: $id})-[:IS]->(p:Person) RETURN p.name LIMIT 1", {"id": slack_user_id}).result_set
    return rows[0][0] if rows else None


def suspend_for_approval(request: TurnRequest, spec: ToolSpec, choice: ToolChoice) -> TurnOutcome:
    container = slack_memory_container_tag(request.memory_scope) or SHARED_TEAM_BRAIN_CONTAINER_TAG
    names = channel_names(request.read_tags + [container])
    tool_input = {**choice.args, "asker_slack_user_id": request.asker_slack_user_id, "channel_id": request.channel}
    decided_at = epoch_ms(getattr(request.event, "ts", None))
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
        learned_at=learned_at(row),
        author=row.get("author") or metadata.get("author_name") or metadata.get("author_id") or "someone",
        scope=row.get("container_tag") or row.get("scope") or SHARED_TEAM_BRAIN_CONTAINER_TAG,
        channel_id=metadata.get("slack_channel_id") or row.get("channel_id"),
    )


def learned_at(row: dict) -> str:
    """When a memory was first saved: created_at, else the message ts, else the last update."""
    return day_from(row.get("created_at"), row.get("ts")) or day_from(row.get("updated_at"))


def render_reply(result: ToolResult, choice: ToolChoice, memories: list[MemoryLine], read_tags: list[str], names: dict[str, str]) -> str:
    return format.format_answer(result.to_dict(), [memory.to_dict() for memory in memories], read_tags, names, chosen_by=choice.via)


def epoch_ms(ts) -> int | None:
    """A Slack message ts ("1700000000.000100") as milliseconds since the epoch."""
    try:
        milliseconds = int(float(ts) * 1000)
    except (TypeError, ValueError):
        return None
    return milliseconds if milliseconds > 0 else None
