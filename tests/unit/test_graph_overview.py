"""The hero endpoints' shapes and limit, pure."""

import unittest

from src.domain.knowledge import graph_overview
from src.domain.knowledge.ask_api import GraphEdge


class GraphOverviewShapesTest(unittest.TestCase):
    def test_the_sample_limit_is_clamped(self):
        self.assertEqual(graph_overview.clamp_sample(48), 48)
        self.assertEqual(graph_overview.clamp_sample(1000), graph_overview.MAX_SAMPLE)
        self.assertEqual(graph_overview.clamp_sample(0), 1)
        self.assertEqual(graph_overview.clamp_sample("many"), graph_overview.DEFAULT_SAMPLE)
        self.assertEqual(graph_overview.clamp_sample(None), graph_overview.DEFAULT_SAMPLE)

    def test_stats_leave_out_the_company_when_the_graph_has_none(self):
        stats = graph_overview.GraphStats(nodes=0, edges=0, labels={}, kinds={}, rels=0, tools=3)
        self.assertEqual(stats.to_dict(), {"nodes": 0, "edges": 0, "labels": {}, "kinds": {}, "rels": 0, "tools": 3})
        named = graph_overview.GraphStats(nodes=1, edges=0, labels={"Team": 1}, kinds={}, rels=0, tools=3, company="acme", company_name="Acme")
        self.assertEqual((named.to_dict()["company"], named.to_dict()["company_name"]), ("acme", "Acme"))

    def test_the_sample_uses_the_path_drawing_edge_shape(self):
        sample = graph_overview.GraphSample(
            [graph_overview.SampleNode("auth", "Auth Service", "Service", "service"), graph_overview.SampleNode("maya", "Maya Ortiz", "Person")],
            [GraphEdge("maya", "OWNS", "auth")],
        )
        self.assertEqual(
            sample.to_dict(),
            {
                "nodes": [
                    {"id": "auth", "name": "Auth Service", "label": "Service", "kind": "service"},
                    {"id": "maya", "name": "Maya Ortiz", "label": "Person", "kind": None},
                ],
                "edges": [{"source": "maya", "rel": "OWNS", "target": "auth"}],
            },
        )


if __name__ == "__main__":
    unittest.main()
