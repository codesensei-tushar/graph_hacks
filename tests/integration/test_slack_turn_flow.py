"""The ported turn loop end to end: real FalkorDB/Redis, fake Slack, no model.

Covers steering (stop), mute and debug reactions, passive thread triage with no
model, the channel chime path, approval expiry through the scheduler, and a
superseded approval.
"""

import json
import time
import unittest
import uuid
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.api.v1 import slack as slack_routes  # noqa: E402
from src.core import scheduler  # noqa: E402
from src.db.falkordb_client import connect, kv  # noqa: E402
from src.domain.access import approvals  # noqa: E402
from src.domain.slack import turn_service  # noqa: E402
from src.domain.slack.turn import event_store, turn_control  # noqa: E402
from src.domain.slack.turn.approval_expiry import _job_id  # noqa: E402
from src.domain.slack.turn.bot_threads import has_bot_thread, is_thread_muted  # noqa: E402
from src.domain.slack.workspace import SlackWorkspace  # noqa: E402
from src.integrations.slack.events import SlackEventInner, SlackReactionItem  # noqa: E402
from src.seed.seed import reset_and_seed  # noqa: E402
from tests.integration.test_slack_permissions import FakeSlack  # noqa: E402


class SlackTurnFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_and_seed()

    @classmethod
    def tearDownClass(cls):
        _, graph = connect()
        graph.delete()

    def setUp(self):
        run = uuid.uuid4().hex[:6].upper()
        self.run = run
        self.alice, self.bob = f"UALICE{run}", f"UBOB{run}"
        self.public = f"CPUB{run}"
        self.team = f"T{run}"
        self.slack = FakeSlack(run)
        self.ws = SlackWorkspace(team_id=self.team, bot_user_id="UBOT", bot_name="company-brain", client=self.slack)
        self.ts = 2000

    def tearDown(self):
        # Approval cards arm real expiry jobs in the shared scheduler queue; drop ours.
        from src.core import scheduler as _scheduler
        from src.domain.slack.turn.approval_expiry import _job_id as _expiry_job_id

        for _channel, card in self.slack.cards:
            _scheduler.cancel(_expiry_job_id(card.approval_id))

    def _ts(self) -> str:
        self.ts += 1
        return f"{self.ts}.000100"

    def _event(self, user, text, *, kind="message", thread_ts=None, channel=None, channel_type="channel"):
        return SlackEventInner(
            type=kind,
            user=user,
            channel=channel or self.public,
            channel_type=channel_type,
            text=text,
            ts=self._ts(),
            thread_ts=thread_ts,
        )

    def _answer_in_thread(self) -> tuple[str, str]:
        """@mention the bot; returns (thread_ts, reply_ts)."""
        ev = self._event(self.alice, "<@UBOT> who owns billing", kind="app_mention")
        turn_service.run_slack_turn(self.ws, ev)
        channel, _text, thread_ts = self.slack.posts[-1]
        self.assertEqual(channel, self.public)
        return ev.ts, f"{len(self.slack.posts)}.0"

    def test_mention_answers_in_thread_and_remembers_it(self):
        thread_ts, _ = self._answer_in_thread()
        self.assertIn("Arjun Mehta", self.slack.posts[-1][1])
        self.assertEqual(self.slack.posts[-1][2], thread_ts)
        self.assertTrue(has_bot_thread(self.team, self.public, thread_ts))
        names = [name for _c, _t, name in self.slack.reactions]
        self.assertIn("ack", names)
        self.assertIn("brain", names)

    def test_owner_can_stop_a_running_turn(self):
        thread_ts = self._ts()
        key = turn_control.slack_thread_turn_key(self.team, self.public, thread_ts)
        control = turn_control.begin_thread_turn(
            thread_key=key, asker_user=self.alice, original_question="research billing", expected=None
        )
        self.assertIsNotNone(control)
        # A live turn's notifier discards its stream with this text (run_slack_turn attaches it).
        cancelled = []
        turn_control.attach_turn_cancel_notifier(control, cancelled.append)
        stop = self._event(self.alice, "stop", thread_ts=thread_ts)
        turn_service.run_slack_turn(self.ws, stop)
        self.assertEqual(turn_control.get_thread_turn(key).status, "cancelled")
        self.assertTrue(control.signal.is_set())
        self.assertEqual(cancelled, ["cancelled"])
        # Someone else saying "stop" in a thread is not a cancel.
        other_key = turn_control.slack_thread_turn_key(self.team, self.public, self._ts())
        other = turn_control.begin_thread_turn(thread_key=other_key, asker_user=self.alice, original_question="x", expected=None)
        turn_service.run_slack_turn(self.ws, self._event(self.bob, "stop", thread_ts=other_key.rsplit(":", 1)[1]))
        self.assertNotEqual(turn_control.get_thread_turn(other_key).status, "cancelled")
        turn_control.mark_thread_turn_completed(other)

    def test_follow_up_while_running_is_queued_not_answered(self):
        thread_ts = self._ts()
        key = turn_control.slack_thread_turn_key(self.team, self.public, thread_ts)
        control = turn_control.begin_thread_turn(
            thread_key=key, asker_user=self.alice, original_question="who owns billing", expected=None
        )
        before = len(self.slack.posts)
        turn_service.run_slack_turn(self.ws, self._event(self.alice, "also include the invoice worker", thread_ts=thread_ts))
        # No model: the gate fails open to APPEND and the update waits in the inbox.
        self.assertEqual(len(self.slack.posts), before)
        pending = [u.instruction for u in turn_control_updates(control)]
        self.assertEqual(pending, ["also include the invoice worker"])
        turn_control.mark_thread_turn_completed(control)

    def test_passive_thread_reply_without_model_stays_silent(self):
        thread_ts, _ = self._answer_in_thread()
        before = len(self.slack.posts)
        ev = self._event(self.bob, "billing alerts are noisy again today", thread_ts=thread_ts)
        turn_service.run_slack_turn(self.ws, ev)
        self.assertEqual(len(self.slack.posts), before)
        identity = event_store.slack_event_identity(self.team, ev)
        self.assertEqual(event_store.get_stored_slack_event_audit(identity).decision, "pass")

    def test_channel_chime_without_model_is_silent_but_keeps_memory(self):
        ev = self._event(self.bob, "the billing service deploy moved to thursday this week")
        with mock.patch("src.domain.slack.turn.chime._arm_channel_observe"):
            slack_routes._run_chime(self.ws, ev)
        self.assertEqual(self.slack.posts, [])
        identity = event_store.slack_event_identity(self.team, ev)
        self.assertEqual(event_store.get_stored_slack_event_audit(identity).decision, "pass")
        _, graph = connect()
        rows = graph.query("MATCH (m:Memory {channel_id: $c}) RETURN m.scope", {"c": self.public}).result_set
        self.assertEqual(rows, [["org_shared"]])

    def test_mute_reaction_mutes_and_only_the_muter_can_unmute(self):
        thread_ts, reply_ts = self._answer_in_thread()
        item = SlackReactionItem(type="message", channel=self.public, ts=reply_ts)
        mute = SlackEventInner(type="reaction_added", user=self.bob, reaction="black_square_for_stop", item=item)
        turn_service.run_slack_mute_reaction(self.ws, mute)
        self.assertTrue(is_thread_muted(self.team, self.public, thread_ts))
        self.assertIn("update_slack_message", self.slack.other_calls)

        other = SlackEventInner(type="reaction_removed", user=self.alice, reaction="black_square_for_stop", item=item)
        turn_service.run_slack_mute_reaction(self.ws, other)
        self.assertTrue(is_thread_muted(self.team, self.public, thread_ts))
        self.assertIn(f"Only <@{self.bob}>", self.slack.ephemerals[-1][2])

        unmute = SlackEventInner(type="reaction_removed", user=self.bob, reaction="black_square_for_stop", item=item)
        turn_service.run_slack_mute_reaction(self.ws, unmute)
        self.assertFalse(is_thread_muted(self.team, self.public, thread_ts))

    def test_debug_reaction_shows_trace_only_on_bot_replies(self):
        _thread_ts, reply_ts = self._answer_in_thread()
        item = SlackReactionItem(type="message", channel=self.public, ts=reply_ts)
        turn_service.run_slack_debug_reaction(self.ws, SlackEventInner(type="reaction_added", user=self.bob, reaction="mag", item=item))
        self.assertIn("Debug id", self.slack.ephemerals[-1][2])
        before = len(self.slack.ephemerals)
        human = SlackReactionItem(type="message", channel=self.public, ts="1.000001")
        turn_service.run_slack_debug_reaction(self.ws, SlackEventInner(type="reaction_added", user=self.bob, reaction="mag", item=human))
        self.assertEqual(len(self.slack.ephemerals), before)

    def test_unanswered_approval_expires_through_the_scheduler(self):
        turn_service.run_slack_turn(self.ws, self._event(self.alice, "<@UBOT> ticket: billing exports time out", kind="app_mention"))
        approval_id = self.slack.cards[-1][1].approval_id
        self.assertTrue(scheduler.exists(_job_id(approval_id)))
        record = json.loads(kv().get(f"brain:approval:{approval_id}"))
        record["expires_at"] = 1
        kv().set(f"brain:approval:{approval_id}", json.dumps(record), keepttl=True)

        with mock.patch("src.domain.slack.workspace.get_workspace_by_team_id", return_value=self.ws):
            scheduler.run_due(now=time.time() + approvals.APPROVAL_EXPIRY_MS / 1000 + 5)
        self.assertEqual(approvals.load_approval(approval_id).status, "expired")
        self.assertIn("update_slack_message", self.slack.other_calls)

    def test_approval_for_a_superseded_turn_is_cancelled(self):
        ask = self._event(self.alice, "<@UBOT> ticket: billing exports time out", kind="app_mention")
        turn_service.run_slack_turn(self.ws, ask)
        approval_id = self.slack.cards[-1][1].approval_id
        key = turn_control.slack_thread_turn_key(self.team, self.public, ask.ts)
        self.assertEqual(turn_control.get_thread_turn(key).status, "waiting_approval")
        turn_control.interrupt_thread_turn(key, "superseded", "never mind")

        with mock.patch("src.domain.slack.turn.approval_requests.update_interaction_response") as respond:
            turn_service.run_slack_approval_decision(self.team, approval_id, True, self.alice, "https://example.invalid")
        self.assertEqual(approvals.load_approval(approval_id).status, "cancelled")
        self.assertIn("superseded", respond.call_args.args[1])


def turn_control_updates(control):
    from src.domain.slack.turn import turn_inbox

    return [
        update
        for update in turn_inbox.list_current_turn_updates(
            thread_key=control.thread_key, turn_id=control.turn_id, revision=control.revision
        )
        if update.status == "pending"
    ]


if __name__ == "__main__":
    unittest.main()
