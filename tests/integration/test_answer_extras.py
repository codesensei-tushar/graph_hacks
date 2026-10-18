"""What sits around an answer on a real FalkorDB: the memories shown under it (only those that
mention its subject or a neighbour), the second half of a compound question offered next, a
typo answered with the nearest name, and small talk answered as help on the ask page."""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.connectors import graph as connector_graph
from src.connectors.base import Container, Edge, ExternalUser, Install, Item, Page
from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import ask_api, ask_service, memories, smalltalk  # noqa: E402
from src.domain.memory import memory_service  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_answer_extras_test{tests.TEST_GRAPH_SUFFIX}"
ORG = ["org_shared"]
SLACK = ["org_shared", "org_internal"]
HUBSPOT = Install(source="hubspot", config={})


class AnswerExtrasTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        ask_api.ANSWERS.clear()
        _, graph = falkordb_client.connect()
        graph.query("MATCH (m:Memory) DETACH DELETE m")
        memory_service.write("billing retries now go through the invoice worker, decided in sync", "org_shared", "UBO", "CPUB", "1700000100.000100")
        memory_service.write("the graph store compaction now runs weekly", "org_shared", "UBO", "CPUB", "1700000200.000100")
        crm = Container(id=HUBSPOT.container_id("crm"), name="HubSpot CRM", external_id="crm", kind="crm")
        connector_graph.write_containers(HUBSPOT, [crm])
        yash = ExternalUser(id=HUBSPOT.user_id("y1"), source="hubspot", external_id="y1", name="Yash Patil", email="")
        account = Item(label="Service", kind="account", id=HUBSPOT.item_id("company:1"), external_id="company:1", container=crm.id, name="Larkspur Freightways")
        connector_graph.write_page(HUBSPOT, Page(items=[account], users=[yash], edges=[Edge("OWNS", yash.id, account.id)]))

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in reversed(cls._patches):
                patch.stop()

    # --- 10: memories under an answer ------------------------------------------------------------

    def test_an_answer_shows_the_memories_that_mention_its_subject(self):
        result = ask_service.ask("who knows billing", ORG)
        attached = memories.attached(result, ORG)
        self.assertTrue(attached, "a memory mentions Billing Service")
        self.assertEqual([memory.text for memory in attached], ["billing retries now go through the invoice worker, decided in sync"])

    def test_an_account_answer_shows_no_unrelated_memory(self):
        result = ask_service.ask("who owns Larkspur Freightways", SLACK)
        self.assertTrue(result.found, result.answer)
        self.assertEqual(memories.attached(result, SLACK), [])

    def test_a_refusal_shows_no_memories(self):
        result = ask_service.ask("who owns the payments gateway", ORG)
        self.assertFalse(result.found)
        self.assertEqual(memories.attached(result, ORG), [])

    # --- 8: compound questions -------------------------------------------------------------------

    def test_a_compound_question_answers_the_first_part_and_offers_the_second(self):
        result = ask_service.ask("who owns billing and what breaks if auth goes down", ORG)
        self.assertEqual(result.tool, "who_owns")
        self.assertTrue(result.answer.startswith("Billing Service is owned by Arjun Mehta."), result.answer)
        self.assertIn("You also asked “what breaks if auth goes down”: ask it on its own for that answer.", result.answer)
        page = ask_api.answer_question("who owns billing and what breaks if auth goes down")
        self.assertIn("You also asked", page.answer)

    def test_who_to_talk_to_after_a_preamble(self):
        result = ask_service.ask("I am new on payments and need to fix a bug, who should I talk to about the invoice worker", ORG)
        self.assertEqual(result.tool, "who_knows")
        self.assertTrue(result.answer.startswith("Invoice Worker is owned by"), result.answer)

    def test_the_subject_named_before_who_should_i_ping_is_kept(self):
        cases = {
            "billing is failing, who should I ping": "Billing Service is owned by Arjun Mehta.",
            "the invoice worker is down, who should we talk to": "Invoice Worker is owned by",
            "I'm stuck on TKT-101, who should I ask": "TKT-101 is",
            # The text after the phrase names nothing; the preamble does.
            "billing is failing, who should I talk to about it": "Billing Service is owned by Arjun Mehta.",
        }
        for question, lead in cases.items():
            with self.subTest(question=question):
                result = ask_service.ask(question, ORG)
                self.assertEqual(result.tool, "who_knows")
                self.assertTrue(result.found, result.answer)
                self.assertTrue(result.answer.startswith(lead), result.answer)

    # --- 9: typos and small talk -----------------------------------------------------------------

    def test_a_typo_is_answered_with_the_nearest_name(self):
        self.assertIn("Nearest: Billing Service", ask_service.ask("who owns biling", ORG).answer)

    def test_the_ask_page_answers_small_talk_as_help(self):
        hello = ask_api.answer_question("hi")
        self.assertEqual((hello.tool, hello.found), ("help", True))
        self.assertTrue(hello.answer.startswith("Hi! Ask me about the company"))
        self.assertEqual(ask_api.answer_question("thanks!").answer, smalltalk.THANKS_TEXT)


if __name__ == "__main__":
    unittest.main()
