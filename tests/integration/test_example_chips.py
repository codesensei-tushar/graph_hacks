"""Every example chip the page offers answers on its company's fresh seed; the last is the deliberate refusal.

The seed alone is what production serves: no replayed chat, no GitHub history, no imported
team. A chip that only answers once the replay has loaded the repos is refused there, so
each company's chips are clicked on a graph with nothing but its seed.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import ask_api  # noqa: E402
from src.seed import company_brain, quillfern, seed  # noqa: E402


class _ChipsOnTheSeed:
    company = quillfern

    @classmethod
    def setUpClass(cls):
        graph = f"company_chips_{cls.company.COMPANY['id'].replace('-', '_')}_test{tests.TEST_GRAPH_SUFFIX}"
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=graph),
            mock.patch.object(seed, "graph_name", return_value=graph),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(cls.company)

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(falkordb_client.graph_name()).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def setUp(self):
        ask_api.ANSWERS.clear()

    def test_every_chip_answers_and_the_last_is_refused_with_the_nearest_names(self):
        listing = ask_api.examples()
        self.assertEqual(listing.questions, ask_api.EXAMPLES[self.company.COMPANY["id"]])
        *chips, refusal = listing.questions
        for question in chips:
            with self.subTest(question=question):
                answer = ask_api.answer_question(question)
                self.assertTrue(answer.found, f"{question}: {answer.answer}")
        refused = ask_api.answer_question(refusal)
        self.assertFalse(refused.found, refused.answer)
        self.assertTrue(refused.answer.startswith("Nothing in the graph matches"), refused.answer)
        self.assertTrue(refused.candidates, refused.answer)


class QuillfernChipsTest(_ChipsOnTheSeed, unittest.TestCase):
    company = quillfern

    def test_a_chip_that_no_longer_answers_is_not_offered(self):
        # Production's older seed has no billing retries decision; that chip is refused there.
        self.addCleanup(seed.reset_and_seed, self.company)
        _, graph = falkordb_client.connect()
        graph.query("MATCH (d:Decision {name: 'Retry renewal webhooks inside the billing service'}) DETACH DELETE d")
        dropped = "is the billing retries decision still the last word"
        questions = ask_api.examples().questions
        self.assertEqual(questions, [question for question in ask_api.EXAMPLES["quillfern"] if question != dropped])
        self.assertEqual(questions[-1], "who knows the payments gateway")

    def test_a_chip_that_raises_is_dropped_alone_and_logged(self):
        broken = "which services are load-bearing"
        answer_question = ask_api.answer_question

        def ask(question, *args, **kwargs):
            if question == broken:
                raise RuntimeError("graph timed out")
            return answer_question(question, *args, **kwargs)

        with mock.patch.object(ask_api, "answer_question", side_effect=ask), self.assertLogs(ask_api.log, "WARNING") as logged:
            questions = ask_api.examples().questions
        self.assertEqual(questions, [question for question in ask_api.EXAMPLES["quillfern"] if question != broken])
        self.assertIn(broken, logged.output[0])


class CompanyBrainChipsTest(_ChipsOnTheSeed, unittest.TestCase):
    company = company_brain
