"""The four demo questions from docs/PROBLEM.md, answered by the graph tools alone.

Seeds the fictional Quillfern company into a throwaway graph and asks each question
through ``src.domain.knowledge.ask_service`` with no Slack and no model. Every answer
must cite the path it walked, and writing one new ticket must change the next answer.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client
from src.domain.knowledge import ask_service
from src.seed import quillfern, seed

GRAPH = "company_demo_questions_test"
ORG = [ask_service.ORG_SHARED]
NEW_TICKET = "A new ticket just landed about a billing bug. Who should look at it?"


def _step(source: str, rel: str, target: str) -> dict:
    return {"from": source, "rel": rel, "to": target}


class DemoQuestionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # ``connect`` and ``reset_and_seed`` each bound ``graph_name`` at import, so both are patched.
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
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

    def assert_cites_path(self, result: dict) -> None:
        self.assertTrue(result["path"], f"{result['tool']} answered without a path: {result['answer']}")
        for step in result["path"]:
            self.assertEqual(set(step), {"from", "rel", "to"})

    def test_who_owns_billing_and_who_else_worked_on_it(self):
        result = ask_service.ask("Who owns billing, and who else has worked on it?", ORG)
        self.assertEqual(result["tool"], "who_knows")
        self.assert_cites_path(result)
        self.assertIn("Billing Service is owned by Arjun Mehta", result["answer"])
        self.assertIn("Lena Fischer", result["answer"])
        self.assertIn("Tomas Reyes", result["answer"])
        for step in (
            _step("Arjun Mehta", "OWNS", "Billing Service"),
            _step("Lena Fischer", "WORKED_ON", "Billing Service"),
            _step("Tomas Reyes", "WORKED_ON", "Billing Service"),
            _step("Sara Lindqvist", "KNOWS", "Billing Service"),
            _step("TKT-102", "ABOUT", "Billing Service"),
            _step("Lena Fischer", "WORKED_ON", "TKT-102"),
        ):
            self.assertIn(step, result["path"])

    def test_why_the_memory_layer_left_postgres(self):
        result = ask_service.ask("Why did we move the memory layer off Postgres?", ORG)
        self.assertEqual(result["tool"], "decision_trace")
        self.assert_cites_path(result)
        decision = "Move the memory layer off Postgres"
        self.assertTrue(result["answer"].startswith(decision), result["answer"])
        self.assertIn("Proposed by Kofi Mensah", result["answer"])
        self.assertIn("Approved by Tomas Reyes", result["answer"])
        thread = "#data-eng thread on the memory migration"
        for step in (
            _step("Kofi Mensah", "PROPOSED", decision),
            _step("Tomas Reyes", "APPROVED", decision),
            _step(decision, "ABOUT", "Memory Layer"),
            _step(decision, "CITES", "RFC: memory layer on a graph store"),
            _step(decision, "CITES", "TKT-101"),
            _step("Kofi Mensah", "AUTHORED", thread),
            _step(thread, "ABOUT", decision),
        ):
            self.assertIn(step, result["path"])

    def test_what_breaks_when_auth_goes_down(self):
        result = ask_service.ask("What breaks if the auth service goes down?", ORG)
        self.assertEqual(result["tool"], "blast_radius")
        self.assert_cites_path(result)
        affected = {step["from"] for step in result["path"]}
        self.assertEqual(
            affected,
            {"API Gateway", "Billing Service", "Notification Service", "Search Service", "Invoice Worker"},
        )
        # Invoice Worker is two hops out: it depends on Billing Service, which depends on Auth.
        self.assertIn(_step("Billing Service", "DEPENDS_ON", "Auth Service"), result["path"])
        self.assertIn(_step("Invoice Worker", "DEPENDS_ON", "Billing Service"), result["path"])
        for name in ("Invoice Worker", "Billing Service", "Notification Service"):
            self.assertIn(name, result["answer"])

    def test_a_new_billing_ticket_changes_the_next_answer(self):
        before = ask_service.ask(NEW_TICKET, ORG)
        self.assertEqual(before["tool"], "who_knows")
        self.assertNotIn("TKT-104", before["answer"])

        written = ask_service.ingest_ticket("Billing bug: refunds double-charged", "billing", ask_service.ORG_SHARED)
        self.assertEqual((written["id"], written["about_name"]), ("TKT-104", "Billing Service"))
        self.assertEqual(written["path"], [_step("TKT-104", "ABOUT", "Billing Service")])

        after = ask_service.ask(NEW_TICKET, ORG)
        self.assert_cites_path(after)
        self.assertIn(_step("TKT-104", "ABOUT", "Billing Service"), after["path"])
        self.assertEqual(len(after["path"]), len(before["path"]) + 1)
        self.assertIn("TKT-104", after["answer"])
        self.assertIn("Arjun Mehta", after["answer"])

    def test_a_ticket_from_a_private_room_stays_in_that_room(self):
        room = "channel:CPRIVATE"
        ask_service.ingest_ticket("Billing bug: refunds double-charged", "billing", room)
        outsider = ask_service.ask(NEW_TICKET, ORG)
        self.assertNotIn(_step("TKT-104", "ABOUT", "Billing Service"), outsider["path"])
        self.assertNotIn("TKT-104", outsider["answer"])
        insider = ask_service.ask(NEW_TICKET, ORG + [room])
        self.assertIn(_step("TKT-104", "ABOUT", "Billing Service"), insider["path"])

    def test_something_not_in_the_graph_says_so(self):
        result = ask_service.ask("Who owns the espresso machine?", ORG)
        self.assertEqual(result["path"], [])
        self.assertIn("Nothing in the graph matches", result["answer"])
