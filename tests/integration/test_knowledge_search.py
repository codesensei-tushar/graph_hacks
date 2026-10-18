"""``search`` on a real FalkorDB, alone and as the refusal fallback: words found through full-text indexes, then the people behind them.

Plain Quillfern, plus what it lacks for the edge cases: a private pull request about refunds
that only its repo's readers can see, and a private memory. The seed creates the indexes, as
production's startup does.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import ask_api, ask_service, catalogue, mcp_tools, search  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_search_test{tests.TEST_GRAPH_SUFFIX}"
ORG = ["org_shared"]
REPO = "github_repo_9002"
PRIVATE = "slack_channel_GTOPIC"


def _cites(result) -> list[str]:
    return [step.cite() for step in result.path]


class SearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        graph = falkordb_client.connect()[1]
        graph.query(
            """
            MATCH (billing:Service {id: 'billing-service'}), (arjun:Person {id: 'arjun'}), (tomas:Person {id: 'tomas'})
            CREATE (pr:PullRequest {id: 'pr-refunds', name: 'billing-internal#7', title: 'Guard refunds against double charges', summary: '', scope: $repo, state: 'closed'})
            CREATE (pr)-[:TOUCHES]->(billing)
            CREATE (a:GitHubUser {id: 'gh-arjun', login: 'arjun-mehta'})-[:IS]->(arjun)
            CREATE (t:GitHubUser {id: 'gh-tomas', login: 'tomas-reyes'})-[:IS]->(tomas)
            CREATE (a)-[:AUTHORED]->(pr)
            CREATE (t)-[:REVIEWED]->(pr)
            CREATE (m:Memory {id: 'memory-private-chargeback', name: 'chargeback disputes now go to Noor', text: 'chargeback disputes now go to Noor, not the billing channel', scope: $private, created_at: 1790000000000})
            CREATE (:SlackUser {id: 'UPRIV', name: 'Priya Shah'})-[:SAID]->(m)
            CREATE (m)-[:MENTIONS]->(billing)
            """,
            {"repo": REPO, "private": PRIVATE},
        )

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def setUp(self):
        ask_api.ANSWERS.clear()

    def test_who_knows_about_a_topic_finds_the_document_and_its_author(self):
        self.assertEqual(ask_service.route("who knows about refunds")[0], "who_knows")
        result = ask_service.ask("who knows about refunds", ORG)
        self.assertEqual(result.tool, "search")
        self.assertTrue(result.found)
        self.assertEqual(result.answer, "“refunds” comes up in the Billing runbook, about Billing Service. Ask first: Arjun Mehta (wrote the Billing runbook).")
        self.assertEqual(_cites(result), ["Arjun Mehta -[AUTHORED]-> Billing runbook", "Billing runbook -[DESCRIBES]-> Billing Service"])

    def test_a_private_pull_request_counts_only_for_its_readers(self):
        reader = ask_service.ask("who knows about refunds", ORG + [REPO])
        self.assertIn("billing-internal#7", reader.answer)
        self.assertIn("Arjun Mehta (wrote the Billing runbook and billing-internal#7)", reader.answer)
        self.assertIn("Tomas Reyes (reviewed billing-internal#7)", reader.answer)
        self.assertIn("billing-internal#7 -[TOUCHES]-> Billing Service", _cites(reader))
        self.assertNotIn("billing-internal#7", ask_service.ask("who knows about refunds", ORG).answer)

    def test_every_kind_of_hit_ranks_people_by_their_edges(self):
        result = ask_service.ask("who can help with webhooks", ORG)
        self.assertEqual(result.tool, "search")
        self.assertEqual(
            result.answer,
            "“webhooks” comes up in TKT-102, *Retry renewal webhooks inside the billing service* and 1 chat message, about Billing Service. "
            "Ask first: Arjun Mehta (proposed *Retry renewal webhooks inside the billing service*, said it in chat), "
            "then Lena Fischer (worked on TKT-102) and Tomas Reyes (approved *Retry renewal webhooks inside the billing service*).",
        )
        cites = _cites(result)
        self.assertIn("Lena Fischer -[WORKED_ON]-> TKT-102", cites)
        self.assertIn("TKT-102 -[ABOUT]-> Billing Service", cites)
        self.assertIn("Arjun Mehta -[PROPOSED]-> Retry renewal webhooks inside the billing service", cites)
        # The chat message is a dated memory, shown with the day it was said.
        self.assertEqual([memory.learned_at for memory in result.memories], ["2026-09-20"])
        self.assertTrue(result.memories[0].text.startswith("billing now retries a failed renewal webhook"))

    def test_a_replaced_decision_says_what_replaced_it(self):
        result = ask_service.ask("who knows about join tables", ORG)
        self.assertIn("“join tables” comes up in", result.answer)
        self.assertIn("*Keep notebook links in Postgres join tables* (since replaced by *Move the memory layer off Postgres*)", result.answer)
        self.assertTrue(result.answer.endswith("Ask first: Tomas Reyes (approved *Keep notebook links in Postgres join tables* and *Move the memory layer off Postgres*), then Kofi Mensah (proposed *Move the memory layer off Postgres*) and Sara Lindqvist (proposed *Keep notebook links in Postgres join tables*)."), result.answer)

    def test_a_private_memory_counts_only_in_its_room(self):
        self.assertFalse(ask_service.ask("who knows about chargeback disputes", ORG).found)
        inside = ask_service.ask("who knows about chargeback disputes", ORG + [PRIVATE])
        self.assertTrue(inside.found)
        self.assertIn("Ask first: Priya Shah (said it in chat).", inside.answer)
        self.assertEqual([memory.scope for memory in inside.memories], [PRIVATE])

    def test_the_refusals_are_unchanged(self):
        # Demo step 6 and the page's last chip: a named thing that is not in the graph.
        refusal = ask_service.ask("who knows the payments gateway", ORG)
        self.assertEqual((refusal.tool, refusal.found), ("who_knows", False))
        self.assertEqual(refusal.answer, "Nothing in the graph matches “payments gateway” as one thing. Nearest: Billing Service (known as “payments”), API Gateway (known as “gateway”).")
        # "about" asks for a topic, but every word must come up together, and these never do.
        self.assertEqual(ask_service.ask("who knows about the payments gateway", ORG).answer, refusal.answer)
        kafka = ask_service.ask("who knows about kafka", ORG)
        self.assertEqual((kafka.tool, kafka.found), ("who_knows", False))
        self.assertEqual(kafka.answer, "Nothing in the graph matches “kafka”. Name a service, document, or ticket that exists in the graph.")

    def test_a_named_thing_still_answers_as_who_knows(self):
        result = ask_service.ask("who knows about billing", ORG)
        self.assertEqual(result.tool, "who_knows")
        self.assertTrue(result.answer.startswith("Billing Service is owned by Arjun Mehta."), result.answer)

    def test_the_tool_answers_the_model_and_mcp_directly(self):
        spec = catalogue.get_tool("search")
        self.assertIsNotNone(spec)
        self.assertFalse(spec.needs_approval)
        self.assertTrue(spec.run({"text": "pdf layout"}, ORG).answer.startswith("“pdf layout” comes up in the Invoice format spec"))
        missing = spec.run({"text": "kafka"}, ORG)
        self.assertFalse(missing.found)
        self.assertEqual(missing.answer, "Nothing I can read mentions “kafka”: no document, ticket, pull request, decision or chat message.")
        self.assertIn("search", mcp_tools.tool_names())
        called = mcp_tools.call_tool("search", {"text": "refunds"})
        self.assertTrue(called["found"])
        self.assertIn({"from": "Arjun Mehta", "rel": "AUTHORED", "to": "Billing runbook"}, called["path"])

    def test_the_ask_page_shows_the_topic_answer_and_its_path(self):
        answer = ask_api.answer_question("who knows about refunds")
        self.assertEqual((answer.tool, answer.found), ("search", True))
        self.assertIn("who knows about refunds", ask_api.examples().questions)
        self.assertEqual({node.label for node in answer.nodes}, {"Person", "Document", "Service"})


    def test_search_answers_directly_from_its_route(self):
        result = ask_service.ask("search for webhooks", ORG)
        self.assertEqual((result.tool, result.found), ("search", True))
        self.assertTrue(result.answer.startswith("“webhooks” comes up in TKT-102"), result.answer)

    def test_every_refusal_point_falls_back_to_search_on_a_topic(self):
        cases = {
            # who_owns: "responsible" routes to it, and "refunds" names no service.
            "who is responsible for things about refunds": "who_owns",
            # onboarding_brief: "brief me" routes to it, and "refunds" names no team.
            "brief me about refunds": "onboarding_brief",
            # decision_trace: "decide" routes to it, and no decision is named "refunds".
            "what did we decide about refunds": "decision_trace",
            # blast_radius: "breaks" routes to it, and "refunds" names no service.
            "what breaks if stuff about refunds is down": "blast_radius",
        }
        for question, routed in cases.items():
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question)[0], routed)
                result = ask_service.ask(question, ORG)
                self.assertEqual((result.tool, result.found), ("search", True), result.answer)
                self.assertIn("the Billing runbook", result.answer)

    def test_a_why_question_keeps_its_refusal(self):
        result = ask_service.ask("why did we decide that about refunds", ORG)
        self.assertEqual((result.tool, result.found), ("decision_trace", False))

    def test_every_hit_comes_back_as_data_to_chain_from(self):
        result = ask_service.ask("who can help with webhooks", ORG)
        grid = result.grid
        self.assertEqual((grid["kind"], grid["topic"]), ("search", "webhooks"))
        rows = {row["name"]: row for row in grid["rows"]}
        self.assertEqual(rows["TKT-102"]["id"], "TKT-102")
        self.assertEqual(rows["TKT-102"]["about"], [{"id": "billing-service", "name": "Billing Service"}])
        self.assertEqual(rows["TKT-102"]["people"], [{"id": "lena", "name": "Lena Fischer", "rel": "WORKED_ON"}])
        memory = next(row for row in grid["rows"] if row["label"] == "Memory")
        self.assertEqual(memory["learned_at"], "2026-09-20")
        self.assertEqual([person["name"] for person in grid["ranked"]], ["Arjun Mehta", "Lena Fischer", "Tomas Reyes"])
        # The page and MCP get it as they get risk_map's.
        self.assertEqual(ask_api.answer_question("who can help with webhooks").to_dict()["grid"]["kind"], "search")
        self.assertEqual(mcp_tools.call_tool("search", {"text": "webhooks"})["grid"]["topic"], "webhooks")

class SearchIndexesTest(unittest.TestCase):
    """Startup creates the indexes once; a second start, or one with them already there, changes nothing."""

    graph_name = f"company_search_indexes_test{tests.TEST_GRAPH_SUFFIX}"

    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=cls.graph_name),
            mock.patch.object(seed, "graph_name", return_value=cls.graph_name),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(cls.graph_name).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def _fields(self) -> dict[str, set[str]]:
        return search.indexed_fields(falkordb_client.connect()[1])

    def test_the_seed_creates_every_index_and_a_second_run_is_a_no_op(self):
        before = self._fields()
        for label, fields in search.INDEXED.items():
            self.assertEqual(before.get(label, set()) & set(fields), set(fields), label)
        self.assertIn("text", before["Memory"])
        self.assertEqual(search.ensure_search_indexes(), [])
        self.assertEqual(search.ensure_search_indexes(), [])
        self.assertEqual(self._fields(), before)

    def test_a_missing_index_keeps_the_refusal_and_the_next_start_restores_it(self):
        graph = falkordb_client.connect()[1]
        graph.query("CALL db.idx.fulltext.drop('Document')")
        try:
            refused = ask_service.ask("who knows about refunds", ORG)
            self.assertEqual((refused.tool, refused.found), ("who_knows", False))
            self.assertEqual(refused.answer, "Nothing in the graph matches “refunds”. Name a service, document, or ticket that exists in the graph.")
        finally:
            self.assertEqual(search.ensure_search_indexes(), ["Document.name", "Document.summary"])
        self.assertEqual(ask_service.ask("who knows about refunds", ORG).tool, "search")
