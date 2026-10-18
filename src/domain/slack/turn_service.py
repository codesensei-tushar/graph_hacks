"""Port of company-brain `brain/slack/turn.ts`.

One Slack turn: someone DMs the bot, @mentions it, names it, or replies in a thread.

``run_slack_turn`` follows ``runSlackTurnInner``:

1. Record the event in the local event store; bots and self are context only.
2. A thread reply that doesn't address the bot (and isn't steering a live turn) goes
   to ``run_slack_passive_thread``: proactivity and mute checks, then triage decides
   ANSWER / ACK / INVESTIGATE / PASS (PASS when no model is configured).
3. Verify the asker. Guests get a denial; unmapped members get the account-link prompt.
4. Steering: a message in a thread with a live turn is classified (stop, or the
   active-turn gate's IGNORE / APPEND / REPLACE) and either queued into the turn's
   inbox or restarts it.
5. Ack reaction, public progress stream, memory scope and read surface.
6. ``compute.compute_turn`` produces the reply (the agent seam). A consequential write
   suspends for an Approve/Deny card; otherwise the reply is delivered, the thread is
   remembered, and any memory writeback is saved.

Also here: ``run_slack_approval_decision``, ``run_slack_debug_reaction``,
``run_slack_mute_reaction``, ``finalize_interrupted_slack_turn`` and the context
retention path. Dropped from upstream: entitlement/billing, MCP connect continuation
and connect buttons, leases, telemetry, fibers (Slack retries plus the event store
cover redelivery), post-turn reflect (an agent feature).
"""

import dataclasses
import logging
import uuid

from src.domain.access import approvals
from src.domain.memory import memory_service
from src.domain.memory.read_scope import resolve_brain_read_container_tags
from src.domain.memory.writeback import (
    SlackMemoryScope,
    slack_memory_container_tag,
    slack_memory_scope_for_turn,
    slack_memory_writer_user_id,
)
from src.domain.slack import channel_audience
from src.domain.slack import channel_membership
from src.domain.slack.compute import TurnRequest, compute_turn, execute_approved_action
from src.domain.slack.turn import event_store, turn_control, turn_inbox
from src.domain.slack.turn.active_turn_gate import (
    ActiveTurnGateInput,
    apply_active_turn_policy,
    triage_active_turn_message,
)
from src.domain.slack.turn.approval_expiry import approval_card, arm_approval_expiry
from src.domain.slack.turn.approval_requests import cancel_superseded_approval
from src.domain.slack.turn.bot_threads import (
    MUTE_OFFER_FOOTER,
    clear_thread_muted,
    get_thread_muted_by,
    has_bot_thread,
    is_thread_muted,
    mark_bot_thread,
    muted_footer,
    set_thread_muted,
)
from src.domain.slack.turn.chime import is_emoji_only_slack_text, triage_profile_user_ids
from src.domain.slack.turn.chime_budget import (
    can_use_answer_fallback,
    release_passive_investigation,
    reserve_chime_answer,
    reserve_passive_investigation,
)
from src.domain.slack.turn.proactivity import (
    PROACTIVITY_FILTER_REASON,
    get_brain_proactivity,
    resolve_channel_proactivity,
)
from src.domain.slack.turn.reaction_queue import enqueue_passive_reaction
from src.domain.slack.turn.triage import (
    TriageAddressedTarget,
    TriageChannel,
    TriageObservabilityContext,
    TriageSpeaker,
    triage_chime_message,
)
from src.domain.slack.turn.triage_sampling import reserve_triage_trace_sample
from src.domain.slack.workspace import (
    SlackWorkspace,
    format_slack_org_member_denial,
    format_slack_profile_lookup_failure,
    get_home_channel,
    get_workspace_by_team_id,
    resolve_actor,
    should_post_slack_org_member_denial,
)
from src.integrations.llm.model import is_model_available
from src.integrations.slack.bot_address import clean_mention, collect_bot_address_aliases, is_bot_addressed_by_name
from src.integrations.slack.client import resolved_approval_blocks, update_interaction_response
from src.integrations.slack.debug_trace_delivery import deliver_slack_debug_trace
from src.integrations.slack.events import (
    BRAIN_MUTE_REACTION,
    SlackEventInner,
    is_assistant_thread_message,
    is_bot_mentioned,
    is_direct_conversation_event,
    is_direct_message,
    is_slack_content_message_subtype,
    mentioned_user_ids,
    should_ignore_message,
)
from src.integrations.slack.message_trace import is_our_slack_bot_message, lookup_message_trace_records, record_message_trace
from src.integrations.slack.routing import SlackReplyTarget, resolve_slack_reply_target
from src.integrations.slack.stream import CONTEXT_FOOTER_BLOCK_ID, SlackStreamSession

log = logging.getLogger(__name__)

THREAD_CONTEXT_PAGE_SIZE = 200
THREAD_CONTEXT_MAX_MESSAGES = 600
THREAD_CONTEXT_MAX_PAGES = 4
THREAD_TRIAGE_CONTEXT_LIMIT = 50
SLACK_ACK_REACTION = "ack"
SLACK_ACK_FALLBACK_REACTION = "white_check_mark"
SLACK_COMPLETED_REPLY_REACTION = "brain"
THREAD_MUTED_FILTER_REASON = "The thread is muted for proactive replies."

# --- small helpers ------------------------------------------------------------------------


def _new_trace_id() -> str:
    return uuid.uuid4().hex


def _user_lookup(workspace: SlackWorkspace, user_id: str):
    return workspace.client.lookup_slack_user_info(user_id)


def _asker_name(info) -> str | None:
    if info is None:
        return None
    return info.real_name or info.display_name or info.name


def _bot_aliases(workspace: SlackWorkspace) -> list[str]:
    return collect_bot_address_aliases(workspace.bot_name, None, workspace.bot_name)


