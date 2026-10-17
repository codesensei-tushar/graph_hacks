"""The keyword router and the depth parser, pure."""

import unittest

from src.domain.knowledge import algorithms, ask_service


class RouteTest(unittest.TestCase):
    def test_each_tool_has_a_keyword_route(self):
        cases = {
            "who owns billing": ("who_owns", {"name": "who owns billing"}),
            "who owns billing, and who else has worked on it": ("who_knows", {"name": "who owns billing, and who else has worked on it"}),
            "why did we move the memory layer off postgres": ("decision_trace", {"topic": "why did we move the memory layer off postgres"}),
            "what breaks if auth goes down": ("blast_radius", {"name": "what breaks if auth goes down"}),
            "what breaks if auth goes down, two hops out": ("blast_radius", {"name": "what breaks if auth goes down, two hops out", "depth": 2}),
            "which services are load bearing": ("load_bearing", {"limit": algorithms.DEFAULT_LOAD_BEARING}),
            "how is maya connected to the invoice worker": ("path_between", {"source": "how is maya connected to the invoice worker", "target": ""}),
            "what have we said about billing": ("related_memories", {"name": "what have we said about billing"}),
            "who said the retries were flaky": ("recall", {"query": "who said the retries were flaky"}),
            "why did we cut vector recall": ("decision_trace", {"topic": "why did we cut vector recall"}),
            "is the billing retries decision still the last word": (
                "decision_pressure",
                {"topic": "is the billing retries decision still the last word"},
            ),
            "who actually works together": ("working_groups", {}),
            "what breaks if arjun leaves": ("if_person_leaves", {"name": "what breaks if arjun leaves"}),
            "": ("none", {}),
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question), expected)

    def test_a_person_leaving_is_not_a_service_going_down(self):
        for question in (
            "what breaks if Arjun leaves",
            "what happens if arjun leaves",
            "what if lena quits",
            "who covers for sara",
            "who takes over from kofi",
            "what if maya is on leave",
            "what is Arjun the only one who knows",
            "what does tomas know that nobody else does",
            "what's the bus factor on billing's owner",
        ):
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question)[0], "if_person_leaves")
        # A service-shaped "only one who knows" question still asks who knows the service.
        self.assertEqual(ask_service.route("who is the only one who knows billing")[0], "who_knows")
        self.assertEqual(ask_service.route("what breaks if the claude api goes down, one hop")[0], "blast_radius")
        self.assertEqual(ask_service.route("what is left uncited after the billing retries decision")[0], "decision_pressure")

    def test_depth_words(self):
        self.assertEqual(algorithms.depth_in("what breaks if auth goes down, one hop"), 1)
        self.assertEqual(algorithms.depth_in("and 3 hops out"), 3)
        self.assertEqual(algorithms.depth_in("blast radius of postgres at depth 9"), 6)
        self.assertEqual(algorithms.depth_in("what depends on auth directly"), 1)
        self.assertIsNone(algorithms.depth_in("what breaks if auth goes down"))
        self.assertEqual(algorithms.depth_in("what breaks if the claude api goes down, one hop"), 1)
        self.assertEqual(algorithms.depth_in("and three hops out"), 3)
        self.assertIsNone(algorithms.depth_in("all the way"))

    def test_depth_is_clamped_to_an_integer_or_none(self):
        self.assertEqual(algorithms.clamp_depth(0), 1)
        self.assertEqual(algorithms.clamp_depth("4"), 4)
        self.assertEqual(algorithms.clamp_depth(99), algorithms.MAX_DEPTH)
        self.assertIsNone(algorithms.clamp_depth(None))
        self.assertIsNone(algorithms.clamp_depth("deep"))
