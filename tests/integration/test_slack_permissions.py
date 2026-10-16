"""End-to-end permission checks: real FalkorDB, fake Slack.

Runs against a throwaway graph (``company_test``) so the dev graph is untouched.
Needs FalkorDB on localhost:6379 (``docker compose up -d``).
"""

import unittest
import uuid
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db.falkordb_client import connect  # noqa: E402
from src.domain.access import approvals  # noqa: E402
from src.domain.access.roles import role_of  # noqa: E402
from src.domain.knowledge import ask_service  # noqa: E402
from src.domain.slack import channel_membership, turn_service  # noqa: E402
from src.domain.slack.workspace import SlackWorkspace  # noqa: E402
from src.integrations.slack.client import (  # noqa: E402
    SlackConversationInfo,
    SlackThreadHistory,
    SlackUserInfo,
    SlackUserInfoLookup,
)
from src.integrations.slack.events import SlackEventInner  # noqa: E402
from src.seed import quillfern  # noqa: E402
from src.seed.seed import reset_and_seed  # noqa: E402

PRIVATE_FACT = "the billing service retry storm comes from the invoice worker backlog"
PUBLIC_FACT = "billing service dashboards moved to the new metrics board"


class FakeSlack:
    """The SlackClient surface the turn loop uses. Unknown calls are recorded no-ops."""

    def __init__(self, run: str):
        self.posts: list[tuple[str, str, str | None]] = []
        self.ephemerals: list[tuple[str, str, str]] = []
        self.cards: list[tuple[str, object]] = []
        self.reactions: list[tuple[str, str, str]] = []
        self.other_calls: list[str] = []
        self.private = {f"GPRIV{run}"}
        self.members = {f"GPRIV{run}": ["UBOT", f"UALICE{run}"]}
        self.guests: set[str] = set()
        self.thread_messages: dict[str, list[dict]] = {}

    def __getattr__(self, name):
        def noop(*args, **kwargs):
            self.other_calls.append(name)
            return None

        return noop

    # --- conversations ---
    def conversation_info(self, channel):
        return SlackConversationInfo(id=channel, name=channel.lower(), is_private=channel in self.private)

    def get_cached_slack_conversation_info(self, team_id, channel):
        return self.conversation_info(channel)

    def conversation_members(self, channel):
        return list(self.members.get(channel, []))

    def bot_private_channels(self):
        return [SlackConversationInfo(id=c, name=c.lower(), is_private=True) for c in self.private]

    def open_conversation(self, user_id):
        return f"D{user_id}"

    def get_slack_thread_history(self, channel, thread_ts, **kwargs):
        return SlackThreadHistory(ok=True, messages=list(self.thread_messages.get(thread_ts, [])), complete=True)

    # --- people ---
    def user_info(self, user_id):
        return SlackUserInfo(
            id=user_id,
            real_name=user_id.lower(),
            email=f"{user_id.lower()}@quillfern.example.com",
            is_restricted=user_id in self.guests,
        )

    def lookup_slack_user_info(self, user_id):
        return SlackUserInfoLookup(ok=True, user=self.user_info(user_id))

    # --- messages ---
    def post_message(self, channel, text, thread_ts=None, blocks=None):
        self.posts.append((channel, text, thread_ts))
        return f"{len(self.posts)}.0"

    post_slack_message = post_message

    def post_slack_reply(self, channel, reply, thread_ts=None, extra_blocks=None):
        return self.post_message(channel, reply, thread_ts)

    def post_ephemeral(self, channel, user, text, thread_ts=None):
        self.ephemerals.append((channel, user, text))
        return True

    post_slack_ephemeral = post_ephemeral

    def post_slack_approval_card(self, channel, thread_ts, card):
        self.cards.append((channel, card))
        return "card.1"

    post_approval_card = post_slack_approval_card

    def get_slack_message_blocks(self, channel, thread_ts, message_ts):
        return {"text": "reply", "blocks": []}

    def update_slack_message(self, channel, ts, text, blocks=None):
        self.other_calls.append("update_slack_message")
        return True

    # --- reactions ---
    def add_slack_reaction(self, channel, ts, name, quiet_errors=None):
        self.reactions.append((channel, ts, name))
        return True

    def add_reaction(self, channel, ts, name):
        return self.add_slack_reaction(channel, ts, name)

    def remove_slack_reaction(self, channel, ts, name):
        pass


class SlackPermissionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_and_seed(quillfern)

    @classmethod
    def tearDownClass(cls):
        _, graph = connect()
        graph.delete()

    def setUp(self):
        _, graph = connect()
        graph.query("MATCH (m:Memory) DETACH DELETE m")
        run = uuid.uuid4().hex[:6].upper()
        self.run = run
        self.alice, self.bob, self.carol = f"UALICE{run}", f"UBOB{run}", f"UCAROL{run}"
        self.private, self.public = f"GPRIV{run}", f"CPUB{run}"
        self.slack = FakeSlack(run)
        self.ws = SlackWorkspace(team_id="T1", bot_user_id="UBOT", bot_name="company-brain", client=self.slack)
        self.ts = 1000

    def tearDown(self):
        # Approval cards arm real expiry jobs in the shared scheduler queue; drop ours.
        from src.core import scheduler as _scheduler
        from src.domain.slack.turn.approval_expiry import _job_id as _expiry_job_id

        for _channel, card in self.slack.cards:
            _scheduler.cancel(_expiry_job_id(card.approval_id))

    def _ts(self) -> str:
        self.ts += 1
        return f"{self.ts}.000{self.run}"

    def say(self, user, channel, text, channel_type, kind="message"):
        ev = SlackEventInner(type=kind, user=user, channel=channel, channel_type=channel_type, text=text, ts=self._ts())
        if kind == "app_mention" or channel_type == "im":
            turn_service.run_slack_turn(self.ws, ev)
        else:
            turn_service.run_slack_context_event(self.ws, ev)
        return self.slack.posts[-1][1] if self.slack.posts else ""

    def ask_dm(self, user, text):
        before = len(self.slack.posts)
        self.say(user, f"D{user}", text, "im")
        return self.slack.posts[-1][1] if len(self.slack.posts) > before else ""

    def ask_in(self, user, channel, channel_type, text):
        before = len(self.slack.posts)
        self.say(user, channel, f"<@UBOT> {text}", channel_type, kind="app_mention")
        return self.slack.posts[-1][1] if len(self.slack.posts) > before else ""

    def seed_conversation(self):
        self.say(self.alice, self.private, PRIVATE_FACT, "group")
        self.say(self.bob, self.public, PUBLIC_FACT, "channel")

    def test_public_channel_reads_only_public_memory(self):
        self.seed_conversation()
        reply = self.ask_in(self.carol, self.public, "channel", "billing service")
        self.assertIn("metrics board", reply)
        self.assertNotIn("retry storm", reply)

    def test_private_channel_reads_itself_and_public(self):
        self.seed_conversation()
        reply = self.ask_in(self.alice, self.private, "group", "billing service")
        self.assertIn("retry storm", reply)
        self.assertIn("metrics board", reply)

    def test_dm_reads_private_channels_the_asker_is_in(self):
        self.seed_conversation()
        self.assertIn("retry storm", self.ask_dm(self.alice, "billing service"))
        self.assertNotIn("retry storm", self.ask_dm(self.carol, "billing service"))

    def test_leaving_a_private_channel_revokes_dm_access(self):
        self.seed_conversation()
        self.assertIn("retry storm", self.ask_dm(self.alice, "billing service"))
        left = SlackEventInner(type="member_left_channel", user=self.alice, channel=self.private)
        channel_membership.run_slack_membership_event(self.slack, "UBOT", left)
        self.assertNotIn("retry storm", self.ask_dm(self.alice, "billing service"))

    def test_backfill_drops_members_slack_no_longer_lists(self):
        self.seed_conversation()
        self.slack.members[self.private] = ["UBOT"]
        channel_membership.reconcile_channel_membership(self.slack)
        self.assertNotIn("retry storm", self.ask_dm(self.alice, "billing service"))

    def test_dm_memory_is_personal(self):
        self.say(self.alice, f"D{self.alice}", f"remember: my billing service on-call swap is with {self.run}", "im")
        self.assertIn(self.run, self.ask_dm(self.alice, "billing service on-call swap"))
        self.assertNotIn(self.run, self.ask_dm(self.bob, "billing service on-call swap"))
        self.assertNotIn(self.run, self.ask_in(self.bob, self.public, "channel", "billing service on-call swap"))

    def test_guests_are_denied(self):
        guest = f"UGUEST{self.run}"
        self.slack.guests.add(guest)
        before = len(self.slack.posts)
        self.say(guest, f"D{guest}", "who owns billing", "im")
        # In a DM the denial is the bot's reply (upstream posts it there, not ephemerally).
        self.assertEqual(len(self.slack.posts), before + 1)
        self.assertIn("full members", self.slack.posts[-1][1])
        self.assertNotIn("Path", self.slack.posts[-1][1])

    def test_thread_reply_in_someone_elses_thread_is_context(self):
        ev = SlackEventInner(
            type="message", user=self.bob, channel=self.public, channel_type="channel",
            text="billing service alerts are noisy again today", ts=self._ts(), thread_ts="999.1",
        )
        turn_service.run_slack_turn(self.ws, ev)
        self.assertEqual(self.slack.posts, [])

    def test_ticket_needs_the_askers_approval_and_stays_private(self):
        self.ask_in(self.alice, self.private, "group", "ticket: billing service double charges annual plans")
        self.assertEqual(len(self.slack.cards), 1)
        approval_id = self.slack.cards[0][1].approval_id

        with mock.patch.object(turn_service, "update_interaction_response") as respond:
            turn_service.run_slack_approval_decision("T1", approval_id, True, self.carol, "https://example.invalid")
            self.assertIn("Only <@", respond.call_args.args[1])
        self.assertEqual(approvals.load_approval(approval_id).status, "pending")

        with mock.patch.object(turn_service, "update_interaction_response"), \
             mock.patch.object(turn_service, "get_workspace_by_team_id", return_value=self.ws):
            turn_service.run_slack_approval_decision("T1", approval_id, True, self.alice, "https://example.invalid")
            self.assertEqual(approvals.load_approval(approval_id).status, "executed")
            turn_service.run_slack_approval_decision("T1", approval_id, True, self.alice, "https://example.invalid")

        filed = self.slack.posts[-1][1]
        self.assertIn("TKT-", filed)
        ticket_id = filed.split("*")[1]
        _, graph = connect()
        scope = graph.query("MATCH (t:Ticket {id: $id}) RETURN t.scope", {"id": ticket_id}).result_set[0][0]
        self.assertEqual(scope, f"slack_channel_{self.private}")
        public = ask_service.who_knows("Billing Service", ["org_shared"])
        self.assertNotIn(ticket_id, str(public.to_dict()["path"]))

    def test_denied_ticket_writes_nothing(self):
        _, graph = connect()
        before = graph.query("MATCH (t:Ticket) RETURN count(t)").result_set[0][0]
        self.ask_in(self.alice, self.private, "group", "ticket: billing exports time out")
        approval_id = self.slack.cards[-1][1].approval_id
        with mock.patch.object(turn_service, "update_interaction_response"), \
             mock.patch.object(turn_service, "get_workspace_by_team_id", return_value=self.ws):
            turn_service.run_slack_approval_decision("T1", approval_id, False, self.alice, "https://example.invalid")
        self.assertEqual(approvals.load_approval(approval_id).status, "denied")
        self.assertEqual(graph.query("MATCH (t:Ticket) RETURN count(t)").result_set[0][0], before)

    def test_only_admins_change_roles(self):
        reply = self.ask_dm(self.carol, f"promote <@{self.bob}>")
        self.assertIn("Only owners and admins", reply)
        self.assertEqual(role_of(self.bob), "member")
        self.ask_dm("UOWNER", f"promote <@{self.bob}>")
        self.assertEqual(role_of(self.bob), "admin")

    def test_members_cannot_wipe_a_channel(self):
        self.seed_conversation()
        self.assertIn("Only owners and admins", self.ask_in(self.bob, self.public, "channel", "forget this channel"))
        self.assertIn("Forgot 1", self.ask_in("UOWNER", self.public, "channel", "forget this channel"))
        self.assertNotIn("metrics board", self.ask_in(self.carol, self.public, "channel", "billing service"))


if __name__ == "__main__":
    unittest.main()
