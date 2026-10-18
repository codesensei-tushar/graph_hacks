"""Thread follow-ups around ``what_changed`` and ``search``, through the real Slack turn on the plain Quillfern seed.

Each test plays a thread the way Slack hands it to the turn: the person's messages with the
bot's mention, the bot's replies with their markdown blocks. The clock is 18 Oct 2026 12:00 UTC,
so the seed's dated changes are a month old at most and an empty week widens to a month.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import agent, what_changed  # noqa: E402
from src.domain.memory.read_scope import resolve_brain_read_container_tags  # noqa: E402
from src.domain.memory.writeback import SlackMemoryScope  # noqa: E402
from src.domain.slack import agent_turn  # noqa: E402
from src.domain.slack.compute import TurnRequest  # noqa: E402
from src.integrations.llm.model import ModelUnavailable  # noqa: E402
from src.integrations.slack.events import SlackEventInner  # noqa: E402
from src.integrations.slack.format import markdown_reply_blocks, plain_text_fallback  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_follow_ups_test{tests.TEST_GRAPH_SUFFIX}"
NOW = 1_792_324_800_000  # 18 Oct 2026 12:00 UTC
CHANNEL = "CFOLLOW"
FROM_THREAD = "picked by keywords. Subject from this thread."


def _turn(question: str, thread: list[dict]) -> TurnRequest:
    scope = SlackMemoryScope(kind="shared", channel_id=CHANNEL, channel_type="channel")
    event = SlackEventInner(type="app_mention", user="UALICE", channel=CHANNEL, channel_type="channel", text=question, ts="1792324800.000100")
    return TurnRequest(
        workspace=SimpleNamespace(team_id="T1", bot_user_id="UBOT"),
        event=event,
        question=question,
        raw_text=question,
        asker_slack_user_id="UALICE",
        asker_name="Alice",
        memory_scope=scope,
        read_tags=resolve_brain_read_container_tags(scope),
        thread=list(thread),
        channel=CHANNEL,
        thread_ts=event.ts,
        is_dm=False,
    )


class SlackFollowUpTest(unittest.TestCase):
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

    def setUp(self):
        for patch in (
            mock.patch.object(what_changed, "now_millis", return_value=NOW),
            mock.patch.object(agent, "generate_object", side_effect=ModelUnavailable("no key")),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.thread: list[dict] = []

    def ask(self, question: str, as_slack_stores_it: bool = True) -> str:
        """One turn in the thread; the reply is stored the way conversations.replies returns it."""
        self.thread.append({"user": "UALICE", "text": f"<@UBOT> {question}", "ts": str(len(self.thread))})
        reply = agent_turn.answer_turn(_turn(question, self.thread)).reply or ""
        stored = {"user": "UBOT", "bot_id": "B1", "text": plain_text_fallback(reply), "ts": str(len(self.thread))}
        if as_slack_stores_it:
            stored["blocks"] = markdown_reply_blocks(reply)
        self.thread.append(stored)
        return reply

    def test_a_follow_up_changes_only_the_window_the_kind_or_the_scope(self):
        self.assertTrue(self.ask("what's new in payments").startswith("Nothing dated in the last 7 days. In the last 30, 3 changes in Payments."))
        last_month = self.ask("and last month?")
        self.assertTrue(last_month.startswith("In the last 30 days, 3 changes in Payments. By thing: Billing Service: 2 changes; Invoice Worker: 1."), last_month)
        self.assertIn(FROM_THREAD, last_month)
        decisions = self.ask("only decisions")
        self.assertTrue(decisions.startswith("In the last 30 days, 1 decision in Payments, newest first: 20 Sep: decided *Retry renewal webhooks"), decisions)
        self.assertEqual(self.ask("what about platform?").split("\n")[0], "No decisions in Platform in the last 30 days.")
        everything = self.ask("everything")
        self.assertTrue(everything.startswith("In the last 30 days, 2 changes in Platform. By thing: Auth Service: 1 change; Notification Service: 1."), everything)

    def test_a_change_question_after_a_search_reads_around_the_service_it_found(self):
        self.assertTrue(self.ask("who knows about refunds").startswith("“refunds” comes up in the Billing runbook, about Billing Service."))
        changed = self.ask("what changed there this month?")
        self.assertTrue(changed.startswith("Around Billing Service and what it depends on (Auth Service, Postgres): in the last 30 days, 3 changes."), changed)
        self.assertIn("Tool `what_changed`, " + FROM_THREAD, changed)
        owner = self.ask("who owns that?")
        self.assertTrue(owner.startswith("Billing Service is owned by Arjun Mehta."), owner)
        self.assertIn("Tool `who_owns`, " + FROM_THREAD, owner)

    def test_a_named_thing_comes_before_what_the_reply_was_about(self):
        self.ask("who owns billing")
        changed = self.ask("what changed?")
        self.assertTrue(changed.startswith("Around Billing Service and what it depends on (Auth Service, Postgres):"), changed)
        self.assertIn(FROM_THREAD, changed)

    def test_a_new_question_in_the_thread_is_its_own_question(self):
        self.ask("what changed this week")
        owner = self.ask("who owns billing")
        self.assertTrue(owner.startswith("Billing Service is owned by Arjun Mehta."), owner)
        self.assertIn("Tool `who_owns`, picked by keywords. Read from:", owner)
        why = self.ask("why did we move the memory layer off postgres")
        self.assertIn("Tool `decision_trace`", why)

    def test_the_footer_is_read_from_slacks_plain_text_fallback_too(self):
        # A reply over Slack's section limit keeps its blocks, but some clients and exports keep only text.
        self.ask("what's new in payments", as_slack_stores_it=False)
        self.assertNotIn("`", self.thread[-1]["text"])
        decisions = self.ask("only decisions")
        self.assertTrue(decisions.startswith("Nothing dated in the last 7 days. In the last 30, 1 decision in Payments, newest first:"), decisions)
        self.assertIn(FROM_THREAD, decisions)

    def test_a_follow_up_needs_no_model_even_when_one_is_configured(self):
        self.ask("what's new in payments")
        with mock.patch.object(agent, "generate_object", side_effect=AssertionError("a follow-up must not call the model")):
            follow_up = self.ask("and last month?")
        self.assertIn("in Payments", follow_up)
        self.assertIn(FROM_THREAD, follow_up)

    def test_what_a_reply_was_about_is_the_service_its_path_points_at_most(self):
        reply = "\n".join(
            [
                "“refunds” comes up in the Billing runbook, about Billing Service.",
                "",
                "*Path* (`search`, 3 edges)",
                "• Arjun Mehta -[AUTHORED]-> Billing runbook",
                "• Billing runbook -[DESCRIBES]-> Billing Service",
                "• TKT-102 -[ABOUT]-> Billing Service",
                "• Invoice format spec -[DESCRIBES]-> Invoice Worker",
            ]
        )
        thread = [{"user": "UALICE", "text": "<@UBOT> who knows about refunds"}, {"user": "UBOT", "bot_id": "B1", "text": reply}]
        things = agent.reply_things(thread, ["org_shared"], frozenset({"UBOT"}))
        self.assertEqual([thing.name for thing in things], ["Billing Service", "Invoice Worker"])

    def test_over_mcp_a_search_answer_says_what_to_call_next(self):
        from src.domain.knowledge import mcp_tools

        pack = mcp_tools.call_tool("search", {"text": "refunds"})
        self.assertIn({"tool": "what_changed", "arguments": {"name": "Billing Service"}, "why": "what changed around it lately (Billing Service)"}, pack["next"])
        then = next(call for call in pack["next"] if call["tool"] == "what_changed")
        changed = mcp_tools.call_tool(then["tool"], then["arguments"])
        self.assertTrue(changed["answer"].startswith("Around Billing Service and what it depends on"), changed["answer"])

    def test_the_questions_the_install_greeting_suggests_now_answer(self):
        from src.domain.knowledge import ask_service
        from src.domain.slack.greet import DEFAULT_STARTERS

        for starter in DEFAULT_STARTERS:
            with self.subTest(starter=starter):
                self.assertEqual(ask_service.route(starter)[0], "what_changed")
                reply = self.ask(starter)
                self.assertIn("Tool `what_changed`", reply)
                self.assertNotIn("Nothing dated changed", reply)

    def test_catch_me_up_reads_around_the_askers_own_things(self):
        _, graph = falkordb_client.connect()
        graph.query("MERGE (u:SlackUser {id: 'UALICE'}) WITH u MATCH (p:Person {id: 'tomas'}) MERGE (u)-[:IS]->(p)")
        self.addCleanup(graph.query, "MATCH (:SlackUser {id: 'UALICE'})-[r:IS]->() DELETE r")
        mine = self.ask("catch me up on what I missed this month")
        self.assertTrue(mine.startswith("In the last 30 days, 2 changes around what Tomas Reyes works on, newest first: 20 Sep: Arjun Mehta said"), mine)
        self.assertIn("• Tomas Reyes -[WORKED_ON]-> Billing Service", mine)
        # A team the question names comes before the asker's own things.
        self.assertIn("in Platform", self.ask("catch me up on platform"))

    def test_an_asker_who_is_not_a_person_in_the_graph_gets_the_company(self):
        reply = self.ask("what did I miss this month")
        self.assertTrue(reply.startswith("In the last 30 days, 4 changes across the company."), reply)
