"""Turn machinery ported from company-brain: policies (pure) and Redis-backed state.

Redis-backed cases use the FalkorDB Redis on localhost:6379 with unique team and
channel ids, and delete what they wrote.
"""

import unittest
import uuid
from types import SimpleNamespace
from dataclasses import dataclass, field
from unittest import mock

from src.core import scheduler
from src.db.falkordb_client import kv
from src.domain.slack.turn import (
    active_turn_gate,
    approval_expiry,
    approval_requests,
    chime,
    chime_budget,
    event_store,
    reaction_queue,
    triage,
    turn_control,
    turn_inbox,
)
from src.domain.slack.turn.prompt_batch import build_slack_prompt_batch
from src.domain.slack.turn.proactivity import parse_brain_proactivity, resolve_channel_proactivity
from src.domain.slack.turn.reaction_policy import ACK_BREAKER_MAX_PER_HOUR, AckBreakerState, evaluate_ack_breaker
from src.domain.slack.turn.reset import reset_turn_state
from src.domain.slack.turn.thread_root import is_slack_history_thread_root
from src.domain.slack.turn.triage_sampling import TRIAGE_TRACE_FULL_CAPTURE_PER_ORG_DAY, evaluate_triage_trace_sampling
from src.integrations.slack.events import SlackEventInner

HOUR = 60 * 60 * 1000
NOW = 1_800_000_000_000


