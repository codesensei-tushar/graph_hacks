"""Every step of the 60-second tour answers on a fresh seed, with and without the Sales team.

The tour asks each step's question through the page and skips a step whose question the graph
does not offer as an example chip. On Quillfern's fresh seed every step is offered, so none is
skipped, and each answers the way its caption says: a path, the honest refusal with its nearest
names, the risk map's grid, a person's profile grid and a dated digest.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import ask_api  # noqa: E402
from src.seed import quillfern, quillfern_sales, seed  # noqa: E402
from tests.unit.test_tour import steps  # noqa: E402


class _TourOnTheSeed:
    graph = f"tour_steps_test{tests.TEST_GRAPH_SUFFIX}"

    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=cls.graph),
            mock.patch.object(seed, "graph_name", return_value=cls.graph),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        cls.seed_more()

    @classmethod
    def seed_more(cls):
        pass

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(cls.graph).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def setUp(self):
        ask_api.ANSWERS.clear()
        self.asked = [step["ask"] for step in steps() if step["ask"]]

    def test_no_step_is_skipped_every_question_is_offered(self):
        offered = ask_api.examples().questions
        for question in self.asked:
            self.assertIn(question, offered)

    def test_each_step_answers_the_way_its_caption_says(self):
        path, refusal, risk, person, changed = (ask_api.answer_question(question) for question in self.asked)
        self.assertTrue(path.found)
        self.assertTrue(path.edges)
        self.assertTrue(any(edge.rel == "OWNS" for edge in path.edges), "step 1's fact names the owner")
        self.assertFalse(refusal.found)
        self.assertTrue(refusal.candidates, refusal.answer)
        self.assertEqual(risk.tool, "risk_map")
        self.assertEqual(risk.grid["kind"], "risk_map")
        self.assertTrue(risk.grid["rows"])
        self.assertEqual(person.tool, "person_profile")
        self.assertEqual(person.grid["kind"], "person_profile")
        self.assertTrue(person.grid["rows"])
        self.assertEqual(changed.tool, "what_changed")
        self.assertTrue(changed.found, changed.answer)
        self.assertRegex(changed.answer, r"\b\d+ changes?\b", "step 5's fact reads the count of changes")


class QuillfernTourTest(_TourOnTheSeed, unittest.TestCase):
    pass


class QuillfernSalesTourTest(_TourOnTheSeed, unittest.TestCase):
    graph = f"tour_steps_sales_test{tests.TEST_GRAPH_SUFFIX}"

    @classmethod
    def seed_more(cls):
        quillfern_sales.seed_sales()
