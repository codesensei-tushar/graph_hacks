"""The seven-step demo and the Sales beat from docs/STRATEGY.md, end to end through the real routes.

Boots the app against a fake Slack Web API (``scripts/replay/slack_api.py``) and a fake
GitHub client, seeds the fictional Quillfern company into a throwaway graph, imports its
Sales team from examples/teams/quillfern-sales.toml through the team importer, replays
the scripted workspace history and repos, then plays the demo steps in order and
asserts on what the bot posted back. No model: answers come from the graph tools and
retained memories, exactly as the no-key deployment behaves.

Steps that need the agent loop (depth follow-ups, thread context, nearest-match
wording) are marked as pending until it lands, so this file is the contract they
have to satisfy.
"""

import dataclasses
import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from fastapi.testclient import TestClient  # noqa: E402

from src.api.v1 import github as github_routes  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.db import falkordb_client  # noqa: E402
from src.domain.github import backfill, webhooks  # noqa: E402
from src.domain.slack import workspace as workspace_module  # noqa: E402
from src.integrations.slack import client as slack_client_module  # noqa: E402
from src.main import app  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402
from src.seed import quillfern_workspace as script  # noqa: E402
from src.seed.quillfern_sales import seed_sales  # noqa: E402
from scripts.replay.driver import (  # noqa: E402
    WorkspaceReplayer,
    clear_team_state,
    fake_workspace_from_script,
    register_fake_workspace,
    use_agent_loop,
)
from scripts.replay.github_api import FakeGitHubClient  # noqa: E402
from scripts.replay.slack_api import FakeSlackServer  # noqa: E402

GRAPH = f"company_demo_replay_test{tests.TEST_GRAPH_SUFFIX}"
WEBHOOK_SECRET = "replay-webhook-secret"
# The team's thread turns and approvals live in Redis, which every checkout's suite shares;
# its own team id keeps another run's clear_team_state from wiping an approval mid-replay.
TEAM = f"{script.TEAM_ID}{tests.TEST_GRAPH_SUFFIX.upper()}"


def edge(source: str, rel: str, target: str) -> str:
    """One cited edge as the reply renders it."""
    return f"• {source} -[{rel}]-> {target}"


class DemoReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = fake_workspace_from_script(TEAM)
        cls.server = FakeSlackServer(cls.fake).start()
        slack_settings = dataclasses.replace(settings, slack_api_base_url=cls.server.base_url)
        github_settings = dataclasses.replace(settings, github_app_id="1", github_webhook_secret=WEBHOOK_SECRET, github_private_key="unused")
        github_client = FakeGitHubClient(script.REPOS, script.INSTALLATION_ID)
        cls.patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
            mock.patch.object(slack_client_module, "settings", slack_settings),
            mock.patch.object(github_routes, "settings", github_settings),
            mock.patch.object(backfill, "client_for", return_value=github_client),
            mock.patch.object(webhooks, "client_for", return_value=github_client),
        ]
        for patch in cls.patches:
            patch.start()
        # Clients are cached per token; drop any built before the base URL was patched.
        if hasattr(workspace_module._client_for, "cache_clear"):
            workspace_module._client_for.cache_clear()

        seed.reset_and_seed(quillfern)
        seed_sales()
        register_fake_workspace(TEAM)
        use_agent_loop()
        cls.http = TestClient(app)
        cls.replayer = WorkspaceReplayer(
            cls.http,
            signing_secret=settings.slack_signing_secret,
            team_id=TEAM,
            fake=cls.fake,
            fake_base_url=cls.server.base_url,
            response_url=cls.server.response_url("test"),
        )
        assert cls.replayer.post_installation_event(WEBHOOK_SECRET) == 200
        cls.history = cls.replayer.play(script.HISTORY)
        cls.steps = dict((step.title, rows) for step, rows in cls.replayer.play_steps(script.DEMO_STEPS))

    @classmethod
    def tearDownClass(cls):
        try:
            clear_team_state(cls.replayer)
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            use_agent_loop(False)
            cls.server.stop()
            for patch in cls.patches:
                patch.stop()

    def _rows(self, title: str):
        return self.steps[title]

    def _only_reply(self, row) -> str:
        self.assertEqual(row.status, 200, row)
        self.assertEqual(len(row.replies), 1, f"expected one reply to {row.message.text!r}, got {row.replies}")
        return row.replies[0]

    def assert_cites_path(self, reply: str) -> None:
        self.assertIn("*Path*", reply)
        self.assertRegex(reply, r"• .+ -\[[A-Z_]+\]-> .+")

    # --- what the replay itself did ----------------------------------------------------------------

    def test_history_is_accepted_and_kept_as_memory(self):
        self.assertTrue(all(row.status == 200 for row in self.history))
        _, graph = falkordb_client.connect()
        count = graph.query("MATCH (m:Memory) RETURN count(m)").result_set[0][0]
        self.assertGreaterEqual(count, len(script.HISTORY))
        private = graph.query("MATCH (m:Memory {channel_id: $c}) RETURN DISTINCT m.scope", {"c": script.PAYMENTS}).result_set
        self.assertEqual(private, [[f"slack_channel_{script.PAYMENTS}"]])

    def test_repos_landed_on_the_right_services(self):
        _, graph = falkordb_client.connect()
        rows = graph.query(
            "MATCH (p:PullRequest)-[:TOUCHES]->(s:Service) RETURN p.name, p.scope, collect(s.id) ORDER BY p.name"
        ).result_set
        touched = {name: (scope, sorted(services)) for name, scope, services in rows}
        self.assertEqual(touched["notebook#52"], ("org_shared", ["billing-service", "invoice-worker"]))
        self.assertEqual(touched["billing-internal#7"], ("github_repo_9002", ["billing-service"]))
        linked = graph.query("MATCH (g:GitHubUser)-[:IS]->(p:Person) RETURN count(p)").result_set[0][0]
        self.assertEqual(linked, len(script.GITHUB_USERS))

    def test_every_answer_came_through_the_agent_loop(self):
        # Approved writes run after the Approve click, outside the loop, so they carry no footer.
        answers = [reply for rows in self.steps.values() for row in rows if row.message.kind != "approve" for reply in row.replies]
        self.assertGreaterEqual(len(answers), 7)
        for reply in answers:
            self.assertRegex(reply, r"_Tool `[a-z_]+`, picked by (keywords|the model)\.", reply)

    def test_every_slack_call_went_to_the_fake_with_the_stored_token(self):
        self.assertTrue(self.fake.calls_to("chat.postMessage"))
        self.assertTrue(self.fake.calls_to("users.info"))
        self.assertNotIn("invalid_auth", {m.text for m in self.fake.messages})

    # --- the seven demo steps -------------------------------------------------------------------------

    def test_1_who_owns_billing_and_who_else_worked_on_it(self):
        reply = self._only_reply(self._rows("Who owns billing, and who else has worked on it")[0])
        self.assert_cites_path(reply)
        self.assertIn("Billing Service is owned by Arjun Mehta", reply)
        for line in (
            edge("Arjun Mehta", "OWNS", "Billing Service"),
            edge("Lena Fischer", "WORKED_ON", "Billing Service"),
            edge("Sara Lindqvist", "KNOWS", "Billing Service"),
            edge("notebook#52", "TOUCHES", "Billing Service"),
            edge("Lena Fischer", "AUTHORED", "notebook#52"),
            edge("Arjun Mehta", "REVIEWED", "notebook#52"),
        ):
            self.assertIn(line, reply)
        # The private repo's pull request is not visible from a public channel.
        self.assertNotIn("billing-internal#7", reply)

    def test_2_why_the_memory_layer_left_postgres(self):
        reply = self._only_reply(self._rows("Trace a decision to its source")[0])
        self.assert_cites_path(reply)
        decision = "Move the memory layer off Postgres"
        self.assertIn("Proposed by Kofi Mensah", reply)
        self.assertIn("Approved by Tomas Reyes", reply)
        self.assertIn("This replaced *Keep notebook links in Postgres join tables* (decided 1 Jun 2026)", reply)
        for line in (
            edge("Kofi Mensah", "PROPOSED", decision),
            edge(decision, "BASED_ON", "RFC: memory layer on a graph store"),
            edge(decision, "BASED_ON", "TKT-101"),
            edge(decision, "SUPERSEDES", "Keep notebook links in Postgres join tables"),
        ):
            self.assertIn(line, reply)
        # Retained channel talk about the same move comes back as a source too.
        self.assertIn("*From conversations*", reply)
        self.assertIn("Kofi owns the migration", reply)

    def test_3_a_plan_change_in_the_channel_is_recorded_after_approval(self):
        said, recorded, approved, asked = self._rows("A plan changes in the channel")
        self.assertEqual(said.status, 200)
        self.assertEqual(said.replies, [], "a plain channel message gets no reply without a model")
        self.assertEqual(recorded.replies, [], "recording a decision waits for approval")
        reply = self._only_reply(approved)
        self.assertIn("Recorded *Move billing retries to the invoice worker*", reply)
        self.assertIn("replacing *Retry renewal webhooks inside the billing service* (decided 20 Sep 2026)", reply)
        self.assertIn(edge("Arjun Mehta", "PROPOSED", "Move billing retries to the invoice worker"), reply)
        self.assertIn(edge("Move billing retries to the invoice worker", "DECIDED_IN", "#engineering"), reply)

    def test_3b_the_next_why_answer_shows_the_new_decision_what_it_replaced_and_when(self):
        reply = self._only_reply(self._rows("A plan changes in the channel")[3])
        self.assert_cites_path(reply)
        new, old = "Move billing retries to the invoice worker", "Retry renewal webhooks inside the billing service"
        self.assertTrue(reply.startswith(new), reply)
        said = "we're moving billing retries to the invoice worker, decided in today's sync"
        self.assertRegex(reply, rf"Decided in #engineering on \d{{1,2}} \w{{3}} 20\d\d by Arjun Mehta: “{said}”\.")
        self.assertIn(f"This replaced *{old}* (decided 20 Sep 2026)", reply)
        for line in (
            edge("Arjun Mehta", "PROPOSED", new),
            edge(new, "ABOUT", "Billing Service"),
            edge(new, "SUPERSEDES", old),
            edge(new, "DECIDED_IN", "#engineering"),
            edge(new, "RECORDED_FROM", f"“{said}”"),
            edge("Tomas Reyes", "APPROVED", old),
        ):
            self.assertIn(line, reply)

    def test_4_blast_radius_then_two_hops_out(self):
        first, deeper = self._rows("Blast radius, then two hops out")
        reply = self._only_reply(first)
        self.assert_cites_path(reply)
        self.assertIn("If Auth Service goes down", reply)
        self.assertIn("depth 1", reply)
        self.assertIn("1 more breaks further out (Invoice Worker at depth 2); ask with more hops.", reply)
        for line in (
            edge("API Gateway", "DEPENDS_ON", "Auth Service"),
            edge("Billing Service", "DEPENDS_ON", "Auth Service"),
        ):
            self.assertIn(line, reply)
        self.assertEqual(deeper.status, 200)
        self.assertEqual(len(deeper.replies), 1, "a follow-up that mentions the bot in the thread is answered")

    def test_4b_two_hops_out_reaches_a_service_the_first_answer_missed(self):
        first, deeper = self._rows("Blast radius, then two hops out")
        self.assertNotIn(edge("Invoice Worker", "DEPENDS_ON", "Billing Service"), self._only_reply(first))
        reply = self._only_reply(deeper)
        self.assertIn(edge("Invoice Worker", "DEPENDS_ON", "Billing Service"), reply)
        self.assertIn("If Auth Service goes down", reply)
        self.assertIn("depth 2", reply)
        self.assertIn("Subject from this thread", reply)

    def test_5_a_ticket_is_filed_after_approval(self):
        asked, approved, follow_up = self._rows("A new ticket, approved, then who should look at it")
        self.assertEqual(asked.status, 200)
        self.assertEqual(asked.replies, [], "a graph write waits for approval instead of answering")
        self.assertIsNotNone(self.replayer.last_approval_id(script.ENGINEERING))
        self.assertEqual(approved.status, 200)
        reply = self._only_reply(approved)
        self.assert_cites_path(reply)
        self.assertIn("Filed *TKT-104* about Billing Service", reply)
        self.assertIn("Arjun Mehta", reply)
        self.assertIn(edge("TKT-104", "ABOUT", "Billing Service"), reply)
        self.assertTrue(self.fake.response_url_posts, "the card is updated through its response_url")
        self.assertEqual(follow_up.status, 200)
        self.assertEqual(len(follow_up.replies), 1, follow_up.replies)

    def test_5b_who_should_look_at_it_resolves_the_threads_ticket(self):
        reply = self._only_reply(self._rows("A new ticket, approved, then who should look at it")[2])
        self.assertIn(edge("Arjun Mehta", "OWNS", "Billing Service"), reply)
        self.assertIn("Subject from this thread", reply)

    def test_6_something_not_in_the_graph(self):
        reply = self._only_reply(self._rows("Something the graph does not know")[0])
        # Whatever else it says, the reply names the nearest things the graph does know.
        self.assertTrue("Billing Service" in reply or "API Gateway" in reply, reply)

    def test_6b_the_reply_says_the_graph_does_not_know_it(self):
        reply = self._only_reply(self._rows("Something the graph does not know")[0])
        self.assertIn("payments gateway", reply.lower())
        self.assertRegex(reply, r"(?i)(nothing|no|not|don't|doesn't).*(match|know)")

    def test_7_a_private_repo_is_seen_only_by_its_collaborator(self):
        outsider, collaborator = self._rows("A private repo, seen only by its collaborator")
        outsider_reply = self._only_reply(outsider)
        collaborator_reply = self._only_reply(collaborator)
        self.assertIn("Billing Service is owned by Arjun Mehta", outsider_reply)
        self.assertNotIn("billing-internal#7", outsider_reply)
        self.assertIn(edge("billing-internal#7", "TOUCHES", "Billing Service"), collaborator_reply)
        self.assertIn(edge("Arjun Mehta", "AUTHORED", "billing-internal#7"), collaborator_reply)
        self.assertIn("quillfern/billing-internal", collaborator_reply)

    # --- the Sales beat: the same tools, for a team imported from a team file ------------------------

    def test_s1_who_owns_renewals_and_who_else_worked_on_it(self):
        reply = self._only_reply(self._rows("S1 Who owns renewals, and who else has worked on it")[0])
        self.assert_cites_path(reply)
        self.assertIn("_Tool `who_knows`", reply)
        self.assertTrue(
            reply.startswith("The Renewals process is run by Priya Shah. Ask first: Priya Shah (owns it), then Dev Patel (worked on it) and Noor Haddad (worked on it)."),
            reply,
        )
        for line in (
            edge("Priya Shah", "OWNS", "Renewals"),
            edge("Dev Patel", "WORKED_ON", "Renewals"),
            edge("Noor Haddad", "WORKED_ON", "Renewals"),
        ):
            self.assertIn(line, reply)

    def test_s2_a_crm_outage_reaches_the_process_and_the_playbook(self):
        reply = self._only_reply(self._rows("S2 What breaks if HubSpot goes down")[0])
        self.assert_cites_path(reply)
        self.assertIn("_Tool `blast_radius`", reply)
        self.assertIn(
            "If the HubSpot system goes down, the Renewals process stalls (1 hop) and the Pricing playbook no longer applies (2 hops). "
            "Depth 2 reached 2 things that depend on it; nothing new past depth 2.",
            reply,
        )
        self.assertIn(edge("Renewals", "DEPENDS_ON", "HubSpot"), reply)
        self.assertIn(edge("Pricing playbook", "DEPENDS_ON", "Renewals"), reply)
        self.assertNotIn("Enterprise accounts", reply)

    def test_s3_why_pricing_changed_names_the_current_decision_and_the_one_it_replaced(self):
        reply = self._only_reply(self._rows("S3 Why did we change pricing")[0])
        self.assert_cites_path(reply)
        self.assertIn("_Tool `decision_trace`", reply)
        new, old = "Sell annual plans only at renewal", "Discount monthly plans at renewal"
        self.assertTrue(reply.startswith(new), reply)
        self.assertIn("Decided 1 Sep 2026", reply)
        self.assertIn(f"This replaced *{old}* (decided 2 Mar 2026)", reply)
        for line in (
            edge("Noor Haddad", "PROPOSED", new),
            edge("Priya Shah", "APPROVED", new),
            edge(new, "BASED_ON", "Pricing playbook"),
            edge(new, "SUPERSEDES", old),
        ):
            self.assertIn(line, reply)

    def test_the_sales_team_came_through_the_importer(self):
        _, graph = falkordb_client.connect()
        runs = dict(graph.query("MATCH (:Team {id: 'sales'})-[:RUNS]->(s:Service) RETURN s.id, s.kind").result_set)
        self.assertEqual(runs, {"renewals": "process", "hubspot": "system", "pricing-playbook": "playbook", "enterprise-accounts": "account"})
        linked = graph.query("MATCH (u:SlackUser {id: 'UPRIYA'})-[:IS]->(p:Person) RETURN p.id").result_set
        self.assertEqual(linked, [["priya"]])
        # Additive: the Sales graph does not touch an engineering service.
        crossing = graph.query(
            "MATCH (:Team {id: 'sales'})-[:RUNS]->(:Service)-[:DEPENDS_ON]-(s:Service)<-[:RUNS]-(t:Team) WHERE t.id <> 'sales' RETURN count(s)"
        ).result_set[0][0]
        self.assertEqual(crossing, 0)


if __name__ == "__main__":
    unittest.main()
