"""The live smoke check's pass/fail decision, on canned responses: no network, no server."""

import re
import unittest
import urllib.parse
from pathlib import Path

import tests  # noqa: F401  pins the test graph and owner before src loads

from scripts import check_live  # noqa: E402

CHIPS = ["who owns billing", "what breaks if auth goes down", "who knows the payments gateway"]
# The last chip and the last README question both name it.
REFUSED = "payments gateway"


def _fake(overrides: dict[str, tuple[int, dict | None]] | None = None):
    """A server where every chip and README question answers as expected, unless overridden by path."""
    overrides = overrides or {}
    asked: list[str] = []

    def fetch(path: str):
        asked.append(path)
        if path in overrides:
            return overrides[path]
        if path == "/api/graph/stats":
            return 200, {"company": "quillfern", "nodes": 30, "edges": 70, "labels": {"Service": 9, "Person": 6}}
        if path == "/api/ask/tools":
            return 200, {"company": "quillfern", "examples": CHIPS}
        if path.startswith("/api/ask?"):
            question = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["q"][0]
            return 200, {"tool": "who_knows", "found": REFUSED not in question, "answer": f"answer to {question}"}
        return 200, None

    return fetch, asked


class CheckLiveTest(unittest.TestCase):
    def test_every_chip_but_the_last_answers_and_the_readme_questions_and_pages_are_checked(self):
        fetch, asked = _fake()
        header, checks = check_live.run(fetch)
        self.assertEqual(header, "company=quillfern nodes=30 edges=70 Person=6 Service=9")
        self.assertTrue(all(check.ok for check in checks), [check.line() for check in checks if not check.ok])
        self.assertEqual(len(checks), len(CHIPS) + len(check_live.README_QUESTIONS) + len(check_live.README_PAGES))
        self.assertIn("/api/team/payments", asked)
        self.assertTrue(all(path.startswith("/") for path in asked))

    def test_a_chip_refused_or_a_refusal_answered_fails_with_why(self):
        refused = check_live.judge_answer("chip: x", True, 200, {"tool": "decision_pressure", "found": False, "answer": "Nothing in the graph matches “x”."})
        self.assertFalse(refused.ok)
        self.assertEqual(refused.line(), "FAIL  chip: x  ->  expected an answer; decision_pressure: Nothing in the graph matches “x”.")
        answered = check_live.judge_answer("chip: y", False, 200, {"tool": "who_knows", "found": True, "answer": "Billing Service " * 20})
        self.assertFalse(answered.ok)
        self.assertTrue(answered.detail.startswith("expected a refusal; who_knows: Billing Service"))
        self.assertLessEqual(len(answered.detail), len("expected a refusal; who_knows: ") + check_live.ANSWER_CHARS)

    def test_an_error_status_fails_and_says_so(self):
        throttled = check_live.judge_answer("readme: z", True, 429, {"error": "too many questions"})
        self.assertEqual((throttled.ok, throttled.detail), (False, "HTTP 429 too many questions"))
        fetch, _ = _fake({"/api/team/payments": (404, {"error": "no team"})})
        _, checks = check_live.run(fetch)
        self.assertEqual([check.label for check in checks if not check.ok], ["page: /api/team/payments"])

    def test_an_unreachable_server_fails_at_the_first_request(self):
        fetch, asked = _fake({"/api/graph/stats": (0, {"error": "connection refused"})})
        header, checks = check_live.run(fetch)
        self.assertEqual(asked, ["/api/graph/stats"])
        self.assertEqual([check.ok for check in checks], [False])
        self.assertIn("HTTP 0", header)

    def test_no_chips_listed_is_a_failure(self):
        fetch, _ = _fake({"/api/ask/tools": (200, {"examples": []})})
        _, checks = check_live.run(fetch)
        self.assertEqual(checks[0].label, "/api/ask/tools")
        self.assertFalse(checks[0].ok)

    def test_the_readme_questions_are_the_ones_in_try_it_in_two_minutes(self):
        readme = (Path(__file__).resolve().parents[2] / "README.md").read_text()
        section = readme.split("## Try it in two minutes", 1)[1].split("\n## ", 1)[0]
        self.assertEqual(re.findall(r"^\| `([^`]+)` \|", section, re.MULTILINE), [question for question, _ in check_live.README_QUESTIONS])
        for path in ("/team/payments", "/?q=what%20if%20arjun%20mehta%20leaves"):
            self.assertIn(path, section)
