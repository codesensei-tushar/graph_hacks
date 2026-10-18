"""``compute_turn``: the one place a Slack turn decides what to say.

company-brain's ``computeTurn`` (``brain/turn/compute.ts``) is the LLM agent loop:
it picks tools, reads memory, proposes writes, and returns a reply, a memory
writeback, or an approval suspension. That agent is not built yet.

**Agent plug-in point.** Call ``register_turn_agent(fn)`` with a function that takes a
``TurnRequest`` and returns a ``TurnOutcome``. Until one is registered, the default
below answers from the company graph (``domain/knowledge``) with the memories that mention
what it answers about, and handles the built-in commands, greetings and thanks.

Everything around this seam (turn control, steering, approvals, streaming, memory
writeback, reactions) is the ported turn loop in ``turn_service.py``, so an agent only
has to produce a ``TurnOutcome``.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from src.connectors.graph import container_names
from src.domain.github.repos import repo_names
from src.domain.access.roles import ROLE_OWNER, role_at_least, role_of, set_role
from src.domain.knowledge import agent, asker, catalogue, smalltalk
from src.domain.knowledge import memories as knowledge_memories
from src.domain.memory import memory_service
from src.domain.memory.writeback import (
    ORG_INTERNAL_CONTAINER_TAG,
    SHARED_TEAM_BRAIN_CONTAINER_TAG,
    SlackMemoryScope,
    scope_label,
    slack_memory_container_tag,
)
from src.domain.onboarding import onboard_command
from src.domain.onboarding.team_import import TeamFileError
from src.domain.slack import channel_membership, format
from src.integrations.slack.events import SlackEventInner, mentioned_user_ids

log = logging.getLogger(__name__)

TICKET_TOOL_NAME = "Create ticket"

_REMEMBER = re.compile(r"^(?:remember|note)\s*[:\-]\s*(.+)$", re.IGNORECASE | re.DOTALL)
_TICKET = re.compile(r"^(?:ticket|file ticket|new ticket)\s*[:\-]\s*(.+)$", re.IGNORECASE | re.DOTALL)
_DECISION = re.compile(r"^(?:decision|decided|record decision|we decided)\s*[:\-]\s*(.+)$", re.IGNORECASE | re.DOTALL)
_SCOPES = re.compile(r"^(?:what can you see|scopes?|what do you know about me)\??$", re.IGNORECASE)
_PROMOTE = re.compile(r"^(promote|demote)\b", re.IGNORECASE)
_FORGET = re.compile(r"^forget (?:this )?(?:channel|conversation|dm)\b", re.IGNORECASE)
_LINK = re.compile(r"^link (?:my )?(?:slack )?account\b", re.IGNORECASE)
_LINK_GITHUB = re.compile(r"^link (?:my )?github\b", re.IGNORECASE)
_BACKFILL = re.compile(r"^(?:backfill|import|add me to|join)(?: (?:the|my|all))? public channels\b", re.IGNORECASE)
_DISCONNECT = re.compile(r"^disconnect(?: (?:company brain|slack|this workspace))?\s*$", re.IGNORECASE)


@dataclass
class TurnApproval:
    """A consequential write waiting for the asker's Approve/Deny."""

    tool_name: str
    tool_input: dict
    summary: str
    container_tag: str


@dataclass
class TurnRequest:
    workspace: object
    event: SlackEventInner
    question: str
    raw_text: str
    asker_slack_user_id: str
    asker_name: str | None
    memory_scope: SlackMemoryScope
    read_tags: list[str]
    thread: list[dict]
    channel: str
    thread_ts: str
    is_dm: bool
    progress: object | None = None
    trace_id: str | None = None
    turn_steering: str | None = None
    live_updates: list[str] = field(default_factory=list)
    passive_investigation: dict | None = None
    agent_main_effort: str | None = None


@dataclass
class TurnOutcome:
    # "done" | "suspended"
    status: str = "done"
    reply: str = ""
    # MemoryWriteback docs (memory/writeback.py) to save after the reply.
    memory: object | None = None
    approval: TurnApproval | None = None
    # Passive investigation concluded there is nothing worth saying.
    silent_conclusion: bool = False
    # Side effect a command wants after delivery, e.g. disconnecting the workspace.
    after_reply: Callable[[], None] | None = None


TurnAgent = Callable[[TurnRequest], TurnOutcome]
_agent: TurnAgent | None = None


