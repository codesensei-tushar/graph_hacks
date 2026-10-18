"""Free-form questions a judge types into the public page, on the Quillfern seed alone with no model.

Each one was answered about the wrong thing or refused although the graph knows the answer,
before the routing, aliases and decision fallback here. The page runs the keyword router, so
these go through ``ask_api.answer_question`` exactly as the page does.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import ask_api  # noqa: E402
from src.seed import quillfern, quillfern_sales, seed  # noqa: E402

GRAPH = f"company_judge_questions_test{tests.TEST_GRAPH_SUFFIX}"


class JudgeQuestionsTest(unittest.TestCase):
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
        ask_api.ANSWERS.clear()

    def _ask(self, question: str):
        return ask_api.answer_question(question)

    def test_each_question_gets_the_tool_and_the_thing_it_asks_about(self):
        cases = {
            "why did we move off postgres": ("decision_trace", "Move the memory layer off Postgres."),
            "why do we use a graph store": ("decision_trace", "Move the memory layer off Postgres."),
            "what decisions have we made about billing": ("decision_trace", "Retry renewal webhooks inside the billing service."),
            "is the memory layer decision still current": ("decision_pressure", "*Move the memory layer off Postgres* is the standing decision about Memory Layer."),
            "what connects sara and the invoice worker": ("path_between", "Sara Lindqvist reaches Invoice Worker in 2 hops"),
            "who is the bus factor for billing": ("who_knows", "Billing Service is owned by Arjun Mehta. Ask first: Arjun Mehta"),
            "what does the payments team own": ("onboarding_brief", "The Payments team is Arjun Mehta (Billing engineer) and Lena Fischer"),
            "who is on the platform team": ("onboarding_brief", "The Platform team is Maya Ortiz (Platform lead), Sara Lindqvist"),
            "what does the invoice worker depend on": ("blast_radius", "Invoice Worker depends on Billing Service and Notification Service (1 hop)"),
            # Round 2: the hero chips and README questions, reworded.
            "why did we leave postgres": ("decision_trace", "Move the memory layer off Postgres."),
            "how are maya and the invoice worker related": ("path_between", "Maya Ortiz reaches Invoice Worker in 3 hops"),
            "path from maya to the invoice worker": ("path_between", "Maya Ortiz reaches Invoice Worker in 3 hops"),
            "which service matters most": ("load_bearing", "Postgres carries the most weight"),
            "who works with who": ("working_groups", "Label propagation over who owns or worked on the same service"),
            "which people work together?": ("working_groups", "Label propagation over who owns or worked on the same service"),
            "who collaborates with whom": ("working_groups", "Label propagation over who owns or worked on the same service"),
            "is the billing retries decision still valid?": ("decision_pressure", "*Retry renewal webhooks inside the billing service* is the standing decision"),
            "is the retries decision still current": ("decision_pressure", "*Retry renewal webhooks inside the billing service* is the standing decision"),
            # Haiku picks the forward walk for these; the keyword path now does too.
            "what does the invoice worker need to run": ("blast_radius", "Invoice Worker depends on Billing Service and Notification Service (1 hop)"),
            "what does billing require": ("blast_radius", "Billing Service depends on Auth Service and Postgres (1 hop)"),
            "what relies on billing": ("blast_radius", "If Billing Service goes down, these stop with it"),
        }
        for question, (tool, start) in cases.items():
            with self.subTest(question=question):
                answer = self._ask(question)
                self.assertEqual((answer.tool, answer.found), (tool, True), answer.answer)
                self.assertTrue(answer.answer.startswith(start), answer.answer)

    def test_a_service_stands_for_its_decision_only_when_the_question_asks_for_a_decision(self):
        # "why is billing slow" names Billing Service but asks for no decision: still refused, never the retries decision.
        slow = self._ask("why is billing slow")
        self.assertEqual((slow.tool, slow.found), ("decision_trace", False))
        self.assertNotIn("Retry renewal webhooks", slow.answer.split("Nearest:")[0])
        # Postgres has no decision about it, so there is nothing to stand for.
        self.assertFalse(self._ask("what decisions have we made about postgres").found)

    def test_a_refusal_quotes_only_what_was_named(self):
        # No thread on the page, so a follow-up names nothing; recall quotes the thing, not "say billing".
        follow_up = self._ask("and two hops out")
        self.assertEqual((follow_up.found, follow_up.answer), (False, "I can't tell what that refers to. Name a service, document, or ticket."))
        self.assertEqual(self._ask("what did anyone say about billing").answer, "Nothing in the memories I can read mentions “billing”.")

    def test_the_graph_store_alias_on_the_decision_does_not_take_the_service(self):
        owner = self._ask("who owns the graph store")
        self.assertEqual((owner.tool, owner.found), ("who_owns", True))
        self.assertTrue(owner.answer.startswith("Graph Store is owned by Sara Lindqvist."), owner.answer)


class JudgeQuestionsWithSalesTest(unittest.TestCase):
    """The same, with the Sales team imported, as production may have it for the recording."""

    @classmethod
    def setUpClass(cls):
        graph = f"company_judge_questions_sales_test{tests.TEST_GRAPH_SUFFIX}"
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=graph),
            mock.patch.object(seed, "graph_name", return_value=graph),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        quillfern_sales.seed_sales()

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(falkordb_client.graph_name()).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def setUp(self):
        ask_api.ANSWERS.clear()

    def test_sales_questions_in_judges_words(self):
        cases = {
            "what does sales own": ("onboarding_brief", "Sales: Renewals, pricing and the enterprise accounts that pay for Quillfern. The Sales team is"),
            "who owns enterprise accounts": ("who_owns", "Enterprise accounts is owned by Dev Patel."),
            "is the annual plans decision still current": ("decision_pressure", "*Sell annual plans only at renewal* is the standing decision"),
        }
        for question, (tool, start) in cases.items():
            with self.subTest(question=question):
                answer = ask_api.answer_question(question)
                self.assertEqual((answer.tool, answer.found), (tool, True), answer.answer)
                self.assertTrue(answer.answer.startswith(start), answer.answer)
