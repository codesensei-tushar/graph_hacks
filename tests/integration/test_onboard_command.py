"""``onboard team:`` in Slack: a card that lists what it will create, then the write after Approve.

Seeds Quillfern into a throwaway graph and drives the command through ``compute_turn`` and
``execute_approved_action``, as a Slack turn does, with a fake Slack client for profiles.
Needs FalkorDB on localhost:6379 (``docker compose up -d``).
"""

import dataclasses
import re
import unittest
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.core import config  # noqa: E402
from src.db import falkordb_client  # noqa: E402
from src.domain.github.repos import readable_github_scopes_for_slack_user  # noqa: E402
from src.domain.knowledge import ask_api, ask_service, catalogue, mcp_tools, team_page  # noqa: E402
from src.domain.memory.writeback import SlackMemoryScope  # noqa: E402
from src.domain.slack import compute  # noqa: E402
from src.domain.slack.compute import TurnRequest  # noqa: E402
from src.integrations.slack.events import SlackEventInner  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_onboard_test{tests.TEST_GRAPH_SUFFIX}"
PUBLIC, PRIVATE = "CSALES", "GDEALDESK"
SALES = (
    "<@UBOT> onboard team: Sales | members: <@UPRIYA> <@UDEV> | owns: Renewals (process), HubSpot (system, <@UDEV>), "
    "Pricing playbook (playbook) | decision: annual plans only, decided 2026-09-01"
)


def _profile(user: str, name: str, **flags) -> SimpleNamespace:
    return SimpleNamespace(id=user, real_name=name, display_name="", name=name.split()[0].lower(), email=f"{user.lower()}@example.com", **flags)


PROFILES = {
    "UPRIYA": _profile("UPRIYA", "Priya Shah"),
    "UDEV": _profile("UDEV", "Dev Patel"),
    "UARJUN": _profile("UARJUN", "Arjun Mehta"),
    "UNEWKOFI": _profile("UNEWKOFI", "Kofi Mensah"),
    "UROBOT": _profile("UROBOT", "Build Bot", is_bot=True),
}
WORKSPACE = SimpleNamespace(team_id="TONBOARD", bot_user_id="UBOT", client=SimpleNamespace(user_info=PROFILES.get))


def _request(raw: str, asker: str = "UPRIYA", channel: str = PUBLIC, private: bool = False, dm: bool = False) -> TurnRequest:
    if dm:
        scope = SlackMemoryScope(kind="dm", channel_id="DPRIYA", channel_type="im", user_id="priya", slack_user_id=asker)
    elif private:
        scope = SlackMemoryScope(kind="private_channel", channel_id=channel, channel_type="group", slack_user_id=asker)
    else:
        scope = SlackMemoryScope(kind="shared", channel_id=channel, channel_type="channel", slack_user_id=asker)
    read_tags = ["org_shared", f"slack_channel_{channel}"] if private else ["org_shared"]
    event = SlackEventInner(type="app_mention", user=asker, channel=channel, channel_type="channel", text=raw, ts="1790000000.000100")
    question = " ".join(re.sub(r"<@[A-Z0-9]+(?:\|[^>]*)?>", "", raw).split())
    return TurnRequest(
        workspace=WORKSPACE, event=event, question=question, raw_text=raw, asker_slack_user_id=asker, asker_name="Priya",
        memory_scope=scope, read_tags=read_tags, thread=[], channel=channel, thread_ts=event.ts, is_dm=dm,
    )


def _approve(outcome) -> str:
    approval = outcome.approval
    return compute.execute_approved_action(approval.tool_name, approval.tool_input, approval.container_tag)


def _count(query: str, **params) -> int:
    _, graph = falkordb_client.connect()
    return int(graph.query(query, params).result_set[0][0])


class OnboardCommandTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
            # The team joins the company the instance runs; here that is Quillfern.
            mock.patch.object(config, "settings", dataclasses.replace(config.settings, seed_company="quillfern")),
        ]
        for patch in cls._patches:
            patch.start()

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def setUp(self):
        seed.reset_and_seed(quillfern)

    def test_the_tool_is_a_write_the_landing_page_sees_and_mcp_does_not(self):
        spec = catalogue.get_tool("onboard_team")
        self.assertTrue(spec.needs_approval)
        self.assertIn("onboard_team", ask_api.write_tools())
        self.assertNotIn("onboard_team", [tool.name for tool in mcp_tools.read_only_tools()])

    def test_the_command_waits_on_a_card_that_lists_everything_it_will_create(self):
        outcome = compute.compute_turn(_request(SALES))
        self.assertEqual(outcome.status, "suspended")
        self.assertEqual(outcome.approval.tool_name, "onboard_team")
        self.assertEqual(outcome.approval.container_tag, "org_shared")
        self.assertEqual(
            outcome.approval.summary,
            "Onboard *Sales*: 2 people (Priya Shah and Dev Patel), 1 system (HubSpot), 1 process (Renewals), 1 playbook (Pricing playbook), "
            "the decision *Annual plans only*, 1 Sep 2026. Owners: Priya Shah (Renewals and Pricing playbook); Dev Patel (HubSpot). "
            "Visible to public channels.",
        )
        self.assertEqual(_count("MATCH (t:Team {id: 'sales'}) RETURN count(t)"), 0, "nothing is written before Approve")

    def test_approve_writes_the_team_and_the_reply_cites_the_edges(self):
        text = _approve(compute.compute_turn(_request(SALES)))
        self.assertIn("Onboarded *Sales*: 2 people, 1 system, 1 process, 1 playbook, 1 decision.", text)
        for line in (
            "• Priya Shah -[MEMBER_OF]-> Sales",
            "• Sales -[RUNS]-> Renewals",
            "• Priya Shah -[OWNS]-> Renewals",
            "• Dev Patel -[OWNS]-> HubSpot",
            "• Annual plans only -[ABOUT]-> Renewals",
            "• Priya Shah -[PROPOSED]-> Annual plans only",
        ):
            self.assertIn(line, text)
        self.assertEqual(_count("MATCH (:SlackUser {id: 'UPRIYA'})-[:IS]->(p:Person {id: 'slack-upriya'}) RETURN count(p)"), 1)
        self.assertTrue(ask_service.ask("who runs renewals", ["org_shared"]).answer.startswith("The Renewals process is run by Priya Shah"))
        page = team_page.team_page("sales").to_dict()
        self.assertEqual(page["decisions"][0]["decided_on"], "1 Sep 2026")
        # Approving the same card twice writes nothing new.
        again = _approve(compute.compute_turn(_request(SALES)))
        self.assertIn("0 new nodes and 0 new edges", again)

    def test_the_launch_team_beat_reads_as_scripted(self):
        # The optional production beat in docs/STRATEGY.md, round 6: one member, who owns everything.
        _, graph = falkordb_client.connect()
        graph.query("MATCH (p:Person {id: 'tomas'}) MERGE (:SlackUser {id: 'UTOMAS'})-[:IS]->(p)")
        line = (
            "<@UBOT> onboard team: launch | members: <@UTOMAS> | owns: Hackathon submission (process), Demo video (product), "
            "Blog post (product), Recording script (playbook)"
        )
        outcome = compute.compute_turn(_request(line, asker="UTOMAS"))
        self.assertEqual(
            outcome.approval.summary,
            "Onboard *Launch*: 1 person (Tomas Reyes), 1 process (Hackathon submission), 2 products (Demo video and Blog post), "
            "1 playbook (Recording script). Tomas Reyes owns all of them. Visible to public channels.",
        )
        _approve(outcome)
        answer = ask_service.ask("who runs the hackathon submission", ["org_shared"]).answer
        self.assertTrue(answer.startswith("The Hackathon submission process is run by Tomas Reyes for the Launch team."), answer)

    def test_a_member_already_linked_is_that_person(self):
        _, graph = falkordb_client.connect()
        graph.query("MATCH (p:Person {id: 'arjun'}) MERGE (:SlackUser {id: 'UARJUN'})-[:IS]->(p)")
        _approve(compute.compute_turn(_request("<@UBOT> onboard team: Billing ops | members: <@UARJUN> | owns: Refund queue (process)", asker="UARJUN")))
        self.assertEqual(_count("MATCH (:Person {id: 'arjun'})-[:MEMBER_OF]->(:Team {id: 'billing-ops'}) RETURN count(*)"), 1)
        self.assertEqual(_count("MATCH (p:Person {name: 'Arjun Mehta'}) RETURN count(p)"), 1, "no second Arjun")
        self.assertEqual(_count("MATCH (:Person {id: 'arjun'})-[:OWNS]->(:Service {id: 'refund-queue'}) RETURN count(*)"), 1)

    def test_a_new_member_named_like_someone_here_does_not_get_their_access(self):
        # Kofi already reads a private repo through his linked GitHub account.
        _, graph = falkordb_client.connect()
        graph.query(
            "MATCH (p:Person {id: 'kofi'}) CREATE (:Repo {id: 9002, scope: 'github_repo_9002'})<-[:CAN_READ]-(:GitHubUser {id: 'gh-kofi', login: 'kofi-mensah'})-[:IS]->(p)"
        )
        _approve(compute.compute_turn(_request("<@UBOT> onboard team: Data guild | members: <@UNEWKOFI> | owns: Data reviews (process)", asker="UNEWKOFI")))
        linked = graph.query("MATCH (:SlackUser {id: 'UNEWKOFI'})-[:IS]->(p:Person) RETURN p.id").result_set
        self.assertEqual(linked, [["slack-unewkofi"]], "a mention with no person becomes a new one, never Kofi by name")
        self.assertEqual(readable_github_scopes_for_slack_user("UNEWKOFI"), [])
        # The check would catch it: a Slack user really linked to Kofi does read the repo.
        graph.query("MATCH (p:Person {id: 'kofi'}) MERGE (:SlackUser {id: 'UKOFI'})-[:IS]->(p)")
        self.assertEqual(readable_github_scopes_for_slack_user("UKOFI"), ["github_repo_9002"])

    def test_a_private_channel_keeps_the_team_to_that_channel(self):
        outcome = compute.compute_turn(_request(SALES, channel=PRIVATE, private=True))
        self.assertEqual(outcome.approval.container_tag, f"slack_channel_{PRIVATE}")
        _approve(outcome)
        _, graph = falkordb_client.connect()
        scopes = dict(graph.query("MATCH (n) WHERE n:Team OR n:Service OR n:Decision WITH n WHERE n.id IN ['sales', 'renewals', 'decision-annual-plans-only'] RETURN n.id, n.scope").result_set)
        self.assertEqual(set(scopes.values()), {f"slack_channel_{PRIVATE}"}, scopes)
        self.assertEqual(graph.query("MATCH (p:Person {id: 'slack-upriya'}) RETURN p.scope").result_set, [[None]], "people belong to the whole company")
        self.assertIsNone(team_page.team_page("sales"), "the public page does not see a private team")
        refused = compute.compute_turn(_request(SALES))
        self.assertEqual(refused.status, "done")
        self.assertIn("'Sales' is already in Company Brain for another channel; onboard it from there", refused.reply)

    def test_a_bad_line_is_answered_with_what_to_fix_and_writes_nothing(self):
        before = _count("MATCH (n) RETURN count(n)")
        bad_kind = compute.compute_turn(_request("<@UBOT> onboard team: Sales | members: <@UPRIYA> | owns: Renewals (department)"))
        self.assertEqual(bad_kind.status, "done")
        self.assertEqual(
            bad_kind.reply,
            "I can't onboard that yet: kind in 'Renewals' is 'department'; use one of service, system, process, product, account, policy, playbook.",
        )
        taken = compute.compute_turn(_request("<@UBOT> onboard team: Sales | members: <@UPRIYA> | owns: Billing Service (service)"))
        self.assertIn("'billing-service' is already run by Payments", taken.reply)
        names = compute.compute_turn(_request("<@UBOT> onboard team: Sales | members: priya dev"))
        self.assertIn("mention each member with @", names.reply)
        robot = compute.compute_turn(_request("<@UBOT> onboard team: Sales | members: <@UROBOT>"))
        self.assertIn("<@UROBOT> is not in Company Brain yet", robot.reply)
        dm = compute.compute_turn(_request(SALES, dm=True))
        self.assertEqual(dm.reply, "Onboard a team from a channel, so the people in it can see it.")
        self.assertEqual(_count("MATCH (n) RETURN count(n)"), before)