def _reply_target(workspace: SlackWorkspace, ev: SlackEventInner, force_full_turn: bool, force_thread_ts: str | None):
    if force_full_turn:
        thread_ts = force_thread_ts or ev.ts
        return SlackReplyTarget(channel=ev.channel, thread_ts=thread_ts) if ev.channel and thread_ts else None
    return resolve_slack_reply_target(workspace.client, ev)


def _memory_scope(workspace: SlackWorkspace, ev: SlackEventInner, is_dm: bool, channel: str) -> tuple[SlackMemoryScope, object]:
    info = None
    if not is_dm and channel:
        info = workspace.client.get_cached_slack_conversation_info(workspace.team_id, channel)
        if info is not None and getattr(info, "name", None):
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
        # Speaking in a private channel proves membership; membership events can be dropped. The channel is
        # marked private only on evidence (the event or conversations.info), not on the fail-closed default.
        known_private = ev.channel_type in ("group", "mpim") or (info is not None and info.is_private is True)
        channel_membership.record_membership(channel, ev.user, is_private=True if known_private else None)
    internal = channel_audience.is_internal_audience(workspace.client, workspace.team_id, channel, info, is_dm=is_dm, asker=ev.user)
    if not internal:
        scope = dataclasses.replace(scope, internal_audience=False)
    return scope, info


class _AckReaction:
    """``ensureAckReaction`` / ``finishAckReaction``: custom ``ack`` with a stock fallback."""

    def __init__(self, workspace: SlackWorkspace, channel: str, message_ts: str, enabled: bool):
        self.client = workspace.client
        self.channel = channel
        self.message_ts = message_ts
        self.enabled = enabled and bool(message_ts)
        self.name: str | None = None
        self._added = False

    def add(self) -> None:
        if not self.enabled or self._added:
            return
        self._added = True
        if self.client.add_slack_reaction(self.channel, self.message_ts, SLACK_ACK_REACTION, quiet_errors=["invalid_name"]):
            self.name = SLACK_ACK_REACTION
        elif self.client.add_slack_reaction(self.channel, self.message_ts, SLACK_ACK_FALLBACK_REACTION):
            self.name = SLACK_ACK_FALLBACK_REACTION

    def remove(self) -> None:
        if self.enabled and self.name:
            self.client.remove_slack_reaction(self.channel, self.message_ts, self.name)

    def finish(self, current: bool) -> None:
        if not self.enabled:
            return
        if not current:
            self.remove()
            return
        if self.name:
            self.client.remove_slack_reaction(self.channel, self.message_ts, self.name)
        if not self.client.add_slack_reaction(
            self.channel, self.message_ts, SLACK_COMPLETED_REPLY_REACTION, quiet_errors=["invalid_name"]
        ) and self.name != SLACK_ACK_FALLBACK_REACTION:
            # No custom :brain: emoji in this workspace; keep a stock "done" mark.
            self.client.add_slack_reaction(self.channel, self.message_ts, SLACK_ACK_FALLBACK_REACTION)


def update_mute_footer(workspace: SlackWorkspace, channel: str, thread_ts: str, message_ts: str, footer_text: str) -> bool:
    message = workspace.client.get_slack_message_blocks(channel, thread_ts, message_ts)
    if not message:
        return False
    footer_block = {"type": "context", "block_id": CONTEXT_FOOTER_BLOCK_ID, "elements": [{"type": "mrkdwn", "text": footer_text}]}
    found = False
    blocks = []
    for block in message.get("blocks") or []:
        if isinstance(block, dict) and block.get("block_id") == CONTEXT_FOOTER_BLOCK_ID:
            found = True
            blocks.append(footer_block)
        else:
            blocks.append(block)
    if not found:
        blocks = (
            [*blocks, footer_block]
            if blocks
            else [{"type": "section", "text": {"type": "mrkdwn", "text": message.get("text") or ""}}, footer_block]
        )
    return workspace.client.update_slack_message(channel, message_ts, message.get("text") or "", blocks)


# --- entry points -------------------------------------------------------------------------


def run_turn(
    workspace: SlackWorkspace,
    event: SlackEventInner,
    *,
    force_full_turn: bool = False,
    force_thread_ts: str | None = None,
    trace_id: str | None = None,
    triage_result: str | None = None,
    agent_main_effort: str | None = None,
    passive_investigation: dict | None = None,
) -> None:
    """The ``runSlackTurn`` signature chime and passive threads call."""
    run_slack_turn(
        workspace,
        event,
        force_full_turn=force_full_turn,
        force_thread_ts=force_thread_ts,
        trace_id=trace_id,
        triage_result=triage_result,
        agent_main_effort=agent_main_effort,
        passive_investigation=passive_investigation,
        skip_reactions=passive_investigation is not None,
    )


def run_slack_turn(
    workspace: SlackWorkspace,
    ev: SlackEventInner,
    addressed_by_name: bool = False,
    *,
    force_full_turn: bool = False,
    force_thread_ts: str | None = None,
    trace_id: str | None = None,
    triage_result: str | None = None,
    agent_main_effort: str | None = None,
    passive_investigation: dict | None = None,
    skip_reactions: bool = False,
) -> None:
    claim: dict = {}
    try:
        _run_slack_turn_inner(
            workspace,
            ev,
            claim,
            addressed_by_name=addressed_by_name,
            force_full_turn=force_full_turn,
            force_thread_ts=force_thread_ts,
            trace_id=trace_id,
            triage_result=triage_result,
            agent_main_effort=agent_main_effort,
            passive_investigation=passive_investigation,
            skip_reactions=skip_reactions,
        )
    finally:
        turn_control.reap_unfinished_thread_turn(claim.get("control"))


