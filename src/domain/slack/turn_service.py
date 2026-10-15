"""One Slack turn: someone DMs the bot, @mentions it, or replies in its thread.

Core path of company-brain ``brain/slack/turn.ts`` (``runSlackTurn``) and
``runSlackApprovalDecision``, without the model loop: the graph tools in
``domain/knowledge`` answer instead of an LLM until the agent lands.

1. Ignore bots and self. Outside DMs, a thread reply without a mention only counts
   in a thread the bot is already in; otherwise it is context.
2. Resolve the asker. Guests and external users get a private denial.
3. Resolve the memory scope from channel privacy (fail closed) and the read surface.
4. Ack with a reaction, answer in the thread, cite the path and where memory came from.
5. Consequential writes (tickets) suspend for an Approve/Deny card only the asker can click.
"""

import logging
import re

from src.core.config import settings
from src.db.falkordb_client import kv
from src.domain.access import approvals
from src.domain.access.roles import ROLE_OWNER, role_at_least, role_of, set_role
from src.domain.knowledge import ask_service
from src.domain.memory import memory_service
from src.domain.memory.read_scope import resolve_brain_read_container_tags
from src.domain.memory.writeback import (
    SHARED_TEAM_BRAIN_CONTAINER_TAG,
    SlackMemoryScope,
    scope_label,
    slack_memory_container_tag,
    slack_memory_scope_for_turn,
)
from src.domain.slack import channel_membership, format
from src.domain.slack.workspace import (
    SlackWorkspace,
    format_slack_org_member_denial,
    format_slack_profile_lookup_failure,
    get_workspace_by_team_id,
    resolve_actor,
)
from src.integrations.slack.bot_address import clean_mention
from src.integrations.slack.client import (
    SlackApprovalCard,
    resolved_approval_blocks,
    update_interaction_response,
)
from src.integrations.slack.events import (
    SlackEventInner,
    is_direct_conversation_event,
    mentioned_user_ids,
    should_ignore_message,
)
from src.integrations.slack.routing import resolve_slack_reply_target

log = logging.getLogger(__name__)

SLACK_ACK_REACTION = "eyes"
SLACK_COMPLETED_REPLY_REACTION = "white_check_mark"
TICKET_TOOL_NAME = "Create ticket"

_REMEMBER = re.compile(r"^(?:remember|note)\s*[:\-]\s*(.+)$", re.IGNORECASE | re.DOTALL)
_TICKET = re.compile(r"^(?:ticket|file ticket|new ticket)\s*[:\-]\s*(.+)$", re.IGNORECASE | re.DOTALL)
_SCOPES = re.compile(r"^(?:what can you see|scopes?|what do you know about me)\??$", re.IGNORECASE)
_PROMOTE = re.compile(r"^(promote|demote)\b", re.IGNORECASE)
_FORGET = re.compile(r"^forget (?:this )?(?:channel|conversation|dm)\b", re.IGNORECASE)
_NAME_PREFIX = re.compile(r"^(?:(?:hey|hi|hello|yo)(?:\s+there)?[\s,!:;-]+)?@?company[\s-]brain[\s,:;!?.-]*", re.IGNORECASE)


# --- bot threads ----------------------------------------------------------------------


def _bot_thread_key(channel: str, thread_ts: str) -> str:
    return f"brain:bot-thread:{channel}:{thread_ts}"


def remember_bot_thread(channel: str, thread_ts: str) -> None:
    kv().set(_bot_thread_key(channel, thread_ts), "1", ex=settings.bot_thread_ttl_seconds)


def is_bot_thread(channel: str | None, thread_ts: str | None) -> bool:
    if not channel or not thread_ts:
        return False
    return bool(kv().exists(_bot_thread_key(channel, thread_ts)))


# --- turn -----------------------------------------------------------------------------


