"""``what_changed`` on a real FalkorDB: the plain Quillfern seed, read at 18 Oct 2026 12:00 UTC.

The seed's dated changes are its decisions and its six public memories, so a week is empty
and the answer widens to a month. Tests that add a pull request, a private memory or a filed
ticket remove it again, so every test reads the same seed.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import agent, ask_service, catalogue, mcp_tools, what_changed  # noqa: E402
from src.domain.memory.read_scope import resolve_brain_read_container_tags  # noqa: E402
from src.domain.memory.writeback import SlackMemoryScope  # noqa: E402
from src.domain.slack import agent_turn  # noqa: E402
from src.domain.slack.compute import TurnRequest  # noqa: E402
from src.integrations.llm.model import ModelUnavailable  # noqa: E402
from src.integrations.slack.events import SlackEventInner  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_what_changed_test{tests.TEST_GRAPH_SUFFIX}"
ORG = ["org_shared"]
PRIVATE = "slack_channel_C9"
NOW = 1_792_324_800_000  # 18 Oct 2026 12:00 UTC
DAY = 86_400_000

LENA = "9 Oct: Lena Fischer said “the invoice worker sends the receipt email itself now, no more waiting for the nightly batch” (Invoice Worker, Notification Service)."
MAYA = "2 Oct: Maya Ortiz said “auth session refresh rotates tokens every hour now, see notebook#45” (Auth Service)."
ARJUN = "20 Sep: Arjun Mehta said “billing now retries a failed renewal webhook once, idempotently, so TKT-102 stops charging twice” (Billing Service, TKT-102)."
RETRIES = "20 Sep: decided *Retry renewal webhooks inside the billing service* (Billing Service), proposed by Arjun Mehta."


def _cites(result) -> list[str]:
    return [step.cite() for step in result.path]


def _changed(question: str, scopes=None):
    tool, args = ask_service.route(question)
    assert tool == "what_changed", (question, tool)
    return catalogue.get_tool(tool).run(args, scopes or ORG)


def _turn(question: str) -> TurnRequest:
    scope = SlackMemoryScope(kind="shared", channel_id="CCHANGED", channel_type="channel")
    event = SlackEventInner(type="app_mention", user="UALICE", channel="CCHANGED", channel_type="channel", text=question, ts="1792324800.000100")
    return TurnRequest(
        workspace=SimpleNamespace(team_id="T1", bot_user_id="UBOT"),
        event=event,
        question=question,
        raw_text=question,
        asker_slack_user_id="UALICE",
        asker_name="Alice",
        memory_scope=scope,
        read_tags=resolve_brain_read_container_tags(scope),
        thread=[],
        channel="CCHANGED",
        thread_ts=event.ts,
        is_dm=False,
    )


class WhatChangedTest(unittest.TestCase):
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
        clock = mock.patch.object(what_changed, "now_millis", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        _, self.graph = falkordb_client.connect()

    def _remove(self, node_id: str) -> None:
        self.addCleanup(self.graph.query, "MATCH (n {id: $id}) DETACH DELETE n", {"id": node_id})

    def test_an_empty_week_widens_to_a_month_across_the_company(self):
        result = _changed("what changed this week")
        self.assertTrue(result.found)
        self.assertEqual(
            result.answer,
            "Nothing dated in the last 7 days. In the last 30, 4 changes across the company. "
            f"By thing: Billing Service: 2 changes; Auth Service: 1; Invoice Worker: 1; Notification Service: 1. Newest first: {LENA} {MAYA} {ARJUN} {RETRIES}",
        )
        cites = _cites(result)
        self.assertIn("Retry renewal webhooks inside the billing service -[ABOUT]-> Billing Service", cites)
        self.assertIn("Arjun Mehta -[PROPOSED]-> Retry renewal webhooks inside the billing service", cites)
        # Memories are cited as memories, dated, not as path edges.
        self.assertEqual([(memory.author, memory.learned_at) for memory in result.memories], [("Lena Fischer", "2026-10-09"), ("Maya Ortiz", "2026-10-02"), ("Arjun Mehta", "2026-09-20")])
        self.assertFalse(any(step.from_label == "Memory" for step in result.path))
        self.assertTrue(all(step.from_id and step.to_id and step.from_label and step.to_label for step in result.path))

    def test_today_widens_once_and_nothing_at_all_is_still_an_answer(self):
        self.assertTrue(_changed("what changed today").answer.startswith("Nothing dated in the last day. In the last 30, 4 changes across the company"))
        with mock.patch.object(what_changed, "now_millis", return_value=NOW + 120 * DAY):
            empty = _changed("what changed this week")
        self.assertEqual((empty.found, empty.answer, empty.path), (True, "Nothing dated changed across the company in the last 30 days.", []))
        self.assertEqual(_changed("what changed in the last 3 days").answer[:62], "Nothing dated in the last 3 days. In the last 30, 4 changes ac")

    def test_a_team_means_what_it_runs(self):
        result = _changed("what's new in payments")
        self.assertEqual(
            result.answer,
            f"Nothing dated in the last 7 days. In the last 30, 3 changes in Payments. By thing: Billing Service: 2 changes; Invoice Worker: 1. Newest first: {LENA} {ARJUN} {RETRIES}",
        )
        self.assertIn("Payments -[RUNS]-> Billing Service", _cites(result))
        self.assertIn("Payments -[RUNS]-> Invoice Worker", _cites(result))
        self.assertNotIn("Maya Ortiz", result.answer)

    def test_a_service_reads_what_it_depends_on_and_names_who_to_ask(self):
        self.graph.query(
            """
            MATCH (auth:Service {id: 'auth-service'}), (maya:Person {id: 'maya'})
            CREATE (pr:PullRequest {id: 'pr-session-keys', name: 'notebook#60', title: 'Rotate session keys', state: 'closed', merged: true,
                                    scope: 'org_shared', updated_at: '2026-10-17T09:30:00Z'})-[:TOUCHES]->(auth),
                   (g:GitHubUser {id: 'gh-maya-test', login: 'maya-ortiz'})-[:AUTHORED]->(pr), (g)-[:IS]->(maya)
            """
        )
        self._remove("pr-session-keys")
        self._remove("gh-maya-test")
        result = _changed("billing is failing, what changed")
        self.assertEqual(
            result.answer,
            "Around Billing Service and what it depends on (Auth Service, Postgres): in the last 7 days, 1 change, newest first: "
            "17 Oct: *notebook#60* “Rotate session keys” by Maya Ortiz, merged (Auth Service). Owners to ask: Maya Ortiz (Auth Service).",
        )
        cites = _cites(result)
        for line in (
            "Billing Service -[DEPENDS_ON]-> Auth Service",
            "notebook#60 -[TOUCHES]-> Auth Service",
            "Maya Ortiz -[AUTHORED]-> notebook#60",
            "Maya Ortiz -[OWNS]-> Auth Service",
        ):
            self.assertIn(line, cites)

    def test_a_service_with_an_empty_month_says_so(self):
        result = _changed("recent changes to the graph store")
        self.assertEqual((result.found, result.answer), (True, "Nothing dated changed around Graph Store in the last 30 days."))

    def test_upstream_changes_are_grouped_by_thing_with_how_far_up_and_a_named_depth_narrows_them(self):
        result = _changed("what changed upstream of the invoice worker")
        self.assertTrue(
            result.answer.startswith(
                "Around Invoice Worker and what it depends on (Billing Service, Notification Service, Auth Service, Postgres): "
                "nothing dated in the last 7 days. In the last 30, 4 changes. By thing: Billing Service (1 hop up): 2 changes; "
                "Invoice Worker: 1; Notification Service (1 hop up): 1; Auth Service (2 hops up): 1. Newest first: "
            ),
            result.answer,
        )
        self.assertTrue(
            result.answer.endswith("Owners to ask: Lena Fischer (Invoice Worker and Notification Service), Arjun Mehta (Billing Service), Maya Ortiz (Auth Service)."),
            result.answer,
        )
        # Auth Service is two hops up through Billing; the route is cited edge by edge.
        self.assertIn("Invoice Worker -[DEPENDS_ON]-> Billing Service", _cites(result))
        self.assertIn("Billing Service -[DEPENDS_ON]-> Auth Service", _cites(result))
        near = _changed("what changed upstream of the invoice worker, one hop")
        self.assertIn("In the last 30, 3 changes.", near.answer)
        self.assertNotIn("Auth Service", near.answer)

    def test_only_one_kind_inside_the_window(self):
        self.assertEqual(_changed("what did the sales team decide recently").answer, f"Nothing dated in the last 14 days. In the last 30, 1 decision across the company, newest first: {RETRIES}")
        self.assertEqual(
            _changed("what decisions did we make this month").answer, f"In the last 30 days, 1 decision across the company, newest first: {RETRIES}"
        )

    def test_a_private_memory_shows_only_to_a_reader_of_its_room(self):
        self.graph.query(
            f"""
            MATCH (billing:Service {{id: 'billing-service'}}), (arjun:SlackUser {{id: 'UARJUN'}})
            CREATE (arjun)-[:SAID]->(:Memory {{id: 'memory-private-refunds', text: 'refunds for the double charges go out monday',
                                              scope: '{PRIVATE}', created_at: {NOW - DAY}}})-[:MENTIONS]->(billing)
            """
        )
        self._remove("memory-private-refunds")
        self.assertNotIn("refunds", _changed("what changed this week").answer)
        private = _changed("what changed this week", ORG + [PRIVATE])
        self.assertEqual(
            private.answer,
            "In the last 7 days, 1 change across the company, newest first: 17 Oct: Arjun Mehta said “refunds for the double charges go out monday” (Billing Service).",
        )
        self.assertEqual([memory.scope for memory in private.memories], [PRIVATE])

    def test_a_ticket_filed_from_slack_is_dated_and_shows_the_same_day(self):
        created = ask_service.ingest_ticket("billing exports time out", "", "org_shared")
        self._remove(created.id)
        [[created_at]] = self.graph.query("MATCH (t:Ticket {id: $id}) RETURN t.created_at", {"id": created.id}).result_set
        self.assertIsInstance(created_at, int)
        with mock.patch.object(what_changed, "now_millis", return_value=created_at + 60_000):
            result = _changed("what changed today")
        self.assertTrue(result.answer.startswith("In the last day, 1 change across the company, newest first: "), result.answer)
        self.assertIn(f"{created.id} “billing exports time out”, open (Billing Service).", result.answer)
        self.assertIn(f"{created.id} -[ABOUT]-> Billing Service", _cites(result))

    def test_it_is_a_read_tool_served_over_mcp(self):
        self.assertIn("what_changed", mcp_tools.tool_names())
        self.assertFalse(catalogue.get_tool("what_changed").needs_approval)
        pack = mcp_tools.call_tool("what_changed", {"name": "payments", "days": 30})
        self.assertTrue(pack["found"])
        self.assertTrue(pack["answer"].startswith("In the last 30 days, 3 changes in Payments. By thing: Billing Service: 2 changes"), pack["answer"])
        decided = mcp_tools.call_tool("what_changed", {"only": "decisions"})
        self.assertTrue(decided["answer"].startswith("Nothing dated in the last 7 days. In the last 30, 1 decision across the company"), decided["answer"])

    def test_in_slack_the_reply_cites_the_memories_it_quotes(self):
        with mock.patch.object(agent, "generate_object", side_effect=ModelUnavailable("no key")):
            outcome = agent_turn.answer_turn(_turn("what changed this week"))
        self.assertTrue(outcome.reply.startswith("Nothing dated in the last 7 days. In the last 30, 4 changes"), outcome.reply)
        self.assertIn("*From conversations*", outcome.reply)
        self.assertIn("Tool `what_changed`, picked by keywords", outcome.reply)
