"""The blast radius connector, counted from walked routes, pure."""

import unittest

from src.domain.knowledge import algorithms


def _routes(*paths: list[str]) -> list[tuple]:
    """Shortest routes in the shape blast_radius keeps them: (name, length, ids, names), ids from the dependent to the failed node."""
    return [(ids[0].title(), len(ids) - 1, ids, [node.title() for node in ids]) for ids in paths]


class MainConnectorsTest(unittest.TestCase):
    def test_the_node_most_dependents_pass_through_wins_with_a_whole_count(self):
        # a and b depend on c directly; d reaches c only through a; e through a or b.
        shortest = {
            "a": _routes(["a", "c"]),
            "b": _routes(["b", "c"]),
            "d": _routes(["d", "a", "c"]),
            "e": _routes(["e", "a", "c"], ["e", "b", "c"]),
        }
        self.assertEqual(algorithms.main_connectors(shortest, shortest), (["a"], 2))

    def test_a_tie_returns_every_tied_node_by_name(self):
        shortest = {"a": _routes(["a", "c"]), "b": _routes(["b", "c"]), "e": _routes(["e", "b", "c"], ["e", "a", "c"])}
        self.assertEqual(algorithms.main_connectors(shortest, shortest), (["a", "b"], 1))

    def test_only_affected_nodes_count_and_direct_dependents_name_none(self):
        shortest = {"a": _routes(["a", "c"]), "d": _routes(["d", "a", "c"])}
        self.assertEqual(algorithms.main_connectors(shortest, {"d": shortest["d"]}), None)
        self.assertIsNone(algorithms.main_connectors({"a": _routes(["a", "c"]), "b": _routes(["b", "c"])}, {"a": [], "b": []}))


if __name__ == "__main__":
    unittest.main()
