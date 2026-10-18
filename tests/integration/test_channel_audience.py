"""Who may read org_internal, and public channels reached by app_mention.

- org_internal (connector data) is read only when everyone in the room is a full member:
  a guest or a Slack Connect channel drops it (and restricted connector scopes) from the reads.
- app_mention carries no channel_type: conversations.info decides, so ``decision:`` asked in
  a public channel is written org_shared, and the channel is not recorded as private.
"""

import unittest
import uuid
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.db.falkordb_client import connect, kv  # noqa: E402
from src.domain.access import approvals  # noqa: E402
from src.domain.memory.read_scope import resolve_brain_read_container_tags  # noqa: E402
from src.domain.memory.writeback import ORG_INTERNAL_CONTAINER_TAG, SlackMemoryScope, slack_memory_scope_for_turn  # noqa: E402
from src.domain.slack import channel_audience, turn_service  # noqa: E402
from src.domain.slack.lifecycle_api import SlackMember  # noqa: E402
from src.domain.slack.workspace import SlackWorkspace  # noqa: E402
from src.integrations.slack.client import SlackConversationInfo, SlackCursorPage  # noqa: E402
from src.integrations.slack.events import SlackEventInner  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402
from src.seed.seed import reset_and_seed  # noqa: E402
from tests.integration.test_slack_permissions import FakeSlack  # noqa: E402

GRAPH = f"company_audience_test{tests.TEST_GRAPH_SUFFIX}"


class DirectorySlack(FakeSlack):
    """FakeSlack with a team directory, members per channel and Slack Connect channels."""

    def __init__(self, run: str, team: str):
        super().__init__(run)
        self.team = team
        self.people: dict[str, SlackMember] = {}
        self.ext_shared: set[str] = set()

    def add(self, user_id: str, *, guest: bool = False, external: bool = False) -> None:
        self.people[user_id] = SlackMember(id=user_id, name=user_id, is_restricted=guest, team_id="TOTHER" if external else self.team)

    def list_slack_users_page(self, cursor=None, limit=200):
        return SlackCursorPage(ok=True, items=list(self.people.values()), complete=True)

    def conversation_info(self, channel):
        return SlackConversationInfo(id=channel, name=channel.lower(), is_private=channel in self.private, is_ext_shared=channel in self.ext_shared)

    get_slack_conversation_info = conversation_info

    def get_conversation_members(self, channel, max_members=1000):
        return list(self.members.get(channel, []))


class ChannelAudienceTest(unittest.TestCase):
    def setUp(self):
        self.run = uuid.uuid4().hex[:6].upper()
        self.team = f"TAUD{self.run}"
        self.slack = DirectorySlack(self.run, self.team)
        self.alice, self.guest = f"UALICE{self.run}", f"UGUEST{self.run}"
        self.slack.add("UBOT")
        self.slack.add(self.alice)
        self.public = f"CPUB{self.run}"
        self.slack.members[self.public] = ["UBOT", self.alice]

    def tearDown(self):
        for key in kv().keys(f"brain:slack:*{self.team}*"):
            kv().delete(key)

    def internal(self, channel=None, *, is_dm=False, asker=None):
        return channel_audience.is_internal_audience(self.slack, self.team, channel or self.public, is_dm=is_dm, asker=asker or self.alice)

    def test_members_only_channel_is_internal(self):
        self.assertTrue(self.internal())

    def test_a_guest_in_the_channel_makes_it_external(self):
        self.slack.add(self.guest, guest=True)
        self.slack.members[self.public].append(self.guest)
        self.assertFalse(self.internal())

    def test_a_guest_elsewhere_in_the_team_does_not(self):
        self.slack.add(self.guest, guest=True)
        self.assertTrue(self.internal())

    def test_slack_connect_and_strangers_are_external(self):
        self.slack.ext_shared.add(self.public)
        self.assertFalse(self.internal())
        self.slack.ext_shared.clear()
        channel_audience.forget(self.team, self.public)
        self.slack.add(self.guest, guest=True)  # the team has outsiders, so members are checked
        self.slack.members[self.public].append("USTRANGER")  # not in this team's directory
        self.assertFalse(self.internal())

    def test_someone_becoming_a_guest_drops_every_cached_verdict_of_the_team(self):
        other = f"COTHER{self.run}"
        self.slack.members[other] = ["UBOT", self.alice]
        self.assertTrue(self.internal())
        self.assertTrue(self.internal(other))
        # Alice becomes a guest (user_change): neither channel may keep its cached "internal".
        self.slack.add(self.alice, guest=True)
        self.assertTrue(self.internal(other, asker="UBOT"))  # still cached
        channel_audience.forget(self.team)
        self.assertFalse(self.internal(asker="UBOT"))
        self.assertFalse(self.internal(other, asker="UBOT"))

    def test_a_channel_shared_with_another_company_is_asked_again(self):
        self.assertTrue(self.internal())
        self.slack.ext_shared.add(self.public)
        channel_audience.forget(self.team, self.public)  # what channel_shared does
        self.assertFalse(self.internal())

    def test_a_guest_asking_is_external_even_in_a_dm(self):
        self.slack.add(self.guest, guest=True)
        self.assertFalse(self.internal(f"D{self.guest}", is_dm=True, asker=self.guest))
        self.assertTrue(self.internal(f"D{self.alice}", is_dm=True, asker=self.alice))

    def test_lookup_failure_fails_closed(self):
        self.slack.list_slack_users_page = lambda cursor=None, limit=200: SlackCursorPage(ok=False, error="ratelimited")
        self.assertFalse(self.internal())

    def test_external_audience_drops_org_internal_and_connector_scopes(self):
        inside = SlackMemoryScope(kind="shared", channel_id=self.public)
        self.assertIn(ORG_INTERNAL_CONTAINER_TAG, resolve_brain_read_container_tags(inside))
        outside = SlackMemoryScope(kind="shared", channel_id=self.public, internal_audience=False)
        self.assertNotIn(ORG_INTERNAL_CONTAINER_TAG, resolve_brain_read_container_tags(outside))
        dm = SlackMemoryScope(kind="dm", channel_id="D1", user_id="p1", slack_user_id=self.guest, internal_audience=False)
        with mock.patch("src.domain.memory.read_scope.readable_container_scopes_for_slack_user", return_value=["connector_x"]):
            tags = resolve_brain_read_container_tags(dm)
        self.assertNotIn(ORG_INTERNAL_CONTAINER_TAG, tags)
        self.assertNotIn("connector_x", tags)