class ChimeBudgetPolicyTest(unittest.TestCase):
    def state(self, **overrides):
        return chime_budget.ChimeBudgetState(**{**chime_budget.empty_chime_budget_state(NOW).__dict__, **overrides})

    def test_absolute_ceiling_wins(self):
        decision = chime_budget.evaluate_chime_budget(state=self.state(total=12), priority="summons", now=NOW)
        self.assertEqual(decision.suppression, "budget_ceiling")

    def test_summons_allowance(self):
        self.assertTrue(chime_budget.evaluate_chime_budget(state=self.state(summons=3), priority="summons", now=NOW).allowed)
        self.assertEqual(
            chime_budget.evaluate_chime_budget(state=self.state(summons=4), priority="summons", now=NOW).suppression,
            "budget_priority_allowance",
        )

    def test_normal_min_interval_then_allowance(self):
        recent = self.state(last_normal_reply_at=NOW - 60_000)
        self.assertEqual(chime_budget.evaluate_chime_budget(state=recent, priority="normal", now=NOW).suppression, "budget_min_interval")
        allowed = chime_budget.evaluate_chime_budget(state=self.state(), priority="normal", now=NOW)
        self.assertEqual((allowed.next.general, allowed.next.last_normal_reply_at), (1, NOW))

    def test_low_needs_quiet_channel(self):
        busy = chime_budget.evaluate_chime_budget(state=self.state(), priority="low", now=NOW, last_channel_activity_at=NOW - 60_000)
        self.assertEqual(busy.suppression, "budget_min_interval")
        self.assertTrue(chime_budget.evaluate_chime_budget(state=self.state(), priority="low", now=NOW, last_channel_activity_at=NOW - HOUR).allowed)

    def test_urgent_finding_pays_only_ceiling(self):
        decision = chime_budget.evaluate_chime_budget(state=self.state(general=6), priority="urgent", now=NOW, urgent_investigation_finding=True)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.next.general, 6)

    def test_new_hour_resets_counts_but_keeps_last_reply(self):
        old = self.state(bucket=NOW // HOUR - 1, total=12, last_normal_reply_at=NOW - 60_000)
        decision = chime_budget.evaluate_chime_budget(state=old, priority="normal", now=NOW)
        self.assertEqual(decision.suppression, "budget_min_interval")

    def test_answer_fallback_only_for_normal_min_interval(self):
        self.assertTrue(chime_budget.can_use_answer_fallback("budget_min_interval", True, "normal"))
        self.assertFalse(chime_budget.can_use_answer_fallback("budget_ceiling", True, "normal"))
        self.assertFalse(chime_budget.can_use_answer_fallback("budget_min_interval", True, "low"))


class SmallPolicyTest(unittest.TestCase):
    def test_ack_breaker_opens_at_threshold(self):
        state = AckBreakerState(bucket=NOW // HOUR, count=ACK_BREAKER_MAX_PER_HOUR, open_until=0)
        decision = evaluate_ack_breaker(state, NOW)
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.opened)
        self.assertFalse(evaluate_ack_breaker(decision.next, NOW + 1).allowed)
        self.assertTrue(evaluate_ack_breaker(decision.next, NOW + HOUR + 1).allowed)

    def test_proactivity_precedence(self):
        settings = {"default": "own_channel_only", "channels": {"C1": "proactive", "C2": "loud"}}
        self.assertEqual(parse_brain_proactivity(settings), {"default": "own_channel_only", "channels": {"C1": "proactive"}})
        self.assertEqual(resolve_channel_proactivity(settings=settings, channel_id="C1"), "proactive")
        self.assertEqual(resolve_channel_proactivity(settings=settings, channel_id="C3"), "quiet")
        self.assertEqual(resolve_channel_proactivity(settings=settings, channel_id="C3", channel_name="company-brain"), "proactive")
        self.assertEqual(resolve_channel_proactivity(settings={}, channel_id="C3"), "proactive")

    def test_prompt_batch_truncates_first_oversized_line(self):
        batch = build_slack_prompt_batch([{"user": "U1", "text": "x" * 50}, {"user": "U2", "text": "y"}], 20)
        self.assertEqual(len(batch.messages), 1)
        self.assertTrue(batch.prompt.endswith(" … [truncated]"))
        self.assertEqual(len(batch.prompt), 20)

    def test_thread_root(self):
        self.assertTrue(is_slack_history_thread_root({"ts": "1.1", "thread_ts": "1.1", "reply_count": 2}))
        self.assertFalse(is_slack_history_thread_root({"ts": "1.2", "thread_ts": "1.1", "reply_count": 2}))
        self.assertFalse(is_slack_history_thread_root({"ts": "1.1", "reply_count": 0}))

    def test_trace_sampling_is_stable(self):
        self.assertTrue(evaluate_triage_trace_sampling(TRIAGE_TRACE_FULL_CAPTURE_PER_ORG_DAY, "t").sampled)
        first = evaluate_triage_trace_sampling(TRIAGE_TRACE_FULL_CAPTURE_PER_ORG_DAY + 1, "trace-abc")
        self.assertEqual(first, evaluate_triage_trace_sampling(TRIAGE_TRACE_FULL_CAPTURE_PER_ORG_DAY + 1, "trace-abc"))
        self.assertEqual(first.sample_rate, 0.2)

    def test_steering(self):
        self.assertEqual(turn_control.classify_turn_steering("stop"), "stop")
        self.assertEqual(turn_control.classify_turn_steering("nvm"), "stop")
        self.assertEqual(turn_control.classify_turn_steering("don't stop"), "none")
        self.assertEqual(turn_control.classify_turn_steering("thanks!"), "none")
        self.assertEqual(turn_control.classify_turn_steering("actually use the billing table"), "revise")
        self.assertEqual(turn_control.classify_turn_steering("ok cool"), "none")

    def test_gate_policy_and_fallback(self):
        self.assertEqual(active_turn_gate.apply_active_turn_policy(outcome="replace", author_owns_turn=False, turn_status="running"), "queue")
        self.assertEqual(active_turn_gate.apply_active_turn_policy(outcome="append", author_owns_turn=True, turn_status="waiting_approval"), "restart")
        gate_input = active_turn_gate.ActiveTurnGateInput(active_question="q", message="looks good", author_owns_turn=True)
        self.assertEqual(active_turn_gate.triage_active_turn_message(gate_input).outcome, "append")
        correction = active_turn_gate.ActiveTurnGateInput(active_question="q", message="actually no, the other one", author_owns_turn=True)
        self.assertEqual(active_turn_gate.triage_active_turn_message(correction, generate=lambda s, p: "IGNORE").outcome, "replace")


class TriageParserTest(unittest.TestCase):
    def test_answer_with_fields(self):
        result = triage.parse_triage_result("ANSWER\nPriority: urgent\nAgentMainEffort: high\nFallback: ack tada")
        self.assertEqual((result.decision, result.priority, result.agent_main_effort, result.fallback_emoji), ("answer", "urgent", "high", "tada"))

    def test_answer_bad_priority_normalizes(self):
        result = triage.parse_triage_result("ANSWER\nPriority: whenever")
        self.assertEqual((result.priority, result.priority_normalized), ("normal", True))

    def test_answer_with_stray_line_falls_back(self):
        self.assertEqual(triage.parse_triage_result("ANSWER\nhello").source, "parse_fallback")

    def test_ack_requires_known_emoji_and_reason(self):
        self.assertEqual(triage.parse_triage_result("ACK\nEmoji: tada\nReason: shipped").emoji, "tada")
        self.assertEqual(triage.parse_triage_result("ACK\nEmoji: thumbsup\nReason: ok").source, "parse_fallback")

    def test_investigate_and_pass(self):
        investigate = triage.parse_triage_result("INVESTIGATE\nPriority: urgent\nReason: check the deploy")
        self.assertEqual((investigate.decision, investigate.priority), ("investigate", "urgent"))
        self.assertEqual(triage.parse_triage_result("PASS\nReason: banter\nbetween people").reason, "banter between people")
        self.assertEqual(triage.parse_triage_result("PASS").source, "parse_fallback")
        self.assertEqual(triage.parse_triage_result("", "channel").reason, "Channel triage output was invalid; Company Brain stayed silent.")

    def test_no_model_is_pass_but_thread_overrides_still_apply(self):
        channel = triage.triage_chime_message(context="channel", question="who owns billing?", context_text="")
        self.assertEqual((channel.decision, channel.source), ("pass", "error_fallback"))
        follow_up = triage.triage_chime_message(
            context="thread",
            question="what about invoices?",
            context_text="",
            thread_follow_up_override={"bot_spoke_previous": True},
        )
        self.assertEqual((follow_up.decision, follow_up.source), ("answer", "explicit_followup_override"))
        accepted = triage.triage_chime_message(
            context="thread",
            question="yes please",
            context_text="Company Brain: want me to check the invoice worker?",
            thread_follow_up_override={"bot_spoke_previous": True},
        )
        self.assertEqual(accepted.source, "affirmative_override")

    def test_provider_error_stays_pass(self):
        def boom(system, prompt):
            raise RuntimeError("provider down")

        result = triage.triage_chime_message(
            context="thread", question="what about invoices?", context_text="", thread_follow_up_override={"bot_spoke_previous": True}, generate=boom
        )
        self.assertEqual((result.decision, result.source), ("pass", "error_fallback"))

    def test_user_prompt_escapes_and_labels(self):
        prompt = triage.build_triage_user_prompt(
            question="hi",
            context_text="A: x",
            context="thread",
            current_speaker=triage.TriageSpeaker(name="<Ana>", slack_user_id="U1"),
            history_complete=False,
            channel=triage.TriageChannel(name="eng"),
        )
        self.assertIn("&lt;Ana&gt; (slack_user_id=U1): hi", prompt)
        self.assertIn("<thread_history_status>", prompt)
        self.assertIn("<channel>name: #eng; topic: not set</channel>", prompt)

    def test_approval_summary(self):
        summary = approval_requests.approval_summary("send_to", {"channel": "C0123456", "message": "ship it"})
        self.assertEqual(summary, "To: <#C0123456>\nMessage:\nship it\nAction: Deliver with your name attributed")
        self.assertEqual(approval_requests.approval_summary("tickets.create", {"title": "Bug"}), "Subject: Bug")
        self.assertEqual(approval_requests.approval_summary("noop", {}), "Run Noop")


class _RedisCase(unittest.TestCase):
    def setUp(self):
        self.run = uuid.uuid4().hex[:8].upper()
        self.team = f"TT{self.run}"
        self.channel = f"CT{self.run}"

    def tearDown(self):
        reset_turn_state(self.team)
        kv().hdel("brain:reaction-breaker", self.channel)
        kv().hdel("brain:chime-budget", self.channel)
        for key in kv().scan_iter(match=f"*{self.run}*"):
            kv().delete(key)
        kv().delete("brain:reaction-queue:schedule")

    def message(self, ts, text="hello there team", user="U1", **extra):
        return SlackEventInner(type="message", channel=self.channel, channel_type="channel", user=user, text=text, ts=ts, **extra)


class EventStoreTest(_RedisCase):
    def test_record_and_load_context(self):
        event_store.record_slack_event(self.team, self.message("100.000001", "first message"))
        event_store.record_slack_event(self.team, self.message("100.000002", "reply", thread_ts="100.000001"))
        event_store.record_slack_event(self.team, self.message("100.000003", "bot", bot_id="B1"))
        channel = event_store.load_local_channel_context(team_id=self.team, channel=self.channel, before_ts="200")
        self.assertEqual([m["ts"] for m in channel], ["100.000001", "100.000003"])
        self.assertEqual(channel[1]["bot_id"], "B1")
        thread = event_store.load_local_thread_context(team_id=self.team, channel=self.channel, thread_ts="100.000001", before_ts="200")
        self.assertEqual([m["ts"] for m in thread], ["100.000001", "100.000002"])
        identity = event_store.slack_event_identity(self.team, self.message("100.000004"))
        self.assertEqual(event_store.get_previous_human_channel_activity_at(identity) is not None, True)

    def test_ring_is_bounded(self):
        for index in range(event_store.SLACK_EVENT_RING_MAX_PER_CHANNEL + 5):
            event_store.record_slack_event(self.team, self.message(f"{1000 + index}.000001"))
        rows = kv().hlen(f"brain:slack-event:{self.team}:{self.channel}")
        self.assertEqual(rows, event_store.SLACK_EVENT_RING_MAX_PER_CHANNEL)

    def test_triage_claim_is_exclusive_and_fenced(self):
        identity = event_store.record_slack_event(self.team, self.message("300.000001"))
        claim = event_store.claim_stored_event_for_triage(identity, now=NOW)
        self.assertIsNotNone(claim)
        self.assertIsNone(event_store.claim_stored_event_for_triage(identity, now=NOW + 1))
        stolen = event_store.claim_stored_event_for_triage(identity, now=NOW + event_store.TRIAGE_CLAIM_LEASE_MS)
        self.assertIsNotNone(stolen)
        self.assertFalse(event_store.record_stored_triage_decision(identity, decision="pass", claim_id=claim.id))
        self.assertTrue(event_store.record_stored_triage_decision(identity, decision="pass", claim_id=stolen.id, reason="r"))
        self.assertEqual(event_store.get_stored_slack_event_audit(identity).decision, "pass")

    def test_deleted_message_drops_text(self):
        event_store.record_slack_event(self.team, self.message("400.000001", "secret plan"))
        event_store.record_slack_event(self.team, SlackEventInner(type="message", channel=self.channel, subtype="message_deleted", ts="400.000009"))
        row = kv().hget(f"brain:slack-event:{self.team}:{self.channel}", "400.000009")
        self.assertIsNotNone(row)

    def test_warmup_claim_and_gap(self):
        self.assertTrue(event_store.claim_local_context_warmup(team_id=self.team, channel=self.channel, now=NOW))
        self.assertFalse(event_store.claim_local_context_warmup(team_id=self.team, channel=self.channel, now=NOW + 1))
        event_store.mark_local_context_warm(team_id=self.team, channel=self.channel)
        self.assertTrue(event_store.is_local_context_history_complete(team_id=self.team, channel=self.channel))
        self.assertTrue(event_store.has_local_context_gap("100000.0", "1.0"))
        event_store.merge_slack_history_into_local_context(
            team_id=self.team, channel=self.channel, messages=[{"ts": "500.000001", "user": "U2", "text": "older"}]
        )
        self.assertEqual(event_store.load_local_channel_context(team_id=self.team, channel=self.channel, before_ts="600")[0]["text"], "older")


class TurnControlTest(_RedisCase):
    def test_turn_lifecycle_with_inbox(self):
        key = turn_control.slack_thread_turn_key(self.team, self.channel, "1.1")
        control = turn_control.begin_thread_turn(thread_key=key, asker_user="U1", original_question="q", expected=None)
        self.assertIsNotNone(control)
        self.assertIsNone(turn_control.begin_thread_turn(thread_key=key, asker_user="U1", original_question="q", expected=None))
        self.assertTrue(turn_control.is_steerable_thread_turn(turn_control.get_thread_turn(key)))

        reservation = turn_inbox.reserve_turn_update(
            thread_key=key, turn_id=control.turn_id, revision=control.revision, message_ts="2.1", author_user="U2", instruction="also invoices"
        )
        self.assertEqual(reservation, "reserved")
        self.assertEqual(
            turn_inbox.reserve_turn_update(
                thread_key=key, turn_id=control.turn_id, revision=control.revision, message_ts="2.2", author_user="U2", instruction="Also  invoices"
            ),
            "duplicate",
        )
        self.assertEqual(turn_control.claim_thread_turn_finalization_if_inbox_empty(control), "updates_pending")
        turn_inbox.wait_for_turn_update_classification(control, timeout_ms=0)
        updates = turn_inbox.consume_pending_turn_updates(control)
        self.assertEqual([u.instruction for u in updates], ["also invoices"])
        self.assertIn("U2: also invoices", turn_inbox.format_turn_updates(updates))
        self.assertEqual(turn_control.claim_thread_turn_finalization_if_inbox_empty(control), "claimed")
        turn_control.mark_thread_turn_completed(control)
        self.assertEqual(turn_control.get_thread_turn(key).status, "completed")

    def test_interrupt_aborts_and_notifies(self):
        key = turn_control.slack_thread_turn_key(self.team, self.channel, "3.1")
        control = turn_control.begin_thread_turn(thread_key=key, asker_user="U1", original_question="q", expected=None)
        notified = []
        turn_control.attach_turn_cancel_notifier(control, notified.append)
        result = turn_control.interrupt_thread_turn(key, "cancelled", "stop")
        self.assertTrue(result.aborted and result.notified)
        self.assertTrue(control.signal.is_set())
        self.assertEqual(notified, ["cancelled"])
        self.assertFalse(turn_control.is_thread_turn_current(control))
        revised = turn_control.begin_thread_turn(
            thread_key=key,
            asker_user="U1",
            original_question="q2",
            expected=turn_control.thread_turn_start_expectation(turn_control.get_thread_turn(key)),
        )
        self.assertEqual(revised.revision, 2)


@dataclass
class _FakeClient:
    reactions: list = field(default_factory=list)
    updates: list = field(default_factory=list)
    history: list = field(default_factory=list)

    def add_slack_reaction_detailed(self, channel, ts, name):
        self.reactions.append((channel, ts, name))
        return reaction_queue._ReactionResult(ok=True, outcome="added")

    def update_slack_message(self, channel, ts, text, blocks=None):
        self.updates.append((channel, ts, text))
        return True

    def get_slack_channel_history(self, channel, **opts):
        return SimpleNamespace(ok=True, messages=self.history)

    def conversation_info(self, channel):
        return SimpleNamespace(name="eng", topic=None, purpose=None)

    def user_info(self, user_id):
        return SimpleNamespace(real_name=user_id.lower(), display_name=None, name=user_id, is_bot=False, tz_offset=0, email=None, is_restricted=False)


@dataclass
class _FakeWorkspace:
    team_id: str
    client: _FakeClient
    bot_user_id: str = "UBOT"


class ReactionQueueTest(_RedisCase):
    def test_enqueue_dedup_and_deliver(self):
        identity = event_store.record_slack_event(self.team, self.message("700.000001"))
        obs = triage.TriageObservabilityContext(org_id=self.team, distinct_id="U1", trace_id="t1")
        with mock.patch.object(scheduler, "schedule"):
            self.assertTrue(reaction_queue.enqueue_passive_reaction(identity=identity, emoji="tada", kind="ack", reason="r", obs=obs).enqueued)
            again = reaction_queue.enqueue_passive_reaction(identity=identity, emoji="tada", kind="ack", reason="r", obs=obs)
        self.assertEqual(again.suppression, "dedup")
        client = _FakeClient()
        schedule_id = kv().get("brain:reaction-queue:schedule")
        schedule_id = schedule_id.decode() if isinstance(schedule_id, bytes) else schedule_id
        with mock.patch("src.domain.slack.workspace.get_workspace_by_team_id", return_value=_FakeWorkspace(self.team, client)), mock.patch.object(
            scheduler, "schedule"
        ):
            reaction_queue.run_passive_reaction_queue({"schedule_id": schedule_id})
        self.assertIn((self.channel, "700.000001", "tada"), client.reactions)
        self.assertEqual(event_store.get_stored_slack_event_audit(identity).action_outcome, "added")


class ApprovalExpiryTest(_RedisCase):
    def test_expired_card_collapses(self):
        from src.domain.access import approvals

        approval = approvals.insert_pending_approval(
            team_id=self.team, channel=self.channel, thread_ts="1.1", asker_user="U1", tool_name="Create ticket",
            tool_input={}, summary="s", container_tag="org_shared", now=1,
        )
        approvals.set_approval_card_ts(approval.approval_id, "9.9")
        client = _FakeClient()
        with mock.patch("src.domain.slack.workspace.get_workspace_by_team_id", return_value=_FakeWorkspace(self.team, client)):
            approval_expiry.run_approval_expiry({"approval_id": approval.approval_id})
        self.assertEqual(approvals.load_approval(approval.approval_id).status, "expired")
        self.assertEqual(client.updates, [(self.channel, "9.9", approval_expiry.EXPIRED_TEXT)])
        kv().delete(f"brain:approval:{approval.approval_id}")


class ChimeTest(_RedisCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(chime, "_arm_channel_observe")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_channel_chime_without_model_stays_silent(self):
        client = _FakeClient()
        workspace = _FakeWorkspace(self.team, client)
        ran = []
        ev = self.message("800.000001", "the billing service is throwing errors since the deploy")
        member = mock.Mock(is_member=True)
        with mock.patch("src.domain.slack.workspace.resolve_actor", return_value=member):
            chime.run_slack_chime_in(workspace, ev, run_turn=lambda *a, **k: ran.append(k))
        self.assertEqual(ran, [])
        identity = event_store.slack_event_identity(self.team, ev)
        audit = event_store.get_stored_slack_event_audit(identity)
        self.assertEqual((audit.decision, audit.action_outcome), ("pass", "silent_by_judgment"))

    def test_channel_chime_answer_spends_budget_and_runs_turn(self):
        client = _FakeClient()
        workspace = _FakeWorkspace(self.team, client)
        ran = []
        member = mock.Mock(is_member=True)
        with mock.patch("src.domain.slack.workspace.resolve_actor", return_value=member), mock.patch.object(
            triage, "generate_text", return_value="ANSWER\nPriority: summons\nAgentMainEffort: low"
        ):
            chime.run_slack_chime_in(workspace, self.message("900.000001", "if only some AI could draft this"), run_turn=lambda *a, **k: ran.append(k))
            chime.run_slack_chime_in(workspace, self.message("900.000002", "<@UBOT> hi there"), run_turn=lambda *a, **k: ran.append(k))
        self.assertEqual(len(ran), 1)
        self.assertEqual((ran[0]["triage_result"], ran[0]["force_thread_ts"]), ("answer", "900.000001"))

    def test_emoji_only_is_filtered(self):
        self.assertTrue(chime.is_emoji_only_slack_text(":tada: 🎉"))
        self.assertFalse(chime.is_emoji_only_slack_text("ship it 🎉"))


if __name__ == "__main__":
    unittest.main()
