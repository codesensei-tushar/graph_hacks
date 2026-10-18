"""pr_explain's routing and wording helpers, pure: which questions reach it, and the description gist."""

import unittest

from src.domain.knowledge import ask_service, pr_explain
from tests.fixtures import graph_hacks_prs as fx


def _body(number: int) -> str:
    return next(pr["body"] for pr in fx.PULL_REQUESTS if pr["number"] == number)


class PrExplainRouteTest(unittest.TestCase):
    def test_questions_about_one_pull_request_reach_it(self):
        for question in (
            "what did PR #207 do",
            "explain PR 207",
            "explain graph_hacks#207",
            "what is PR-45",
            "PR #207",
            "graph_hacks#207",
            "codesensei-tushar/graph_hacks#207?",
            "tell me about #216",
            "who did #216",
            "who wrote PR 216",
            "who made graph_hacks#216",
            "what does PR #207 affect",
            "what does #207 touch",
            "what did PR 207 change",
            "who reviewed PR 207",
            "who approved graph_hacks#207",
            "what tickets does PR 207 close",
            "does PR 207 fix PAY-12",
            "is PR 207 merged",
            "what's the status of PR 207",
        ):
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question), ("pr_explain", {"name": question}))

    def test_ticket_keys_and_other_tools_keep_their_routes(self):
        cases = {
            "what is PAY-12": "ticket_status",
            "is PAY-12 fixed": "ticket_status",
            "who is working on PAY-11": "ticket_status",
            "what blocks PAY-11": "ticket_status",
            "what does PAY-13 block": "ticket_status",
            "PAY-12": "ticket_status",
            "who should know about PR #216": "who_should_know",
            "who should know about notebook#45": "who_should_know",
            "who else should review notebook#45": "who_should_know",
            "who should I ping about billing-internal#7": "who_should_know",
            "what changed in the scheduler this week": "what_changed",
            "explore graph_hacks#207": "explore",
            "who knows notebook#45": "who_knows",
            "who reviewed the most code": "people_ranking",
            "what is issue #12": "who_knows",
        }
        for question, tool in cases.items():
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question)[0], tool)

    def test_a_bare_number_is_not_a_pull_request_in_a_question(self):
        self.assertFalse(pr_explain.asks_about_a_pull_request("what happened in 207"))
        self.assertTrue(pr_explain.asks_about_a_pull_request("what happened in PR 207"))


class GistTest(unittest.TestCase):
    def test_a_size_marker_goes_and_what_it_says_stays(self):
        self.assertTrue(pr_explain.gist(_body(207)).startswith("The connector write path for every org's graph, and account→Person identity links."))
        self.assertTrue(pr_explain.gist(_body(216)).startswith("It changes how every background job runs."))

    def test_measurements_tables_and_headings_are_skipped(self):
        text = pr_explain.gist(_body(207))
        for skipped in ("Measured with", "| page |", "##", "**"):
            self.assertNotIn(skipped, text)
        self.assertLessEqual(len(text), pr_explain.GIST_CHARS + 1)

    def test_a_marker_with_only_a_word_after_it_is_dropped(self):
        self.assertTrue(pr_explain.gist(_body(221)).startswith("1. Rotate the session on promotion (follow-up from the #181 review)."))

    def test_plain_prose_is_kept_as_written(self):
        self.assertTrue(pr_explain.gist(_body(217)).startswith("Puts the original Take the 60-second tour button back, as #201 shipped it."))
        self.assertEqual(pr_explain.gist(""), "")

    def test_when(self):
        self.assertEqual(pr_explain.when("2026-10-18T17:15:37Z"), "18 Oct 2026, 17:15 UTC")
        self.assertEqual(pr_explain.when("soon"), "soon")


if __name__ == "__main__":
    unittest.main()