class OnboardThroughSlackTest(unittest.TestCase):
    """The same command through the real routes: a signed event, the card, the Approve click."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        from scripts.replay.driver import WorkspaceReplayer, clear_team_state, fake_workspace_from_script, register_fake_workspace, use_agent_loop
        from scripts.replay.slack_api import FakeSlackServer
        from src.domain.slack import workspace as workspace_module
        from src.integrations.slack import client as slack_client_module
        from src.main import app
        from src.seed import quillfern_workspace as script
        from src.seed.quillfern_sales import seed_sales

        cls.script, cls.clear_team_state, cls.use_agent_loop = script, clear_team_state, use_agent_loop
        cls.fake = fake_workspace_from_script()
        cls.server = FakeSlackServer(cls.fake).start()
        cls.patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=f"{GRAPH}_routes"),
            mock.patch.object(seed, "graph_name", return_value=f"{GRAPH}_routes"),
            mock.patch.object(config, "settings", dataclasses.replace(config.settings, seed_company="quillfern")),
            mock.patch.object(slack_client_module, "settings", dataclasses.replace(config.settings, slack_api_base_url=cls.server.base_url)),
        ]
        for patch in cls.patches:
            patch.start()
        if hasattr(workspace_module._client_for, "cache_clear"):
            workspace_module._client_for.cache_clear()
        seed.reset_and_seed(quillfern)
        seed_sales()
        register_fake_workspace()
        use_agent_loop()
        cls.replayer = WorkspaceReplayer(
            TestClient(app), signing_secret=config.settings.slack_signing_secret, fake=cls.fake,
            fake_base_url=cls.server.base_url, response_url=cls.server.response_url("onboard"),
        )
        line = "onboard team: Deal desk | members: <@UPRIYA> <@UNOOR> | owns: Quote approvals (process), Discount desk (playbook, <@UNOOR>)"
        step = script.DemoStep("Onboard", (script.WorkspaceMessage(script.SALES, "UPRIYA", line, "ask"), script.WorkspaceMessage(script.SALES, "UPRIYA", "", "approve")))
        ((_, cls.rows),) = cls.replayer.play_steps((step,))

    @classmethod
    def tearDownClass(cls):
        try:
            cls.clear_team_state(cls.replayer)
            falkordb_client.client().select_graph(f"{GRAPH}_routes").delete()
        finally:
            cls.use_agent_loop(False)
            cls.server.stop()
            for patch in cls.patches:
                patch.stop()

    def test_the_card_comes_first_and_the_approved_reply_cites_the_edges(self):
        asked, approved = self.rows
        self.assertEqual((asked.status, asked.replies), (200, []), "the command waits on its card")
        self.assertIsNotNone(self.replayer.last_approval_id(self.script.SALES))
        self.assertEqual(len(approved.replies), 1, approved.replies)
        reply = approved.replies[0]
        self.assertIn("Onboarded *Deal desk*: 2 people, 1 process, 1 playbook. ", reply)
        for line in ("• Priya Shah -[MEMBER_OF]-> Deal desk", "• Priya Shah -[OWNS]-> Quote approvals", "• Noor Haddad -[OWNS]-> Discount desk"):
            self.assertIn(line, reply)
        # Priya and Noor were already linked by the Sales import: the same people, not new ones.
        self.assertEqual(_count("MATCH (p:Person)-[:MEMBER_OF]->(:Team {id: 'deal-desk'}) WHERE p.id IN ['priya', 'noor'] RETURN count(p)"), 2)


if __name__ == "__main__":
    unittest.main()
