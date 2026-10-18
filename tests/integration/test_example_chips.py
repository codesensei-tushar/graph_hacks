"""Every example chip the page offers answers on its company's fresh seed; the last is the deliberate refusal.

The seed alone is what production serves: no replayed chat, no GitHub history, no imported
team. A chip that only answers once the replay has loaded the repos is refused there, so
each company's chips are clicked on a graph with nothing but its seed.
"""

import dataclasses
import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.core.config import settings  # noqa: E402
from src.db import falkordb_client  # noqa: E402
from src.domain.github import backfill  # noqa: E402
from src.domain.knowledge import ask_api, companies, graph_overview, proof, what_changed  # noqa: E402
from src.seed import company_brain, quillfern, seed  # noqa: E402
from tests.fixtures import graph_hacks_prs  # noqa: E402


class _ChipsOnTheSeed:
    company = quillfern

    @classmethod
    def setUpClass(cls):
        graph = f"company_chips_{cls.company.COMPANY['id'].replace('-', '_')}_test{tests.TEST_GRAPH_SUFFIX}"
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=graph),
            mock.patch.object(seed, "graph_name", return_value=graph),
            # "what changed this week" reads the seed's dates against a fixed day, not the day CI runs.
            mock.patch.object(what_changed, "now_millis", return_value=1_792_324_800_000),
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

    def expected(self) -> list[str]:
        return ask_api.EXAMPLES[self.company.COMPANY["id"]]

    def test_every_chip_answers_and_the_last_is_refused_with_the_nearest_names(self):
        listing = ask_api.examples()
        self.assertEqual(listing.questions, self.expected())
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

    def test_the_memory_chip_answers_with_a_path_and_waits_for_memories(self):
        memory_chip = "what have we said about billing"
        answer = ask_api.answer_question(memory_chip)
        self.assertEqual((answer.tool, answer.found), ("related_memories", True))
        self.assertTrue(answer.memories, answer.answer)
        self.assertTrue(any(step["rel"] == "MENTIONS" for step in answer.path), answer.answer)
        # Production before its seed refresh: no Memory nodes, so the chip is not offered.
        self.addCleanup(seed.reset_and_seed, self.company)
        falkordb_client.connect()[1].query("MATCH (m:Memory) DETACH DELETE m")
        ask_api.ANSWERS.clear()
        self.assertEqual(ask_api.examples().questions, [question for question in ask_api.EXAMPLES["quillfern"] if question != memory_chip])

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

    def expected(self) -> list[str]:
        # The repo's own chips lead; the pull request ones wait for GitHub ingest, which a fresh seed has not had.
        waiting = {"what did PR #207 do", "who should know about PR #216"}
        return [question for question in ask_api.REPO_EXAMPLES if question not in waiting] + ask_api.EXAMPLES["company-brain"]


class RepoNextToQuillfernChipsTest(unittest.TestCase):
    """Production's legacy graph for the live demo: Quillfern (``SEED_COMPANY``), the self-seed merged next
    to it, and this repo's pull requests from GitHub. The page still speaks for Quillfern, with the
    repo's chips first and Quillfern's refusal last."""

    graph = f"company_chips_repo_next_to_quillfern_test{tests.TEST_GRAPH_SUFFIX}"

    @classmethod
    def setUpClass(cls):
        production = dataclasses.replace(settings, seed_company="quillfern")
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=cls.graph),
            mock.patch.object(seed, "graph_name", return_value=cls.graph),
            mock.patch.object(what_changed, "now_millis", return_value=1_792_350_000_000),  # 18 Oct 2026, 19:00 UTC
            mock.patch.object(companies, "settings", production),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        seed.seed_company(company_brain)

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(cls.graph).delete()
        finally:
            ask_api.ANSWERS.clear()
            for patch in cls._patches:
                patch.stop()

    def setUp(self):
        ask_api.ANSWERS.clear()

    def _backfill(self):
        with mock.patch.object(backfill, "client_for", return_value=graph_hacks_prs.FakeGitHub()):
            backfill.backfill_installation(7)
        ask_api.ANSWERS.clear()

    def test_the_page_speaks_for_quillfern_and_offers_the_repo_chips_first(self):
        self._backfill()
        listing = ask_api.examples()
        self.assertEqual((listing.company_id, listing.company_name, listing.also), ("quillfern", "Quillfern", ["company-brain"]))
        self.assertEqual(listing.questions, ask_api.REPO_EXAMPLES + ask_api.EXAMPLES["quillfern"])
        *chips, refusal = listing.questions
        for question in chips:
            with self.subTest(question=question):
                answer = ask_api.answer_question(question)
                self.assertTrue(answer.found and answer.path, f"{question}: {answer.answer}")
        self.assertEqual(ask_api.answer_question("what did PR #207 do").tool, "pr_explain")
        self.assertFalse(ask_api.answer_question(refusal).found)
        # The stats line and the proof still name Quillfern.
        self.assertEqual(graph_overview.graph_stats().company, "quillfern")
        self.assertEqual(proof.company_id(), "quillfern")

    def test_before_github_ingest_the_pull_request_chips_wait(self):
        _, graph = falkordb_client.connect()
        graph.query("MATCH (n) WHERE n:PullRequest OR n:Repo OR n:GitHubUser DETACH DELETE n")
        questions = ask_api.examples().questions
        self.assertNotIn("what did PR #207 do", questions)
        self.assertNotIn("who should know about PR #216", questions)
        self.assertEqual(questions[:4], [question for question in ask_api.REPO_EXAMPLES if "PR #" not in question])
        self.assertEqual(questions[-1], ask_api.EXAMPLES["quillfern"][-1])