def run_slack_turn(workspace: SlackWorkspace, ev: SlackEventInner, addressed_by_name: bool = False) -> None:
    if should_ignore_message(ev) or not ev.user or ev.user == workspace.bot_user_id:
        return
    is_dm = is_direct_conversation_event(ev)
    explicitly_addressed = ev.type == "app_mention" or addressed_by_name
    if not is_dm and not explicitly_addressed and ev.thread_ts and not is_bot_thread(ev.channel, ev.thread_ts):
        # A reply in someone else's thread is context, not a summons.
        run_slack_context_event(workspace, ev)
        return

    client = workspace.client
    target = resolve_slack_reply_target(client, ev)
    if target is None:
        return

    actor = resolve_actor(workspace, ev.user)
    if actor is None:
        client.post_ephemeral(target.channel, ev.user, format_slack_profile_lookup_failure(), target.thread_ts)
        return
    if not actor.is_member:
        client.post_ephemeral(target.channel, ev.user, format_slack_org_member_denial(), target.thread_ts)
        return

    scope = _memory_scope(workspace, ev, is_dm)
    if ev.channel and ev.ts:
        client.add_reaction(ev.channel, ev.ts, SLACK_ACK_REACTION)

    text = clean_mention(ev.text)
    if addressed_by_name:
        text = _NAME_PREFIX.sub("", text).strip()
    try:
        reply = _handle(workspace, ev, scope, text, target.channel, target.thread_ts)
    except Exception:
        log.exception("[slack] turn failed channel=%s ts=%s", ev.channel, ev.ts)
        reply = "Something went wrong on my side while reading the graph. Try again in a moment."

    if reply:
        client.post_message(target.channel, reply, target.thread_ts)
    remember_bot_thread(target.channel, target.thread_ts)
    if ev.channel and ev.ts:
        client.swap_reaction(ev.channel, ev.ts, SLACK_ACK_REACTION, SLACK_COMPLETED_REPLY_REACTION)


def _memory_scope(workspace: SlackWorkspace, ev: SlackEventInner, is_dm: bool) -> SlackMemoryScope:
    channel = ev.channel or ""
    info = None
    if not is_dm and channel:
        info = workspace.client.conversation_info(channel)
        if info is not None and info.name:
            channel_membership.remember_channel_name(channel, info.name, bool(info.is_private))
    scope = slack_memory_scope_for_turn(
        is_dm=is_dm,
        channel=channel,
        channel_type=ev.channel_type,
        user_id=ev.user,
        slack_user_id=ev.user,
        conversation_info=info,
    )
    if scope.kind == "private_channel" and ev.user:
        # Speaking in a private channel proves membership; events can be dropped.
        channel_membership.record_membership(channel, ev.user)
    return scope


def _handle(workspace: SlackWorkspace, ev: SlackEventInner, scope: SlackMemoryScope, text: str, channel: str, thread_ts: str) -> str | None:
    asker = ev.user or ""
    read_tags = resolve_brain_read_container_tags(scope)
    write_tag = slack_memory_container_tag(scope)
    names = _channel_names(read_tags + ([write_tag] if write_tag else []))

    if not text or text.lower() in ("help", "?"):
        return format.HELP_TEXT
    if _SCOPES.match(text):
        return format.format_scopes(read_tags, write_tag, names)

    if match := _REMEMBER.match(text):
        if not write_tag:
            return "I can't save anything from here."
        memory_service.write(match.group(1).strip(), write_tag, asker, scope.channel_id or "", ev.ts or "")
        return f"Saved. Anyone who can read {scope_label(write_tag, names)} can recall it."

    if match := _TICKET.match(text):
        return _request_ticket(workspace, ev, scope, match.group(1).strip(), channel, thread_ts, names)

    if _PROMOTE.match(text):
        # Mentions are stripped from `text`; read targets from the raw message.
        return _change_role(asker, text, ev.text or "", workspace.bot_user_id)

    if _FORGET.match(text):
        return _forget(asker, scope, write_tag, names)

    result = ask_service.ask(text, read_tags)
    memories = memory_service.search(text, read_tags, limit=3)
    return format.format_answer(result, memories, read_tags, names)


def _channel_names(tags: list[str]) -> dict[str, str]:
    ids = [tag.removeprefix("slack_channel_") for tag in tags if tag.startswith("slack_channel_")]
    return channel_membership.channel_names(ids)


# --- commands ------------------------------------------------------------------------


def _request_ticket(workspace, ev, scope, title, channel, thread_ts, names) -> str | None:
    write_tag = slack_memory_container_tag(scope) or SHARED_TEAM_BRAIN_CONTAINER_TAG
    summary = f"Create a ticket titled *{title}* in the company graph and link it to the service it's about. Visible to {scope_label(write_tag, names)}."
    approval = approvals.insert_pending_approval(
        team_id=workspace.team_id,
        channel=channel,
        thread_ts=thread_ts,
        asker_user=ev.user or "",
        tool_name=TICKET_TOOL_NAME,
        tool_input={"title": title},
        summary=summary,
        container_tag=write_tag,
    )
    card_ts = workspace.client.post_approval_card(channel, thread_ts, _card(approval))
    approvals.set_approval_card_ts(approval.approval_id, card_ts)
    return None


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