def _run_slack_turn_inner(
    workspace: SlackWorkspace,
    ev: SlackEventInner,
    claim: dict,
    *,
    addressed_by_name: bool,
    force_full_turn: bool,
    force_thread_ts: str | None,
    trace_id: str | None,
    triage_result: str | None,
    agent_main_effort: str | None,
    passive_investigation: dict | None,
    skip_reactions: bool,
) -> None:
    team_id = workspace.team_id
    client = workspace.client
    identity = event_store.record_slack_event(team_id, ev)
    if ev.bot_id or ev.app_id or (workspace.bot_user_id and ev.user == workspace.bot_user_id):
        if identity:
            event_store.mark_stored_event_filtered(identity, "Bot-authored messages are retained as context and never triaged.")
        return
    if not ev.user:
        return
    is_mention = ev.type == "app_mention" or is_bot_mentioned(ev.text, workspace.bot_user_id)
    is_dm = is_direct_message(ev) or is_direct_conversation_event(ev)
    is_assistant_thread = is_assistant_thread_message(ev)

    question = clean_mention(ev.text)
    if not question:
        if identity:
            event_store.mark_stored_event_filtered(identity, "The Slack message has no triageable text.")
        return
    is_name_addressed = addressed_by_name or is_bot_addressed_by_name(ev.text, _bot_aliases(workspace))
    explicit = force_full_turn or is_mention or is_dm or is_assistant_thread or is_name_addressed
    if identity and explicit:
        audit = event_store.get_stored_slack_event_audit(identity)
        if not (audit and audit.decision):
            event_store.record_stored_triage_decision(
                identity,
                decision="explicit",
                source="explicit_bypass",
                reason="Explicit Slack conversation bypasses unsolicited proactivity budgets.",
            )

    pending_thread_turn = (
        turn_control.get_thread_turn(turn_control.slack_thread_turn_key(team_id, ev.channel, ev.thread_ts))
        if ev.channel and ev.thread_ts
        else None
    )
    if (
        not force_full_turn
        and identity
        and ev.thread_ts
        and not is_mention
        and not is_dm
        and not is_assistant_thread
        and not is_name_addressed
        and (pending_thread_turn is None or pending_thread_turn.status != "finalizing")
        and not turn_control.is_steerable_thread_turn(pending_thread_turn)
    ):
        channel_info = client.get_cached_slack_conversation_info(team_id, ev.channel or "")
        proactivity = resolve_channel_proactivity(
            settings=get_brain_proactivity(),
            channel_id=ev.channel,
            home_channel_id=(get_home_channel(team_id) or {}).get("channel_id"),
            channel_name=getattr(channel_info, "name", None),
        )
        if proactivity == "quiet":
            event_store.mark_stored_event_filtered(identity, PROACTIVITY_FILTER_REASON)
            return
        if is_thread_muted(team_id, ev.channel or "", ev.thread_ts):
            event_store.mark_stored_event_filtered(identity, THREAD_MUTED_FILTER_REASON)
            return
        retain_without_model(workspace, ev)
        run_slack_passive_thread(workspace, ev, identity=identity, question=question)
        return

    if not explicit and not ev.thread_ts:
        return

    target = _reply_target(workspace, ev, force_full_turn, force_thread_ts)
    if target is None:
        log.warning("[company-brain] no reply target team=%s event=%s channel=%s", team_id, ev.type, ev.channel)
        return
    channel, thread_ts = target.channel, target.thread_ts
    denial_allowed = should_post_slack_org_member_denial(
        force_full_turn=force_full_turn,
        is_mention=is_mention,
        is_name_addressed=is_name_addressed,
        is_dm=is_dm,
        is_assistant_thread=is_assistant_thread,
    )

    asker_lookup = _user_lookup(workspace, ev.user)
    if not asker_lookup.ok:
        log.warning("[company-brain] turn verification failed slackUser=%s reason=%s", ev.user, asker_lookup.reason)
        if denial_allowed:
            client.post_slack_message(channel, format_slack_profile_lookup_failure(), thread_ts)
        return
    asker = asker_lookup.user
    actor = resolve_actor(workspace, ev.user)
    if actor is None or not actor.is_member or not actor.person_id:
        log.warning("[company-brain] turn denied for non-member slackUser=%s lookup=%s", ev.user, actor.lookup if actor else "-")
        if denial_allowed:
            _deny_non_member(workspace, ev, asker, actor, channel, thread_ts, is_dm)
        return

    trace_id = trace_id or _new_trace_id()
    thread_key = turn_control.slack_thread_turn_key(team_id, channel, thread_ts)
    active_turn = turn_control.get_thread_turn(thread_key)
    effective_question = question
    turn_steering: str | None = None
    while True:
        if active_turn is not None and active_turn.status == "finalizing":
            active_turn = turn_control.wait_for_thread_turn_finalization(active_turn)
            continue
        start_expectation = turn_control.thread_turn_start_expectation(active_turn)
        if not turn_control.is_steerable_thread_turn(active_turn):
            break

        author_owns_turn = ev.user == active_turn.asker_user
        if author_owns_turn and turn_control.classify_turn_steering(question) == "stop":
            interrupted = turn_control.interrupt_thread_turn(thread_key, "cancelled", question)
            if not interrupted.notified:
                client.post_slack_message(channel, "Got it, stopping here.", thread_ts)
            return

        message_ts = ev.ts or ""
        reservation = turn_inbox.reserve_turn_update(
            thread_key=thread_key,
            turn_id=active_turn.turn_id,
            revision=active_turn.revision,
            message_ts=message_ts,
            author_user=ev.user,
            author_name=_asker_name(asker),
            instruction=question,
        )
        if reservation == "duplicate":
            return
        if reservation == "inactive":
            active_turn = turn_control.get_thread_turn(thread_key)
            continue

        current_instructions = [
            *([active_turn.latest_instruction] if active_turn.latest_instruction else []),
            *(
                f"{update.author_name or update.author_user}: {update.instruction}"
                for update in turn_inbox.list_current_turn_updates(
                    thread_key=thread_key, turn_id=active_turn.turn_id, revision=active_turn.revision
                )
            ),
        ]
        gate = triage_active_turn_message(
            ActiveTurnGateInput(
                active_question=active_turn.original_question,
                current_instructions=current_instructions,
                message=question,
                author_name=_asker_name(asker),
                author_owns_turn=author_owns_turn,
            )
        )
        latest = turn_control.get_thread_turn(thread_key)
        if (
            not turn_control.is_steerable_thread_turn(latest)
            or latest.turn_id != active_turn.turn_id
            or latest.revision != active_turn.revision
        ):
            turn_inbox.discard_turn_update_reservation(
                thread_key=thread_key, turn_id=active_turn.turn_id, revision=active_turn.revision, message_ts=message_ts
            )
            active_turn = latest
            continue
        if gate.outcome == "ignore":
            turn_inbox.resolve_turn_update(thread_key=thread_key, message_ts=message_ts, outcome=gate.outcome, status="ignored")
            return
        action = apply_active_turn_policy(
            outcome=gate.outcome,
            author_owns_turn=author_owns_turn,
            turn_status="waiting_approval" if active_turn.status == "waiting_approval" else "running",
        )
        if action == "queue":
            turn_inbox.resolve_turn_update(thread_key=thread_key, message_ts=message_ts, outcome=gate.outcome, status="pending")
            return

        turn_inbox.resolve_turn_update(thread_key=thread_key, message_ts=message_ts, outcome=gate.outcome, status="ignored")
        interrupted = turn_control.interrupt_thread_turn(thread_key, "superseded", question)
        if interrupted.row is None:
            return
        start_expectation = turn_control.ThreadTurnStartExpectation(
            turn_id=interrupted.row.turn_id, revision=interrupted.row.revision, status="superseded"
        )
        effective_question = interrupted.row.original_question.strip() or question
        turn_steering = question
        break

    thread_root_ts = ev.thread_ts or (force_thread_ts if force_full_turn and force_thread_ts else ev.ts)
    thread: list[dict] = []
    if thread_root_ts:
        history = client.get_slack_thread_history(
            channel,
            thread_root_ts,
            page_limit=THREAD_CONTEXT_PAGE_SIZE,
            max_messages=THREAD_CONTEXT_MAX_MESSAGES,
            max_pages=THREAD_CONTEXT_MAX_PAGES,
        )
        thread = list(getattr(history, "messages", None) or [])
    known_bot_thread = has_bot_thread(team_id, channel, thread_ts)
    bot_in_thread = known_bot_thread or any(is_our_slack_bot_message(m, workspace.bot_user_id) for m in thread)
    in_channel_thread = bool(ev.thread_ts)
    if (
        not force_full_turn
        and not turn_steering
        and not is_mention
        and not is_name_addressed
        and not is_dm
        and not is_assistant_thread
        and not in_channel_thread
    ):
        return

    ack = _AckReaction(workspace, channel, ev.ts or "", enabled=not skip_reactions)
    if not passive_investigation:
        ack.add()

    proactive_turn = bool(passive_investigation) or triage_result in ("answer", "investigate")
    mute_offer = proactive_turn and not bot_in_thread
    stream = SlackStreamSession(
        client,
        channel=channel,
        thread_ts=thread_ts,
        team_id=team_id,
        recipient_user_id=ev.user,
        public_progress=True,
        clear_assistant_status_on_progress=not passive_investigation,
        context_footer=(
            (lambda: muted_footer(get_thread_muted_by(team_id, channel, thread_ts)) if get_thread_muted_by(team_id, channel, thread_ts) else MUTE_OFFER_FOOTER)
            if mute_offer
            else None
        ),
    )
    if not passive_investigation:
        client.set_assistant_thread_status(channel, thread_ts, "Dreaming", ["Dreaming"])

    memory_scope, _info = _memory_scope(workspace, ev, is_dm, channel)
    read_tags = resolve_brain_read_container_tags(memory_scope)

    control = turn_control.begin_thread_turn(
        thread_key=thread_key,
        asker_user=ev.user,
        original_question=effective_question,
        latest_instruction=turn_steering,
        expected=start_expectation,
    )
    if control is None:
        log.info("[company-brain] stale turn start team=%s channel=%s thread=%s", team_id, channel, thread_ts)
        if not passive_investigation:
            client.clear_assistant_thread_status(channel, thread_ts)
            ack.remove()
        return
    claim["control"] = control
    if not passive_investigation:
        turn_control.attach_turn_cancel_notifier(
            control, lambda status: stream.discard("Got it, stopping here." if status == "cancelled" else None)
        )

    request = TurnRequest(
        workspace=workspace,
        event=ev,
        question=effective_question,
        raw_text=ev.text or "",
        asker_slack_user_id=ev.user,
        asker_name=_asker_name(asker),
        memory_scope=memory_scope,
        read_tags=read_tags,
        thread=thread,
        channel=channel,
        thread_ts=thread_ts,
        is_dm=is_dm,
        progress=None if passive_investigation else turn_control.fenced_progress(control, stream.progress),
        trace_id=trace_id,
        turn_steering=turn_steering,
        passive_investigation=passive_investigation,
        agent_main_effort=agent_main_effort,
    )

    reply = ""
    memory = None
    failed = False
    after_reply = None
    try:
        outcome = None
        for _ in range(3):
            request.live_updates += [update.instruction for update in turn_inbox.consume_pending_turn_updates(control)]
            outcome = compute_turn(request)
            if outcome.status == "suspended":
                break
            finalization = turn_control.claim_thread_turn_finalization_if_inbox_empty(control)
            if finalization != "updates_pending":
                break
        if outcome.status == "suspended" and outcome.approval is not None:
            _suspend_for_approval(workspace, ev, control, stream, outcome, channel, thread_ts, passive_investigation)
            return
        reply = outcome.reply
        memory = None if passive_investigation else outcome.memory
        after_reply = outcome.after_reply
    except Exception:
        log.exception("[company-brain] turn failed channel=%s ts=%s", channel, ev.ts)
        if not turn_control.is_thread_turn_current(control):
            return
        failed = True
        reply = "" if passive_investigation else "Sorry — I hit an error while working on that. Try again in a moment."
    finally:
        current = turn_control.is_thread_turn_current(control)
        if passive_investigation and current and reply.strip() and passive_investigation.get("budget_finding") is not False:
            identity_row = passive_investigation.get("identity")
            priority = passive_investigation.get("priority") or "normal"
            budget = reserve_chime_answer(
                channel_id=channel,
                priority=priority,
                last_channel_activity_at=event_store.get_previous_human_channel_activity_at(identity_row) if identity_row else None,
                urgent_investigation_finding=priority == "urgent",
            )
            if not budget.allowed:
                if identity_row:
                    event_store.record_stored_suppression(identity_row, suppression=budget.suppression)
                reply = ""
            elif identity_row:
                event_store.record_stored_action_outcome(identity_row, "investigation_finding_budget_reserved")
        if proactive_turn and reply.strip() and is_thread_muted(team_id, channel, thread_ts):
            reply = ""

        finalize_result = stream.finalize(reply, failed) if current else None
        reply_ts = finalize_result.message_ts if finalize_result else None
        if current and reply.strip() and not (finalize_result and finalize_result.streamed):
            reply_ts = stream.post_fallback(reply) or reply_ts
        if reply_ts:
            record_message_trace(channel, reply_ts, trace_id, thread_ts)
            if mute_offer:
                client.add_slack_reaction(channel, reply_ts, BRAIN_MUTE_REACTION)
        if current and (reply_ts or reply.strip()):
            mark_bot_thread(team_id, channel, thread_ts)
        if passive_investigation:
            spoke = current and bool(reply.strip())
            identity_row = passive_investigation.get("identity")
            if identity_row:
                event_store.record_stored_action_outcome(identity_row, "investigation_spoke" if spoke else "investigation_silent")
            if passive_investigation.get("claim"):
                release_passive_investigation(passive_investigation["claim"], "completed" if spoke else ("failed" if failed else "silent"))
        else:
            ack.finish(current)

    if not turn_control.is_thread_turn_current(control):
        return
    try:
        writer = slack_memory_writer_user_id(memory_scope, ev.user)
        if not passive_investigation and memory and writer and turn_control.is_thread_turn_current(control):
            memory_service.write_memories(memory, memory_scope, author_id=ev.user)
    finally:
        turn_control.mark_thread_turn_completed(control)
    if after_reply is not None:
        try:
            after_reply()
        except Exception:
            log.exception("[company-brain] post-reply action failed")