class ApprovedActionAudienceTest(unittest.TestCase):
    """An approved write runs with the audience its request had: a guest or Slack Connect room
    never reads org_internal, so a decision cannot name (or link to) a connector account."""

    def test_approved_decision_from_an_external_room_does_not_read_org_internal(self):
        from src.domain.slack import compute

        graph_name = f"company_audience_approval_test{tests.TEST_GRAPH_SUFFIX}"
        with mock.patch.object(falkordb_client, "graph_name", return_value=graph_name), mock.patch.object(seed, "graph_name", return_value=graph_name):
            try:
                reset_and_seed(quillfern)
                _, g = falkordb_client.connect()
                g.query("CREATE (:Service {id: 'hubspot:default:company:9', name: 'Initech Secret Deal', kind: 'account', scope: 'org_internal', external_id: 'company:9'})")
                text = compute.execute_approved_action("record_decision", {"title": "Discount policy", "about": "initech"}, "org_shared", internal_audience=False)
                self.assertNotIn("Initech", text)
                linked = g.query("MATCH (d:Decision)-[:ABOUT]->(:Service {id: 'hubspot:default:company:9'}) RETURN count(d)").result_set[0][0]
                self.assertEqual(linked, 0)
            finally:
                falkordb_client.client().select_graph(graph_name).delete()


class AppMentionPrivacyTest(unittest.TestCase):
    def test_conversations_info_decides_when_channel_type_is_missing(self):
        public = SlackConversationInfo(id="CPUB", is_private=False)
        private = SlackConversationInfo(id="CPRIV", is_private=True)
        self.assertEqual(slack_memory_scope_for_turn(is_dm=False, channel="CPUB", conversation_info=public).kind, "shared")
        self.assertEqual(slack_memory_scope_for_turn(is_dm=False, channel="CPRIV", conversation_info=private).kind, "private_channel")
        # Unknown: fails closed, as before.
        self.assertEqual(slack_memory_scope_for_turn(is_dm=False, channel="CPUB").kind, "private_channel")
        # A live private event still wins over cached public info.
        self.assertEqual(slack_memory_scope_for_turn(is_dm=False, channel="CPUB", channel_type="group", conversation_info=public).kind, "private_channel")


class PublicDecisionByMentionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls.patches:
            patch.start()
        reset_and_seed(quillfern)

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in cls.patches:
                patch.stop()

    def setUp(self):
        self.run = uuid.uuid4().hex[:6].upper()
        self.slack = FakeSlack(self.run)
        self.alice, self.public = f"UALICE{self.run}", f"CPUB{self.run}"
        self.ws = SlackWorkspace(team_id="T1", bot_user_id="UBOT", bot_name="company-brain", client=self.slack)

    def tearDown(self):
        from src.core import scheduler
        from src.domain.slack.turn.approval_expiry import _job_id

        for _channel, card in self.slack.cards:
            scheduler.cancel(_job_id(card.approval_id))

    def test_decision_by_app_mention_in_a_public_channel_is_org_shared(self):
        title = f"move billing retries to the invoice worker {self.run.lower()}"
        ev = SlackEventInner(type="app_mention", user=self.alice, channel=self.public, channel_type=None, text=f"<@UBOT> decision: {title}", ts="3000.0001")
        turn_service.run_slack_turn(self.ws, ev)
        self.assertEqual(len(self.slack.cards), 1, self.slack.posts)
        approval_id = self.slack.cards[0][1].approval_id
        with mock.patch.object(turn_service, "update_interaction_response"), mock.patch.object(turn_service, "get_workspace_by_team_id", return_value=self.ws):
            turn_service.run_slack_approval_decision("T1", approval_id, True, self.alice, "https://example.invalid")
        self.assertEqual(approvals.load_approval(approval_id).status, "executed")
        _, graph = connect()
        rows = graph.query("MATCH (d:Decision) WHERE toLower(d.name) CONTAINS $run RETURN d.scope", {"run": self.run.lower()}).result_set
        self.assertEqual([row[0] for row in rows], ["org_shared"])
        private = graph.query("MATCH (c:Channel {id: $id}) RETURN c.is_private", {"id": self.public}).result_set
        self.assertFalse(private and private[0][0])


if __name__ == "__main__":
    unittest.main()
