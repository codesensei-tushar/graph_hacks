"""The Slack turn around the agent against a real FalkorDB seeded with the fictional Quillfern company.

The model is mocked to return a tool choice (or to be unavailable); everything after
the choice is real: resolution, the traversal, the cited path, the approval flow.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import agent  # noqa: E402
from src.domain.memory.read_scope import resolve_brain_read_container_tags  # noqa: E402
from src.domain.memory.writeback import SlackMemoryScope  # noqa: E402
from src.domain.slack import agent_turn, compute  # noqa: E402
from src.domain.slack.compute import TurnRequest  # noqa: E402
from src.integrations.llm.model import ModelUnavailable  # noqa: E402
from src.integrations.slack.events import SlackEventInner  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_agent_test{tests.TEST_GRAPH_SUFFIX}"
CHANNEL = "CAGENT"


BOT = SimpleNamespace(team_id="T1", bot_user_id="UBOT")


def _request(question: str, thread: list[dict] | None = None) -> TurnRequest:
    scope = SlackMemoryScope(kind="shared", channel_id=CHANNEL, channel_type="channel")
    event = SlackEventInner(type="app_mention", user="UALICE", channel=CHANNEL, channel_type="channel", text=question, ts="1700000000.000100")
    return TurnRequest(
        workspace=BOT,
        event=event,
        question=question,
        raw_text=question,
        asker_slack_user_id="UALICE",
        asker_name="Alice",
        memory_scope=scope,
        read_tags=resolve_brain_read_container_tags(scope),
        thread=thread or [],
        channel=CHANNEL,
        thread_ts=event.ts,
        is_dm=False,
    )


def _model_picks(tool: str, args: dict):
    return mock.patch.object(agent, "generate_object", return_value={"tool": tool, "args": args})


class KnowledgeAgentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def test_the_model_picks_who_owns_and_the_reply_cites_the_edge(self):
        with _model_picks("who_owns", {"name": "billing"}):
            outcome = agent_turn.answer_turn(_request("who owns billing"))
        self.assertEqual(outcome.status, "done")
        self.assertIn("Billing Service is owned by Arjun Mehta", outcome.reply)
        self.assertIn("• Arjun Mehta -[OWNS]-> Billing Service", outcome.reply)
        self.assertIn("• Payments -[RUNS]-> Billing Service", outcome.reply)
        self.assertIn("Tool `who_owns`, picked by the model", outcome.reply)

    def test_without_a_model_the_keyword_router_answers_the_same_question(self):
        with mock.patch.object(agent, "generate_object", side_effect=ModelUnavailable("no key")):
            outcome = agent_turn.answer_turn(_request("what breaks if the auth service goes down"))
        self.assertIn("If Auth Service goes down", outcome.reply)
        self.assertIn("• Billing Service -[DEPENDS_ON]-> Auth Service", outcome.reply)
        self.assertIn("• Invoice Worker -[DEPENDS_ON]-> Billing Service", outcome.reply)
        self.assertIn("Tool `blast_radius`, picked by keywords", outcome.reply)

    def test_a_follow_up_in_a_thread_reaches_the_model_with_the_thread(self):
        thread = [{"user": "UALICE", "text": "<@UBOT> what breaks if auth goes down", "ts": "1"}, {"user": "UBOT", "bot_id": "B1", "text": "If Auth Service goes down...", "ts": "2"}]
        with _model_picks("blast_radius", {"name": "auth"}) as model:
            outcome = agent_turn.answer_turn(_request("and the full chain please", thread=thread))
        self.assertIn("UALICE: <@UBOT> what breaks if auth goes down", model.call_args.args[0].prompt)
        self.assertIn("app: If Auth Service goes down", model.call_args.args[0].prompt)
        self.assertIn("Invoice Worker", outcome.reply)

    def test_something_not_in_the_graph_is_refused_with_the_nearest_names(self):
        with _model_picks("who_knows", {"name": "payments gateway"}):
            outcome = agent_turn.answer_turn(_request("who knows the payments gateway"))
        self.assertIn("Nothing in the graph matches “payments gateway” as one thing", outcome.reply)
        self.assertIn("Nearest: Billing Service (known as “payments”), API Gateway (known as “gateway”)", outcome.reply)
        self.assertNotIn("*Path*", outcome.reply)
        self.assertIn("Tool `who_knows`, picked by the model", outcome.reply)

    def test_a_write_suspends_for_approval_and_runs_through_the_catalogue_after_it(self):
        with _model_picks("ingest_ticket", {"title": "Billing exports time out", "about": "billing"}):
            outcome = agent_turn.answer_turn(_request("file a ticket: billing exports time out"))
        self.assertEqual(outcome.status, "suspended")
        approval = outcome.approval
        self.assertEqual(approval.tool_name, "ingest_ticket")
        self.assertEqual(approval.tool_input["asker_slack_user_id"], "UALICE")
        self.assertEqual(approval.tool_input["channel_id"], CHANNEL)
        self.assertEqual(approval.tool_input["decided_at"], 1_700_000_000_000)
        self.assertEqual(approval.summary, "File a ticket titled *Billing exports time out* in the company graph, linked to *Billing Service*. Visible to public channels.")
        _, graph = falkordb_client.connect()
        self.assertEqual(graph.query("MATCH (t:Ticket {id: 'TKT-104'}) RETURN count(t)").result_set[0][0], 0)

        text = compute.execute_approved_action(approval.tool_name, approval.tool_input, approval.container_tag)
        self.assertIn("Filed *TKT-104* about Billing Service", text)
        self.assertIn("• TKT-104 -[ABOUT]-> Billing Service", text)
        self.assertIn("Arjun Mehta", text)

    def test_a_follow_up_takes_its_subject_and_depth_from_the_thread(self):
        with mock.patch.object(agent, "generate_object", side_effect=ModelUnavailable("no key")):
            first = agent_turn.answer_turn(_request("what breaks if auth goes down, directly"))
            thread = [
                {"user": "USARA", "text": "<@UBOT> what breaks if auth goes down, directly", "ts": "1"},
                {"user": "UBOT", "bot_id": "B1", "text": first.reply, "ts": "2"},
                {"user": "USARA", "text": "<@UBOT> and two hops out", "ts": "3"},
            ]
            deeper = agent_turn.answer_turn(_request("and two hops out", thread=thread))
        self.assertIn("depth 1", first.reply)
        self.assertNotIn("• Invoice Worker -[DEPENDS_ON]-> Billing Service", first.reply)
        self.assertIn("If Auth Service goes down", deeper.reply)
        self.assertIn("depth 2", deeper.reply)
        self.assertIn("• Invoice Worker -[DEPENDS_ON]-> Billing Service", deeper.reply)
        self.assertIn("Tool `blast_radius`, picked by keywords. Subject from this thread.", deeper.reply)

    def test_who_should_look_at_it_resolves_the_threads_ticket(self):
        thread = [
            {"user": "UKOFI", "text": "<@UBOT> ticket: annual plan renewals charge twice", "ts": "1"},
            {"user": "UBOT", "bot_id": "B1", "text": "Filed *TKT-102* about Billing Service. Billing Service is owned by Arjun Mehta.\n\n_Tool `ingest_ticket`, run after your approval. Read from: public channels_", "ts": "2"},
            {"user": "UKOFI", "text": "<@UBOT> who should look at it", "ts": "3"},
        ]
        with mock.patch.object(agent, "generate_object", side_effect=ModelUnavailable("no key")):
            outcome = agent_turn.answer_turn(_request("who should look at it", thread=thread))
        # The subject is the ticket the approval filed, and the tool is who_knows, not the writer again.
        self.assertIn("• TKT-102 -[ABOUT]-> Billing Service", outcome.reply)
        self.assertIn("• Arjun Mehta -[OWNS]-> Billing Service", outcome.reply)
        self.assertIn("Tool `who_knows`, picked by keywords. Subject from this thread.", outcome.reply)

    def test_a_refusal_in_a_thread_stays_a_refusal(self):
        thread = [{"user": "USARA", "text": "<@UBOT> what breaks if auth goes down", "ts": "1"}, {"user": "UBOT", "bot_id": "B1", "text": "If Auth Service goes down...", "ts": "2"}]
        with mock.patch.object(agent, "generate_object", side_effect=ModelUnavailable("no key")):
            outcome = agent_turn.answer_turn(_request("who knows the payments gateway", thread=thread))
        self.assertIn("Nothing in the graph matches “payments gateway” as one thing", outcome.reply)
        self.assertNotIn("Auth Service goes down", outcome.reply)

    def test_the_turn_seam_routes_to_the_agent_once_registered(self):
        compute.register_turn_agent(agent_turn.answer_turn)
        try:
            with _model_picks("who_owns", {"name": "memory layer"}):
                outcome = compute.compute_turn(_request("who owns the memory layer"))
        finally:
            compute.register_turn_agent(None)
        self.assertIn("Memory Layer is owned by Kofi Mensah", outcome.reply)
        self.assertIn("picked by the model", outcome.reply)