def _deny_non_member(workspace, ev, asker, actor, channel: str, thread_ts: str, is_dm: bool) -> None:
    client = workspace.client
    is_guest = actor is not None and not actor.is_member
    if not is_guest:
        from src.domain.slack.account_link import post_slack_account_link_prompt

        try:
            if post_slack_account_link_prompt(
                web=client.web,
                team_id=workspace.team_id,
                slack_user_id=ev.user,
                org_name=workspace.team_name or "this workspace",
                channel=channel,
                is_dm=is_dm,
                thread_ts=thread_ts,
                slack_email=getattr(asker, "email", None),
                slack_display_name=_asker_name(asker),
            ):
                return
        except Exception:
            log.warning("[company-brain] account link prompt failed slackUser=%s", ev.user, exc_info=True)
    denial = format_slack_org_member_denial(person_name=_asker_name(asker), org_name=workspace.team_name)
    if is_dm:
        client.post_slack_message(channel, denial, thread_ts)
    else:
        client.post_slack_ephemeral(channel, ev.user, denial, thread_ts)


def _suspend_for_approval(workspace, ev, control, stream, outcome, channel: str, thread_ts: str, passive_investigation) -> None:
    client = workspace.client
    if passive_investigation:
        turn_control.mark_thread_turn_completed(control)
        return
    if not turn_control.mark_thread_turn_waiting_for_approval(control):
        return
    request = outcome.approval
    pending = approvals.insert_pending_approval(
        team_id=workspace.team_id,
        channel=channel,
        thread_ts=thread_ts,
        asker_user=ev.user or "",
        tool_name=request.tool_name,
        tool_input=request.tool_input,
        summary=request.summary,
        container_tag=request.container_tag,
        extra={
            "turn_control": {"thread_key": control.thread_key, "turn_id": control.turn_id, "revision": control.revision},
            # Fixed at request time, like the container: whether the room may read org_internal.
            "internal_audience": channel_audience.is_internal_audience(
                workspace.client, workspace.team_id, channel, is_dm=is_direct_message(ev), asker=ev.user
            ),
        },
    )
    if not turn_control.is_thread_turn_current(control):
        return
    stream.finalize("", False)
    if not turn_control.is_thread_turn_current(control):
        return
    card_ts = client.post_slack_approval_card(channel, thread_ts, approval_card(pending))
    if not card_ts:
        # No buttons reached Slack: don't leave a pending row the asker can never act on.
        approvals.mark_approval_terminal(pending.approval_id, "error")
        stream.post_fallback(
            "I need your sign-off to run that action, but I couldn't post the Approve/Deny buttons just now. Mind asking again?"
        )
        turn_control.mark_thread_turn_completed(control)
        return
    approvals.set_approval_card_ts(pending.approval_id, card_ts)
    pending.card_ts = card_ts
    arm_approval_expiry(pending)
    mark_bot_thread(workspace.team_id, channel, thread_ts)