# --- context retention ---------------------------------------------------------------


def run_slack_context_event(workspace: SlackWorkspace, ev: SlackEventInner) -> None:
    """Keep a channel message as memory in its room's container. Port of ``onSlackContextEvent``."""
    if should_ignore_message(ev) or not ev.user or not ev.channel or ev.user == workspace.bot_user_id:
        return
    if is_direct_conversation_event(ev):
        return
    text = clean_mention(ev.text)
    if not memory_service.worth_keeping(text):
        return
    scope = _memory_scope(workspace, ev, is_dm=False)
    write_tag = slack_memory_container_tag(scope)
    if not write_tag:
        return
    resolve_actor(workspace, ev.user)
    memory_service.write(text, write_tag, ev.user, ev.channel, ev.ts or "")


# --- approvals -----------------------------------------------------------------------


def _card(approval: approvals.PendingApproval) -> SlackApprovalCard:
    return SlackApprovalCard(
        approval_id=approval.approval_id,
        summary=approval.summary,
        tool_name=approval.tool_name,
        asker_user=approval.asker_user,
        expires_at=approval.expires_at / 1000,
    )


def run_slack_approval_decision(team_id: str, approval_id: str, approved: bool, user_id: str, response_url: str | None) -> None:
    def ephemeral(text: str) -> None:
        update_interaction_response(response_url or "", text, response_type="ephemeral")

    def resolve(status: str, text: str) -> None:
        update_interaction_response(response_url or "", text, blocks=resolved_approval_blocks(_card(approval), status), replace_original=True)

    approval = approvals.load_approval(approval_id)
    if approval is None:
        ephemeral("I couldn't find that approval request. It may have already expired.")
        return
    if approval.team_id != team_id:
        ephemeral("That approval belongs to a different Slack workspace.")
        return
    if approval.asker_user != user_id:
        ephemeral(f"Only <@{approval.asker_user}> can approve or deny this action.")
        return
    if approval.status != "pending":
        ephemeral(f"That approval is already {approval.status}.")
        return
    if approvals.approval_is_expired(approval):
        approvals.mark_approval_terminal(approval_id, "expired")
        resolve("Expired", "This approval expired after 15 minutes.")
        return

    workspace = get_workspace_by_team_id(team_id)
    if workspace is None:
        ephemeral("I couldn't resume that action. The workspace configuration is missing.")
        return
    actor = resolve_actor(workspace, user_id)
    if actor is None:
        ephemeral(format_slack_profile_lookup_failure())
        return
    if not actor.is_member:
        ephemeral(format_slack_org_member_denial())
        return

    if not approvals.mark_approval_decided(approval_id, "approved" if approved else "denied", user_id):
        ephemeral("That approval was already decided.")
        return
    if not approved:
        resolve("Denied", "Denied. Nothing was changed.")
        return

    resolve("Approved", "Approved.")
    try:
        result = _execute(approval)
    except Exception:
        log.exception("[slack] approved action failed approval=%s", approval_id)
        approvals.mark_approval_terminal(approval_id, "error", from_status="approved")
        workspace.client.post_message(approval.channel, "That action failed. Nothing was written.", approval.thread_ts)
        return
    approvals.mark_approval_terminal(approval_id, "executed", from_status="approved")
    workspace.client.post_message(approval.channel, result, approval.thread_ts)


def _execute(approval: approvals.PendingApproval) -> str:
    if approval.tool_name != TICKET_TOOL_NAME:
        raise ValueError(f"unknown approved tool {approval.tool_name}")
    title = approval.tool_input["title"]
    created = ask_service.ingest_ticket(title, "", approval.container_tag)
    read_tags = list(dict.fromkeys([SHARED_TEAM_BRAIN_CONTAINER_TAG, approval.container_tag]))
    follow = ask_service.who_knows(created["about_name"], read_tags)
    result = {
        "tool": "ingest",
        "answer": f"Filed *{created['id']}* about {created['about_name']}. {follow['answer']}",
        "path": created["path"] + follow["path"],
    }
    return format.format_answer(result, [], read_tags, _channel_names(read_tags))