def register_turn_agent(agent: TurnAgent | None) -> None:
    """Install the LLM agent. ``None`` restores the built-in graph answerer."""
    global _agent
    _agent = agent


def compute_turn(request: TurnRequest) -> TurnOutcome:
    command = _command(request)
    if command is not None:
        return command
    if _agent is not None:
        return _agent(request)
    return _answer_from_graph(request)


# --- default answerer ------------------------------------------------------------------


def _answer_from_graph(request: TurnRequest) -> TurnOutcome:
    if request.passive_investigation:
        # Investigating unprompted needs the agent; without it, stay silent.
        return TurnOutcome(silent_conclusion=True)
    question = " ".join([request.question, *request.live_updates]).strip()
    # The same keyword choice and thread fallback the agent uses, minus the model call.
    bot_ids = frozenset(filter(None, [getattr(request.workspace, "bot_user_id", None)]))
    # I, me and my are the asker, as in the agent turn; an unknown asker is told how to link, in Slack.
    choice = asker.for_the_asker(agent.choose_with_keywords(question), question, request.asker_slack_user_id, request.read_tags)
    result, choice = agent.run_choice(choice, question, request.read_tags, request.thread, bot_ids)
    result = asker.in_slack(result)
    # Only memories that mention what the answer is about, or something one hop from it.
    memories = result.memories or (knowledge_memories.attached(result, request.read_tags) if result.found else [])
    memories = [memory.to_dict() for memory in memories]
    names = channel_names(request.read_tags)
    return TurnOutcome(reply=format.format_answer(result.to_dict(), memories, request.read_tags, names, chosen_by=choice.via))


def channel_names(tags: list[str]) -> dict[str, str]:
    ids = [tag.removeprefix("slack_channel_") for tag in tags if tag.startswith("slack_channel_")]
    return {**channel_membership.channel_names(ids), **repo_names(tags), **container_names(tags)}


# --- built-in commands -------------------------------------------------------------------


def _command(request: TurnRequest) -> TurnOutcome | None:
    text = request.question
    asker = request.asker_slack_user_id
    scope = request.memory_scope
    write_tag = slack_memory_container_tag(scope)
    names = channel_names(request.read_tags + ([write_tag] if write_tag else []))

    if not text or text.lower() in ("help", "?"):
        return TurnOutcome(reply=format.HELP_TEXT)
    # "hi", "what can you do", "thanks!": help or an acknowledgement, never a graph question.
    small = smalltalk.kind(text)
    if small == smalltalk.HELP:
        return TurnOutcome(reply=format.HELP_TEXT)
    if small == smalltalk.THANKS:
        return TurnOutcome(reply=smalltalk.THANKS_TEXT)
    if _SCOPES.match(text):
        return TurnOutcome(reply=format.format_scopes(request.read_tags, write_tag, names))

    if match := _REMEMBER.match(text):
        if not write_tag:
            return TurnOutcome(reply="I can't save anything from here.")
        memory_service.write(match.group(1).strip(), write_tag, asker, scope.channel_id or "", request.event.ts or "")
        return TurnOutcome(reply="Saved. Only you can recall it, in your DMs with me." if write_tag.startswith("user_") else f"Saved. Anyone who can read {scope_label(write_tag, names)} can recall it.")

    if match := _TICKET.match(text):
        title, about = _split_ticket(match.group(1).strip())
        container = write_tag or SHARED_TEAM_BRAIN_CONTAINER_TAG
        tool_input = {"title": title, "about": about} if about else {"title": title}
        preview = catalogue.get_tool("ingest_ticket").preview(tool_input, request.read_tags)
        return TurnOutcome(
            status="suspended",
            approval=TurnApproval(
                tool_name=TICKET_TOOL_NAME,
                tool_input=tool_input,
                summary=f"{preview}. Visible to {scope_label(container, names)}.",
                container_tag=container,
            ),
        )

    if match := _DECISION.match(text):
        # The no-model way to record a decision; with a model the agent picks record_decision itself.
        container = write_tag or SHARED_TEAM_BRAIN_CONTAINER_TAG
        spec = catalogue.get_tool("record_decision")
        tool_input = {"title": match.group(1).strip(), "asker_slack_user_id": asker, "channel_id": request.channel}
        if decided_at := _epoch_millis(request.event.ts):
            tool_input["decided_at"] = decided_at
        preview = spec.preview(tool_input, request.read_tags)
        return TurnOutcome(
            status="suspended",
            approval=TurnApproval(
                tool_name=spec.name,
                tool_input=tool_input,
                summary=f"{preview}. Visible to {scope_label(container, names)}.",
                container_tag=container,
            ),
        )

    if onboard_command.COMMAND.match(text):
        return _onboard(request, write_tag, names)

    if _PROMOTE.match(text):
        # Mentions are stripped from the question; read targets from the raw message.
        return TurnOutcome(reply=_change_role(asker, text, request.raw_text, request.workspace.bot_user_id))

    if _FORGET.match(text):
        return TurnOutcome(reply=_forget(asker, scope, write_tag, names))

    if _LINK.match(text):
        return TurnOutcome(reply=_link_account(request))

    if _LINK_GITHUB.match(text):
        return TurnOutcome(reply=_link_github(request))

    if _BACKFILL.match(text):
        from src.domain.slack.channels.rollout import start_public_channel_rollout_from_command

        return TurnOutcome(reply=start_public_channel_rollout_from_command(request.workspace.team_id, asker))

    if _DISCONNECT.match(text):
        if not role_at_least(role_of(asker), "admin"):
            return TurnOutcome(reply="Only owners and admins can disconnect Company Brain.")
        from src.domain.slack.workspace import disconnect_slack_workspace

        team_id = request.workspace.team_id
        return TurnOutcome(
            reply="Disconnecting Company Brain from this workspace. An admin can reinstall it anytime.",
            after_reply=lambda: disconnect_slack_workspace(team_id),
        )
    return None