# --- passive thread -----------------------------------------------------------------------


def _bot_spoke_previous(thread: list[dict], current_ts: str | None, bot_user_id: str | None) -> bool:
    earlier = [m for m in thread if not current_ts or (m.get("ts") or "") < current_ts]
    if not earlier:
        return False
    return is_our_slack_bot_message(earlier[-1], bot_user_id)


def _format_thread_text(thread: list[dict], user_names: dict[str, str], bot_user_ids: set[str], exclude_ts: str | None) -> str:
    lines = []
    for message in thread:
        if message.get("ts") == exclude_ts or not message.get("text"):
            continue
        user = message.get("user") or ""
        speaker = user_names.get(user, user or "someone")
        is_app = bool(message.get("bot_id") or message.get("app_id") or user in bot_user_ids)
        lines.append(f"{speaker} (app): {message['text']}" if is_app else f"{speaker}: {message['text']}")
    return "\n".join(lines[-THREAD_TRIAGE_CONTEXT_LIMIT:])


def run_slack_passive_thread(workspace: SlackWorkspace, ev: SlackEventInner, *, identity, question: str) -> None:
    triage_claim = event_store.claim_stored_event_for_triage(identity)
    if triage_claim is None:
        return
    if not ev.channel or not ev.thread_ts or not ev.user or not ev.ts:
        event_store.mark_stored_event_filtered(
            identity, "Passive thread triage requires a human-authored thread message.", triage_claim.id
        )
        return
    if not is_slack_content_message_subtype(ev.subtype) or not question.strip() or is_emoji_only_slack_text(question):
        event_store.mark_stored_event_filtered(identity, "The Slack thread event is structural noise or has no text.", triage_claim.id)
        return
    team_id = workspace.team_id
    client = workspace.client
    bot_user_id = workspace.bot_user_id
    asker_lookup = _user_lookup(workspace, ev.user)
    if not asker_lookup.ok:
        event_store.mark_stored_event_filtered(
            identity, f"Slack speaker verification failed: {asker_lookup.reason}.", triage_claim.id
        )
        return
    actor = resolve_actor(workspace, ev.user)
    if actor is None or not actor.is_member or not actor.person_id:
        event_store.mark_stored_event_filtered(
            identity, "The Slack speaker is not a current member of this organization.", triage_claim.id
        )
        return

    # warmLocalThreadContext: read Slack history on a cold or gappy scope, then serve
    # the bounded window from the local store.
    thread: list[dict] | None = None
    history_complete = event_store.is_local_context_history_complete(team_id=team_id, channel=ev.channel, thread_ts=ev.thread_ts)
    if event_store.claim_local_context_warmup(team_id=team_id, channel=ev.channel, thread_ts=ev.thread_ts, current_message_ts=ev.ts):
        history = client.get_slack_thread_history(
            ev.channel, ev.thread_ts, page_limit=THREAD_CONTEXT_PAGE_SIZE, max_messages=THREAD_TRIAGE_CONTEXT_LIMIT + 1
        )
        # Leave the scope cold on a failed read so the next event retries.
        if getattr(history, "ok", False):
            snapshot = event_store.merge_slack_thread_history_into_local_context(
                team_id=team_id,
                channel=ev.channel,
                thread_ts=ev.thread_ts,
                before_ts=ev.ts,
                messages=history.messages,
                limit=THREAD_TRIAGE_CONTEXT_LIMIT,
            )
            thread = snapshot.messages
            history_complete = bool(history.complete) and not snapshot.truncated
            if snapshot.retained_complete:
                event_store.mark_local_context_warm(
                    team_id=team_id, channel=ev.channel, thread_ts=ev.thread_ts, history_complete=history_complete
                )
    if thread is None:
        thread = event_store.load_local_thread_context(
            team_id=team_id, channel=ev.channel, thread_ts=ev.thread_ts, before_ts=ev.ts, limit=THREAD_TRIAGE_CONTEXT_LIMIT
        )
    other_mention_ids = mentioned_user_ids(ev.text, bot_user_id)
    profile_ids = triage_profile_user_ids(
        current_user_id=ev.user, mentioned_ids=other_mention_ids, history=thread, bot_user_id=bot_user_id
    )
    user_names: dict[str, str] = {}
    bot_user_ids: set[str] = set()
    for user_id in profile_ids:
        info = client.user_info(user_id)
        if info is None:
            continue
        user_names[user_id] = _asker_name(info) or user_id
        if info.is_bot:
            bot_user_ids.add(user_id)
    if bot_user_id:
        bot_user_ids.add(bot_user_id)
    known_bot_thread = has_bot_thread(team_id, ev.channel, ev.thread_ts)
    bot_in_thread = known_bot_thread or bool(bot_user_id and any(m.get("user") == bot_user_id for m in thread))
    bot_spoke_last = _bot_spoke_previous(thread, ev.ts, bot_user_id) if thread else known_bot_thread
    addressed_targets = [
        TriageAddressedTarget(slack_user_id=user_id, name=user_names.get(user_id), is_bot=user_id in bot_user_ids)
        for user_id in other_mention_ids
    ]
    trace_id = _new_trace_id()
    channel_info = client.get_cached_slack_conversation_info(team_id, ev.channel)
    sampling = reserve_triage_trace_sample(org_id=team_id, trace_id=trace_id)
    obs = TriageObservabilityContext(
        org_id=team_id,
        distinct_id=ev.user,
        trace_id=trace_id,
        session_id=f"{ev.channel}:{ev.thread_ts}",
        channel=ev.channel,
        message_ts=ev.ts,
        thread_ts=ev.thread_ts,
        chime_context="thread",
        triage_trace_sampled=sampling.sampled,
        triage_trace_sample_rate=sampling.sample_rate,
    )
    asker = asker_lookup.user
    triage = triage_chime_message(
        context="thread",
        question=question,
        context_text=_format_thread_text(thread, user_names, bot_user_ids, ev.ts),
        current_speaker=TriageSpeaker(name=_asker_name(asker), slack_user_id=ev.user),
        message_stamp=ev.ts,
        history_complete=history_complete,
        addressed_targets=addressed_targets,
        channel=(
            TriageChannel(
                name=getattr(channel_info, "name", None),
                topic=getattr(channel_info, "topic", None),
                purpose=getattr(channel_info, "purpose", None),
            )
            if channel_info
            else None
        ),
        thread_follow_up_override={"bot_spoke_previous": bot_spoke_last},
        obs=obs,
    )
    finalized = event_store.record_stored_triage_decision(
        identity,
        decision=triage.decision,
        claim_id=triage_claim.id,
        source=triage.source,
        priority=triage.priority,
        reason=triage.reason,
        emoji=triage.emoji if triage.decision == "ack" else None,
        fallback_emoji=triage.fallback_emoji if triage.decision == "answer" else None,
        trace_id=trace_id,
    )
    if not finalized:
        return
    if triage.decision == "pass":
        event_store.record_stored_action_outcome(identity, "silent_by_judgment")
        return
    if triage.decision == "ack":
        enqueue_passive_reaction(identity=identity, emoji=triage.emoji, kind="ack", reason=triage.reason or "", obs=obs)
        return
    if triage.decision == "answer":
        if not bot_in_thread:
            budget = reserve_chime_answer(
                channel_id=ev.channel,
                priority=triage.priority or "normal",
                last_channel_activity_at=event_store.get_previous_human_channel_activity_at(identity),
            )
            if not budget.allowed:
                fallback_used = can_use_answer_fallback(budget.suppression, bool(triage.fallback_emoji), triage.priority or "normal")
                event_store.record_stored_suppression(identity, suppression=budget.suppression, fallback_used=fallback_used)
                if fallback_used and triage.fallback_emoji:
                    enqueue_passive_reaction(
                        identity=identity,
                        emoji=triage.fallback_emoji,
                        kind="answer_fallback",
                        reason="Triage explicitly supplied a semantically valid reaction fallback.",
                        obs=obs,
                    )
                return
        event_store.record_stored_action_outcome(identity, "answer_budget_reserved")
        run_turn(
            workspace,
            ev,
            force_full_turn=True,
            force_thread_ts=ev.thread_ts,
            trace_id=trace_id,
            triage_result="answer",
            agent_main_effort=triage.agent_main_effort,
        )
        event_store.record_stored_action_outcome(identity, "answer_turn_completed")
        return

    investigation = reserve_passive_investigation(trace_id=trace_id, channel_id=ev.channel, priority=triage.priority or "normal")
    if not investigation.allowed:
        event_store.record_stored_suppression(identity, suppression=investigation.suppression)
        return
    try:
        run_turn(
            workspace,
            ev,
            force_full_turn=True,
            force_thread_ts=ev.thread_ts,
            trace_id=trace_id,
            triage_result="investigate",
            agent_main_effort=triage.agent_main_effort,
            passive_investigation={
                "reason": triage.reason,
                "priority": triage.priority,
                "obs": obs,
                "identity": identity,
                "claim": investigation.claim,
                "budget_finding": not bot_in_thread,
            },
        )
        release_passive_investigation(investigation.claim, "silent")
    except Exception:
        release_passive_investigation(investigation.claim, "failed")
        raise


