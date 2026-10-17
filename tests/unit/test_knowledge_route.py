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
            "": ("none", {}),
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question), expected)

    def test_depth_words(self):
        self.assertEqual(algorithms.depth_in("what breaks if auth goes down, one hop"), 1)
        self.assertEqual(algorithms.depth_in("and 3 hops out"), 3)
        self.assertEqual(algorithms.depth_in("blast radius of postgres at depth 9"), 6)
        self.assertEqual(algorithms.depth_in("what depends on auth directly"), 1)
        self.assertIsNone(algorithms.depth_in("what breaks if auth goes down"))

    def test_depth_is_clamped_to_an_integer_or_none(self):
        self.assertEqual(algorithms.clamp_depth(0), 1)
        self.assertEqual(algorithms.clamp_depth("4"), 4)
        self.assertEqual(algorithms.clamp_depth(99), algorithms.MAX_DEPTH)
        self.assertIsNone(algorithms.clamp_depth(None))
        self.assertIsNone(algorithms.clamp_depth("deep"))