def _epoch_millis(ts) -> int | None:
    """A Slack message ts ("1700000000.000100") as epoch milliseconds, or None."""
    try:
        millis = int(float(ts) * 1000)
    except (TypeError, ValueError):
        return None
    return millis if millis > 0 else None


def _split_ticket(text: str) -> tuple[str, str]:
    """``<title> about <service>`` names the service outright; otherwise it is read from the title."""
    title, separator, about = text.rpartition(" about ")
    if separator and title.strip() and about.strip():
        return title.strip(), about.strip()
    return text, ""


def _onboard(request: TurnRequest, write_tag: str | None, names: dict[str, str]) -> TurnOutcome:
    """``onboard team:``: check the line against the graph, then wait for Approve on a card that lists it all."""
    if request.is_dm:
        return TurnOutcome(reply="Onboard a team from a channel, so the people in it can see it.")
    # Members are mentions, which the question has lost; read the raw message without the bot's own.
    bot = getattr(request.workspace, "bot_user_id", None) or ""
    raw = re.sub(rf"<@{re.escape(bot)}(?:\|[^>]*)?>", "", request.raw_text or "").strip() if bot else (request.raw_text or "").strip()
    match = onboard_command.COMMAND.match(raw)
    container = write_tag or SHARED_TEAM_BRAIN_CONTAINER_TAG
    try:
        args = onboard_command.command_args(match.group(1) if match else "")
        unlinked = set(onboard_command.unlinked_mentions(args, request.read_tags))
        # A new person may be named by their handle when Slack has nothing better; someone already
        # here keeps their name unless Slack has a real or display name for them.
        args["profiles"], args["handles"] = {}, {}
        for user in onboard_command.mentioned_users(args):
            info = _profile(request.workspace, user)
            if info is None:
                continue
            if name := info.real_name or info.display_name or (info.name if user in unlinked else None):
                args["profiles"][user] = name
            if info.name:
                args["handles"][user] = info.name
        args["asker_slack_user_id"] = request.asker_slack_user_id
        args["channel_id"] = request.channel
        if decided_at := _epoch_millis(request.event.ts):
            args["decided_at"] = decided_at
        team = onboard_command.check(args, container, request.read_tags)
    except TeamFileError as error:
        if onboard_command.NO_COMPANY in error.problems:
            return TurnOutcome(reply=onboard_command.NO_COMPANY)
        return TurnOutcome(reply=f"I can't onboard that yet: {onboard_command.for_slack(error.problems)}.")
    return TurnOutcome(
        status="suspended",
        approval=TurnApproval(
            tool_name=onboard_command.TOOL_NAME,
            tool_input=args,
            summary=f"{onboard_command.card(team)}. Visible to {scope_label(container, names)}.{_onboard_approver()}",
            container_tag=container,
        ),
    )


def _onboard_approver() -> str:
    """In a console org an owner or admin approves onboarding (``turn_service``); say so on the card."""
    from src.core import tenancy

    return "" if tenancy.is_legacy() else " An owner or admin approves this."


