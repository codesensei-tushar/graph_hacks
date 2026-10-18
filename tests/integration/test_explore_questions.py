"""Every question the ask page offers is answered on a fresh seed: a judge's next click lands.

The page offers questions in three places: "Ask next" after an answer, "Try instead" after a
refusal, and a click on a node of a drawing (the hero's sample and every answer's path). Each is
BrainGraph.questionsFor (graph.js, QUESTIONS) of a node the page drew, or an answer.js NEXT step,
which tests/unit/test_explore.py holds to those same questions. So asking every question
QUESTIONS gives for every node of every example chip's answer, and of the hero's sample, asks
everything a judge can click; so does each team page's link for a person, a thing, a decision
and a ticket. Each must be found. A question that is not is fixed in the
mapping, never here.
"""

import re
import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import ask_api, graph_overview, team_page  # noqa: E402
from src.seed import quillfern, quillfern_sales, seed  # noqa: E402
from tests.page_questions import NEXT, questions_for  # noqa: E402

MAX_NEXT = 3


class _ExploreOnTheSeed:
    graph = f"explore_questions_test{tests.TEST_GRAPH_SUFFIX}"
    # Questions asked first; every node their answers draw is then asked about.
    extra: list[str] = []

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
        graph_overview.ANSWERS.clear()

    def offered(self) -> dict[str, str]:
        """Every question a click can ask, each with where it is offered."""
        *chips, _ = ask_api.EXAMPLES["quillfern"]
        offered: dict[str, str] = {}
        for start in chips + self.extra:
            answer = ask_api.answer_question(start)
            for node in answer.nodes:
                for question in questions_for(node.name, node.label, node.kind):
                    offered.setdefault(question, f"a node of {start!r}")
        for node in graph_overview.public_sample().nodes:
            for question in questions_for(node.name, node.label, node.kind):
                offered.setdefault(question, "a node of the hero's sample")
        # A team page links each person, thing, decision and ticket to its first question.
        for team in team_page.public_teams():
            page = team_page.public_team_page(team.id)
            named = [(member.name, "Person", None) for member in page.members]
            named += [(thing.name, "Service", thing.kind) for group in page.owns for thing in group.items]
            named += [(decision.name, "Decision", None) for decision in page.decisions]
            named += [(ticket.id, "Ticket", None) for ticket in page.tickets]
            for name, label, kind in named:
                offered.setdefault(questions_for(name, label, kind)[0], f"the {team.name} team page")
        return offered

    def test_every_question_a_node_offers_is_found(self):
        offered = self.offered()
        self.assertGreater(len(offered), 20)
        missed = []
        for question, where in sorted(offered.items()):
            answer = ask_api.answer_question(question)
            if not answer.found:
                missed.append(f"{question!r}, offered on {where}: {answer.answer}")
        self.assertEqual(missed, [])

    def test_every_next_step_that_names_no_node_is_found(self):
        for step in {step for steps in NEXT.values() for step in steps if not re.search(r"\{\w+\}", step)}:
            with self.subTest(step=step):
                answer = ask_api.answer_question(step)
                self.assertTrue(answer.found, answer.answer)
                self.assertTrue(answer.path, answer.answer)

    def test_the_refusal_offers_its_nearest_names_as_questions_that_are_found(self):
        refused = ask_api.answer_question(ask_api.EXAMPLES["quillfern"][-1])
        self.assertFalse(refused.found)
        self.assertTrue(refused.candidates, refused.answer)
        for name in refused.candidates[:MAX_NEXT]:
            question = questions_for(name)[0]
            with self.subTest(question=question):
                self.assertEqual(question, f"who knows {name}")
                answer = ask_api.answer_question(question)
                self.assertTrue(answer.found, answer.answer)


class QuillfernExploreTest(_ExploreOnTheSeed, unittest.TestCase):
    pass


class QuillfernSalesExploreTest(_ExploreOnTheSeed, unittest.TestCase):
    """With the Sales team imported: a process, a system, a playbook and an account, and their people."""

    graph = f"explore_questions_sales_test{tests.TEST_GRAPH_SUFFIX}"
    extra = ["who is on the sales team", "what if priya shah leaves", "what breaks if hubspot goes down", "why did we discount monthly plans at renewal"]

    @classmethod
    def seed_more(cls):
        quillfern_sales.seed_sales()

    def test_the_sales_kinds_are_among_the_nodes_asked_about(self):
        kinds = {node.kind for start in self.extra for node in ask_api.answer_question(start).nodes if node.label == "Service"}
        self.assertLessEqual({"process", "system", "playbook", "account"}, kinds)