# --- context retention ---------------------------------------------------------------------


def run_slack_context_event(workspace: SlackWorkspace, ev: SlackEventInner) -> None:
    """Port of ``onSlackContextEvent``: keep the message as local context, never triaged."""
    identity = event_store.record_slack_event(workspace.team_id, ev)
    if identity is not None:
        audit = event_store.get_stored_slack_event_audit(identity)
        if not (audit and audit.decision):
            event_store.mark_stored_event_filtered(identity, "Slack event was retained as local context but is not model-triaged.")
    retain_without_model(workspace, ev)


def retain_without_model(workspace: SlackWorkspace, ev: SlackEventInner) -> None:
    """Deviation: without a model, channel-observe can't distill memories, so keep the
    raw human message in its room's container instead. With a model this is a no-op and
    channel-observe owns channel memory, as upstream."""
    if is_model_available():
        return
    if should_ignore_message(ev) or not ev.user or not ev.channel or ev.user == workspace.bot_user_id:
        return
    if is_direct_conversation_event(ev):
        return
    text = clean_mention(ev.text)
    if not memory_service.worth_keeping(text):
        return
    scope, _info = _memory_scope(workspace, ev, False, ev.channel)
    write_tag = slack_memory_container_tag(scope)
    if not write_tag:
        return
    resolve_actor(workspace, ev.user)
    memory_service.write(text, write_tag, ev.user, ev.channel, ev.ts or "")


