"""One question about several things, and two questions in one: every part is answered or named.

Quillfern on a real FalkorDB, through ``agent.run_choice`` as the page and Slack run a keyword choice.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import agent, compound  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_compound_objects_test{tests.TEST_GRAPH_SUFFIX}"
ORG = ["org_shared"]


def _run(question: str):
    result, _choice = agent.run_choice(agent.choose_with_keywords(question), question, ORG)
    return result


class CompoundObjectsTest(unittest.TestCase):
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
            for patch in reversed(cls._patches):
                patch.stop()

    def test_one_verb_two_things_answers_both(self):
        result = _run("who knows auth and postgres")
        first, second = result.answer.split(compound.SEPARATOR)
        self.assertTrue(first.startswith("Auth Service is owned by Maya Ortiz."), first)
        self.assertTrue(second.startswith("Postgres is owned by Sara Lindqvist."), second)
        self.assertEqual([part["question"] for part in result.parts], ["Auth Service", "Postgres"])

    def test_a_comma_list_answers_each_line(self):
        result = _run("who owns billing, auth, postgres and search")
        self.assertEqual(
            result.answer.split(compound.SEPARATOR),
            [
                "Billing Service is owned by Arjun Mehta. Run by the Payments team.",
                "Auth Service is owned by Maya Ortiz. Run by the Platform team.",
                "Postgres is owned by Sara Lindqvist. Run by the Platform team.",
                "Search Service is owned by Kofi Mensah. Run by the Data team.",
            ],
        )

    def test_past_the_cap_the_rest_is_named(self):
        with mock.patch.object(compound, "MAX_OBJECTS", 2):
            result = _run("who owns billing, auth and postgres")
        self.assertTrue(result.answer.endswith("(Answered for the first 2; ask about Postgres separately.)"), result.answer)

    def test_two_questions_joined_by_and_what_get_both_answers(self):
        result = _run("what breaks if auth goes down and what if maya leaves")
        self.assertEqual(result.tool, "blast_radius + if_person_leaves")
        self.assertIn("If Auth Service goes down", result.answer)
        self.assertIn("If Maya Ortiz leaves", result.answer)

    def test_ordinary_questions_are_not_split(self):
        self.assertEqual(_run("how is maya connected to the invoice worker").tool, "path_between")
        self.assertNotIn(compound.SEPARATOR, _run("who owns billing, and who else has worked on it").answer)
        self.assertFalse(_run("who owns the payments gateway").found)
        self.assertNotIn(compound.SEPARATOR, _run("what breaks if auth goes down").answer)


if __name__ == "__main__":
    unittest.main()
