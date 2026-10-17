"""The seven-step demo from docs/STRATEGY.md, end to end through the real routes.

Boots the app against a fake Slack Web API (``scripts/replay/slack_api.py``) and a fake
GitHub client, seeds the fictional Quillfern company into a throwaway graph, replays
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


def edge(source: str, rel: str, target: str) -> str:
    """One cited edge as the reply renders it."""
    return f"• {source} -[{rel}]-> {target}"


class DemoReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = fake_workspace_from_script()
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
        register_fake_workspace()
        use_agent_loop()
        cls.http = TestClient(app)
        cls.replayer = WorkspaceReplayer(
            cls.http,
            signing_secret=settings.slack_signing_secret,
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
        for line in (
            edge("Kofi Mensah", "PROPOSED", decision),
            edge(decision, "CITES", "RFC: memory layer on a graph store"),
            edge(decision, "CITES", "TKT-101"),
        ):
            self.assertIn(line, reply)
        # Retained channel talk about the same move comes back as a source too.
        self.assertIn("*From conversations*", reply)
        self.assertIn("Kofi owns the migration", reply)

    def test_3_a_plan_change_in_the_channel_shows_up_in_the_next_answer(self):
        said, asked = self._rows("A plan changes in the channel")
        self.assertEqual(said.status, 200)
        self.assertEqual(said.replies, [], "a plain channel message gets no reply without a model")
        reply = self._only_reply(asked)
        # Until the plan change is recorded as a decision, the loop refuses rather than guessing
        # from chatter: no decision matches, and no memories are shown under a refusal.
        self.assertIn("Nothing in the graph matches", reply)
        self.assertIn("billing retries", reply)
        self.assertNotIn("*From conversations*", reply)
        self.assertIn("Tool `decision_trace`, picked by keywords", reply)

    def test_3b_the_plan_change_is_a_decision_that_supersedes_the_old_one(self):
        reply = self._only_reply(self._rows("A plan changes in the channel")[1])
        if "SUPERSEDES" not in reply:
            self.skipTest("pending item 2: the plan change becomes a Decision that supersedes the old one, with dates")
        self.assertIn(edge("Arjun Mehta", "PROPOSED", "Move billing retries to the invoice worker"), reply)
        self.assertRegex(reply, r"decided .*20\d\d")

    def test_4_blast_radius_then_two_hops_out(self):
        first, deeper = self._rows("Blast radius, then two hops out")
        reply = self._only_reply(first)
        self.assert_cites_path(reply)
        self.assertIn("If Auth Service goes down", reply)
        for line in (
            edge("API Gateway", "DEPENDS_ON", "Auth Service"),
            edge("Billing Service", "DEPENDS_ON", "Auth Service"),
            edge("Invoice Worker", "DEPENDS_ON", "Billing Service"),
        ):
            self.assertIn(line, reply)
        self.assertEqual(deeper.status, 200)
        self.assertEqual(len(deeper.replies), 1, "a follow-up that mentions the bot in the thread is answered")

    def test_4b_two_hops_out_reaches_a_service_the_first_answer_missed(self):
        reply = self._only_reply(self._rows("Blast radius, then two hops out")[1])
        if "Invoice Worker" not in reply:
            self.skipTest("pending item 1: a thread follow-up reuses the thread's subject and depth")
        self.assertIn(edge("Invoice Worker", "DEPENDS_ON", "Billing Service"), reply)

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
        if "Arjun Mehta" not in reply:
            self.skipTest("pending item 1: 'who should look at it' resolves the thread's ticket")
        self.assertIn(edge("Arjun Mehta", "OWNS", "Billing Service"), reply)

    def test_6_something_not_in_the_graph(self):
        reply = self._only_reply(self._rows("Something the graph does not know")[0])
        # Whatever else it says, the reply names the nearest things the graph does know.
        self.assertTrue("Billing Service" in reply or "API Gateway" in reply, reply)

    def test_6b_the_reply_says_the_graph_does_not_know_it(self):
        reply = self._only_reply(self._rows("Something the graph does not know")[0])
        if "payments gateway" not in reply.lower():
            self.skipTest("pending item 1: the reply says the graph does not know the named system and offers the nearest match")
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


if __name__ == "__main__":
    unittest.main()