# --- reactions -----------------------------------------------------------------------------


def _reactor_is_member(workspace: SlackWorkspace, user_id: str) -> bool:
    lookup = _user_lookup(workspace, user_id)
    if not lookup.ok:
        return False
    actor = resolve_actor(workspace, user_id)
    return bool(actor and actor.is_member and actor.person_id)


def run_slack_debug_reaction(workspace: SlackWorkspace, ev: SlackEventInner) -> None:
    channel = ev.item.channel if ev.item else None
    message_ts = ev.item.ts if ev.item else None
    if not channel or not message_ts or not ev.user:
        return
    if not _reactor_is_member(workspace, ev.user):
        return
    trace = lookup_message_trace_records(channel, [message_ts]).get(message_ts)
    # Only our own replies carry a trace; reacting to a human message is a no-op.
    if trace is None:
        return
    delivery = deliver_slack_debug_trace(
        workspace.client, channel=channel, reactor_user=ev.user, text=f"Debug id: `{trace.trace_id}`", thread_ts=trace.thread_ts
    )
    log.info("[company-brain] debug-reaction channel=%s ts=%s trace=%s delivery=%s", channel, message_ts, trace.trace_id, delivery)


def run_slack_mute_reaction(workspace: SlackWorkspace, ev: SlackEventInner) -> None:
    channel = ev.item.channel if ev.item else None
    message_ts = ev.item.ts if ev.item else None
    if not channel or not message_ts or not ev.user:
        return
    if workspace.bot_user_id and ev.user == workspace.bot_user_id:
        return
    trace = lookup_message_trace_records(channel, [message_ts]).get(message_ts)
    if trace is None or not trace.thread_ts:
        return
    thread_ts = trace.thread_ts
    if not _reactor_is_member(workspace, ev.user):
        return
    team_id = workspace.team_id
    client = workspace.client
    if ev.type == "reaction_added":
        set_thread_muted(team_id, channel, thread_ts, ev.user)
        owner = get_thread_muted_by(team_id, channel, thread_ts) or ev.user
        if not update_mute_footer(workspace, channel, thread_ts, message_ts, muted_footer(owner)):
            client.post_slack_ephemeral(
                channel,
                ev.user,
                "Got it — I'll stay out of this thread. Mention me if you need me; remove the :black_square_for_stop: to undo.",
                thread_ts,
            )
        return
    muted_by = get_thread_muted_by(team_id, channel, thread_ts)
    if not muted_by:
        return
    if muted_by != ev.user:
        client.post_slack_ephemeral(
            channel,
            ev.user,
            f"Only <@{muted_by}>'s :black_square_for_stop: controls this mute — mention me if you need me now.",
            thread_ts,
        )
        return
    clear_thread_muted(team_id, channel, thread_ts)
    if not update_mute_footer(workspace, channel, thread_ts, message_ts, MUTE_OFFER_FOOTER):
        client.post_slack_ephemeral(channel, ev.user, "Okay — I can chime in here again.", thread_ts)