def _profile(workspace, slack_user_id: str):
    """A full member's Slack profile (real name, display name, login handle in ``name``); None for a
    bot, a guest, or a failed lookup. Names prefer the real name, then the display name, and use the
    handle only for someone new."""
    from src.domain.slack.workspace import is_workspace_member

    info = workspace.client.user_info(slack_user_id)
    if info is None or not is_workspace_member(info):
        return None
    return info


def _change_role(asker: str, text: str, raw_text: str, bot_user_id: str | None) -> str:
    if not role_at_least(role_of(asker), "admin"):
        return "Only owners and admins can change roles."
    targets = mentioned_user_ids(raw_text, bot_user_id)
    if not targets:
        return "Mention who to change, like `promote @someone`."
    promote = text.lower().startswith("promote")
    done = []
    for target in targets:
        if role_of(target) == ROLE_OWNER:
            continue
        set_role(target, "admin" if promote else "member")
        done.append(f"<@{target}>")
    if not done:
        return "The owner's role can't be changed here."
    return f"{', '.join(done)} {'is now an admin' if promote else 'is now a member'}."


def _forget(asker: str, scope: SlackMemoryScope, write_tag: str | None, names: dict[str, str]) -> str:
    if not write_tag:
        return "There's nothing saved from here."
    if scope.kind == "dm":
        count = memory_service.forget_scope(write_tag)
        return f"Forgot {count} memories from your DMs."
    if not role_at_least(role_of(asker), "admin"):
        return "Only owners and admins can wipe a channel's memory."
    channel_filter = scope.channel_id if write_tag == SHARED_TEAM_BRAIN_CONTAINER_TAG else None
    count = memory_service.forget_scope(write_tag, channel_filter)
    where = f"<#{scope.channel_id}>" if channel_filter else scope_label(write_tag, names)
    return f"Forgot {count} memories from {where}."


def _link_github(request: TurnRequest) -> str:
    from src.domain.github.account_link import create_github_link_url
    from src.integrations.github import app_config

    if not app_config.link_enabled():
        return "GitHub linking isn't set up on this server yet."
    if not request.is_dm:
        # The link is personal; it belongs in a DM, not a channel.
        return "Send `link github` to me in a DM and I'll give you a sign-in link."
    url = create_github_link_url(request.asker_slack_user_id)
    return (
        f"<{url}|Sign in with GitHub> to link your account. It expires in 15 minutes. "
        "Once linked, I can use private repos you collaborate on when you ask me in a DM."
    )


def _link_account(request: TurnRequest) -> str:
    from src.domain.slack.account_link import post_slack_account_link_prompt

    workspace = request.workspace
    info = workspace.client.user_info(request.asker_slack_user_id)
    posted = post_slack_account_link_prompt(
        web=workspace.client.web,
        team_id=workspace.team_id,
        slack_user_id=request.asker_slack_user_id,
        org_name=workspace.team_name or "this workspace",
        channel=request.channel,
        is_dm=request.is_dm,
        thread_ts=request.thread_ts,
        slack_email=info.email if info else None,
        slack_display_name=request.asker_name,
    )
    if not posted:
        return "I couldn't create a link just now. Try again in a minute."
    return "" if request.is_dm else "I sent you a private link to connect your account."


# --- approved actions -------------------------------------------------------------------


def execute_approved_action(tool_name: str, tool_input: dict, container_tag: str, internal_audience: bool = False) -> str:
    """Run an approved write through the tool catalogue. The write lands in ``container_tag``.

    ``internal_audience`` is whether everyone in the room the approval came from is a full member
    (read_scope.py): only then may the action read org_internal, or it could name a connector
    account to a guest and link an org_shared node to it. It fails closed."""
    internal = [ORG_INTERNAL_CONTAINER_TAG] if internal_audience else []
    read_tags = list(dict.fromkeys([SHARED_TEAM_BRAIN_CONTAINER_TAG, *internal, container_tag]))
    spec = catalogue.get_tool("ingest_ticket" if tool_name == TICKET_TOOL_NAME else tool_name)
    if spec is None or not spec.needs_approval:
        raise ValueError(f"unknown approved tool {tool_name}")
    result = spec.run({**tool_input, "scope": container_tag}, read_tags)
    return format.format_answer(result.to_dict(), [], read_tags, channel_names(read_tags), chosen_by="approval")
