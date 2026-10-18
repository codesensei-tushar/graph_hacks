"""Day one for a company that onboarded a team with no aliases: "billing" finds Billing Service, and
"I" in Slack is the person the Slack user is.

A fresh graph, written as ``onboard team:`` writes it: a Payments team with Amy and Alice, running
Billing Service and Invoice Worker, no aliases, and Amy's Slack user linked to her Person.
"""

import types
import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import agent, ask_api, ask_service, asker, mcp_tools, resolve  # noqa: E402
from src.domain.memory.writeback import SlackMemoryScope  # noqa: E402
from src.domain.slack import compute  # noqa: E402

GRAPH = f"company_day_one_test{tests.TEST_GRAPH_SUFFIX}"
ORG = ["org_shared"]


class DayOneNamesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patch = mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH)
        cls._patch.start()
        ask_api.ANSWERS.clear()
        _, graph = falkordb_client.connect()
        graph.query("MATCH (n) DETACH DELETE n")
        graph.query(
            """
            CREATE (team:Team {id: 'payments', name: 'Payments'}),
                   (amy:Person {id: 'amy', name: 'Amy Member'}), (alice:Person {id: 'alice', name: 'Alice Admin'}),
                   (billing:Service {id: 'billing-service', name: 'Billing Service', kind: 'service'}),
                   (worker:Service {id: 'invoice-worker', name: 'Invoice Worker', kind: 'service'}),
                   (:Service {id: 'larkspur', name: 'Larkspur Freightways', kind: 'account'}),
                   (amy)-[:MEMBER_OF]->(team), (alice)-[:MEMBER_OF]->(team),
                   (team)-[:RUNS]->(billing), (team)-[:RUNS]->(worker), (alice)-[:OWNS]->(billing), (amy)-[:OWNS]->(worker),
                   (:SlackUser {id: 'UAMY', name: 'amy'})-[:IS]->(amy)
            """
        )

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            cls._patch.stop()

    def test_one_word_of_a_name_resolves_and_says_so(self):
        result = ask_service.ask("who knows billing", ORG)
        self.assertTrue(result.found, result.answer)
        self.assertTrue(result.answer.startswith("(Taking “billing” as Billing Service.) Billing Service is owned by Alice Admin."), result.answer)
        self.assertTrue(ask_service.ask("who owns larkspur", ORG).answer.startswith("(Taking “larkspur” as Larkspur Freightways.)"))
        # A unique start of a word, four letters or more.
        self.assertTrue(ask_service.ask("who knows invo", ORG).answer.startswith("(Taking “invo” as Invoice Worker.)"))
        self.assertIn("Taking “billing”", ask_api.answer_question("what are the open tickets about billing").answer)
        self.assertIn("Taking “billing”", mcp_tools.call_tool("who_knows", {"name": "billing"})["answer"])

    def test_two_things_that_fit_are_refused(self):
        _, graph = falkordb_client.connect()
        graph.query("CREATE (:Service {id: 'billing-exports', name: 'Billing Exports', kind: 'service'})")
        try:
            result = ask_service.ask("who knows billing", ORG)
            self.assertFalse(result.found)
            self.assertIn("Nothing in the graph matches “billing”", result.answer)
            # Whole names still resolve outright, with no note.
            self.assertTrue(ask_service.ask("who owns billing service", ORG).answer.startswith("Billing Service is owned by"))
        finally:
            graph.query("MATCH (s:Service {id: 'billing-exports'}) DETACH DELETE s")

    def test_part_of_a_long_title_is_never_taken(self):
        rows = [resolve.EntityRow("d1", "Keep notebook links in Postgres join tables", "Decision")]
        self.assertFalse(resolve.resolve_rows("who knows about tables", rows).found)

    # --- the asker in Slack ----------------------------------------------------------------------

    def _turn(self, user: str, text: str):
        request = types.SimpleNamespace(
            question=text, live_updates=[], passive_investigation=None, workspace=types.SimpleNamespace(bot_user_id="UBOT"),
            asker_slack_user_id=user, read_tags=ORG, thread=[], memory_scope=SlackMemoryScope(kind="dm", user_id=user, slack_user_id=user),
        )
        with mock.patch.object(compute, "channel_names", return_value={}):
            return compute._answer_from_graph(request).reply

    def test_the_built_in_slack_answerer_knows_the_asker(self):
        reply = self._turn("UAMY", "what team am I on")
        self.assertTrue(reply.startswith("You're on Payments. You own Invoice Worker."), reply)

    def test_slack_never_says_ask_in_slack(self):
        reply = self._turn("UNOBODY", "what team am I on")
        self.assertTrue(reply.startswith(asker.SLACK_SELF_MISS), reply)
        self.assertNotIn("Ask in Slack", reply)
        # Off Slack, the page still says where to ask.
        self.assertEqual(ask_api.answer_question("what team am I on").answer, resolve.SELF_MISS)
        # The agent path in Slack too.
        choice = asker.for_the_asker(agent.choose_with_keywords("what team am I on"), "what team am I on", "UNOBODY", ORG)
        result, _choice = agent.run_choice(choice, "what team am I on", ORG)
        self.assertEqual(asker.in_slack(result).answer, asker.SLACK_SELF_MISS)


if __name__ == "__main__":
    unittest.main()