# --- interrupted turns ---------------------------------------------------------------------


def finalize_interrupted_slack_turn(workspace: SlackWorkspace, snapshot: dict) -> None:
    """Close a turn whose worker died mid-flight (upstream: fiber recovery gave up)."""
    if not snapshot.get("thread_key") or not snapshot.get("turn_id") or not snapshot.get("turn_revision"):
        return
    recovered = turn_control.TurnControlSnapshot(
        thread_key=snapshot["thread_key"], turn_id=snapshot["turn_id"], revision=snapshot["turn_revision"]
    )
    if not turn_control.complete_interrupted_thread_turn(recovered):
        return
    ev = snapshot["event"]
    target = resolve_slack_reply_target(workspace.client, ev)
    if target is None:
        return
    workspace.client.clear_assistant_thread_status(target.channel, target.thread_ts)
    terminal_reply = (snapshot.get("terminal_proposal") or {}).get("reply") or (
        "I couldn't finish this after retrying. Reply here to start fresh."
    )
    if snapshot.get("progress_message_ts") and workspace.client.update_slack_message(
        target.channel, snapshot["progress_message_ts"], terminal_reply, []
    ):
        return
    workspace.client.post_slack_message(target.channel, terminal_reply, target.thread_ts)


# --- approvals -----------------------------------------------------------------------------


def _turn_control_of(approval: approvals.PendingApproval):
    stored = (approval.extra or {}).get("turn_control")
    if not stored:
        return None
    return turn_control.TurnControlSnapshot(thread_key=stored["thread_key"], turn_id=stored["turn_id"], revision=stored["revision"])


def run_slack_approval_decision(team_id: str, approval_id: str, approved: bool, user_id: str, response_url: str | None) -> None:
    def ephemeral(text: str) -> None:
        update_interaction_response(response_url or "", text, response_type="ephemeral")

    def resolve(status: str, text: str) -> None:
        update_interaction_response(
            response_url or "", text, blocks=resolved_approval_blocks(approval_card(approval), status), replace_original=True
        )

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
    control = _turn_control_of(approval)
    if control is not None:
        turn_inbox.wait_for_turn_update_classification(control)
        if not turn_control.is_thread_turn_current(control):
            cancel_superseded_approval(approval, response_url)
            return

    workspace = get_workspace_by_team_id(team_id)
    if workspace is None:
        ephemeral("I couldn't resume that action. The workspace configuration is missing.")
        return
    lookup = _user_lookup(workspace, user_id)
    if not lookup.ok:
        ephemeral(format_slack_profile_lookup_failure())
        return
    actor = resolve_actor(workspace, user_id)
    if actor is None or not actor.is_member or not actor.person_id:
        ephemeral(format_slack_org_member_denial(person_name=_asker_name(lookup.user), org_name=workspace.team_name))
        return

    if not approvals.mark_approval_decided(approval_id, "approved" if approved else "denied", user_id):
        ephemeral("That approval was already decided.")
        return
    if not approved:
        resolve("Denied", "Denied. Nothing was changed.")
        turn_control.mark_thread_turn_completed(control)
        return

    resolve("Approved", "Approved.")
    try:
        result = execute_approved_action(
            approval.tool_name, approval.tool_input, approval.container_tag, internal_audience=approval.extra.get("internal_audience") is True
        )
    except Exception:
        log.exception("[company-brain] approved action failed approval=%s", approval_id)
        approvals.mark_approval_terminal(approval_id, "error", from_status="approved")
        workspace.client.post_slack_reply(approval.channel, "That action failed. Nothing was written.", approval.thread_ts)
        turn_control.mark_thread_turn_completed(control)
        return
    approvals.mark_approval_terminal(approval_id, "executed", from_status="approved")
    reply_ts = workspace.client.post_slack_reply(approval.channel, result, approval.thread_ts)
    if reply_ts:
        record_message_trace(approval.channel, reply_ts, approval.approval_id, approval.thread_ts)
    mark_bot_thread(team_id, approval.channel, approval.thread_ts)
    turn_control.mark_thread_turn_completed(control)


__all__ = [
    "run_turn",
    "run_slack_turn",
    "run_slack_passive_thread",
    "run_slack_context_event",
    "retain_without_model",
    "run_slack_debug_reaction",
    "run_slack_mute_reaction",
    "run_slack_approval_decision",
    "finalize_interrupted_slack_turn",
    "update_mute_footer",
]
