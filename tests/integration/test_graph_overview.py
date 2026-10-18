"""The hero's graph endpoints on Quillfern with its Sales team: real counts, a connected sample, public reads only.

The setup adds what a public page must not show or count: a private channel's memory, a
forgotten memory, a private repo's pull request and a private service. It adds a public
memory and a public pull request too, which do count.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from fastapi.testclient import TestClient  # noqa: E402

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import algorithms, ask_api, graph_overview  # noqa: E402
from src.main import app  # noqa: E402
from src.seed import quillfern, quillfern_sales, seed  # noqa: E402

GRAPH = f"company_graph_overview_test{tests.TEST_GRAPH_SUFFIX}"
PRIVATE = ("m-private", "m-forgotten", "pr-private", "secret-ledger")


class GraphOverviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        quillfern_sales.seed_sales()
        _, graph = falkordb_client.connect()
        graph.query(
            """
            MATCH (billing:Service {id: 'billing-service'}), (arjun:Person {id: 'arjun'})
            CREATE (:Memory {id: 'm-public', text: 'renewal retries look flaky again', scope: 'org_shared'})-[:MENTIONS]->(billing),
                   (:Memory {id: 'm-private', text: 'the acquisition closes in March', scope: 'slack_channel_C3'})-[:MENTIONS]->(billing),
                   (:Memory {id: 'm-forgotten', text: 'an old note', scope: 'org_shared', is_forgotten: true})-[:MENTIONS]->(billing),
                   (:PullRequest {id: 'pr-public', name: 'app#41', scope: 'org_shared'})-[:TOUCHES]->(billing),
                   (:PullRequest {id: 'pr-private', name: 'vault#7', scope: 'github_repo_9'})-[:TOUCHES]->(billing),
                   (arjun)-[:OWNS]->(:Service {id: 'secret-ledger', name: 'Secret Ledger', scope: 'slack_channel_C3'})
            """
        )
        cls.http = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def setUp(self):
        ask_api.ANSWERS.clear()
        ask_api.LIMITER.reset()

    def get(self, path: str) -> dict:
        response = self.http.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_stats_count_only_what_a_public_channel_reads(self):
        stats = self.get("/api/graph/stats")
        self.assertEqual((stats["company"], stats["company_name"]), ("quillfern", "Quillfern"))
        # The private service, the private and the forgotten memory and the private repo's pull request are not counted.
        self.assertEqual(
            stats["labels"],
            {"Person": 9, "Team": 4, "Service": 13, "Document": 4, "Ticket": 3, "Decision": 5, "PullRequest": 1, "Memory": 7, "Chat": 1},
        )
        self.assertEqual(stats["nodes"], sum(stats["labels"].values()))
        self.assertEqual(stats["kinds"], {"service": 9, "system": 1, "process": 1, "account": 1, "playbook": 1})
        # Quillfern and Sales have 115 edges between public nodes: two are the seeded chat's AUTHORED and
        # ABOUT, 15 the seeded memories' MENTIONS. The public memory and pull request here add one each.
        self.assertEqual(stats["edges"], 117)
        self.assertEqual(stats["rels"], 15)
        self.assertEqual(stats["tools"], len(ask_api.read_tools()))

    def test_the_sample_is_connected_readable_and_public(self):
        sample = self.get("/api/graph/sample?limit=48")
        ids = {node["id"] for node in sample["nodes"]}
        self.assertLessEqual(len(ids), 48)
        self.assertEqual(len(ids), len(sample["nodes"]))
        for node in sample["nodes"]:
            self.assertEqual(set(node), {"id", "name", "label", "kind"})
            self.assertIn(node["label"], graph_overview.SAMPLE_LABELS)
            self.assertEqual(node["kind"] is None, node["label"] != "Service", node)
        for edge in sample["edges"]:
            self.assertEqual(set(edge), {"source", "rel", "target"})
            self.assertIn(edge["source"], ids)
            self.assertIn(edge["target"], ids)
        # One picture: every node is on an edge.
        self.assertEqual(ids, {end for edge in sample["edges"] for end in (edge["source"], edge["target"])})
        self.assertFalse(ids & set(PRIVATE))
        self.assertNotIn("m-public", ids)
        self.assertNotIn("pr-public", ids)
        self.assertLessEqual({"Person", "Team", "Service", "Decision"}, {node["label"] for node in sample["nodes"]})
        self.assertLessEqual({"DEPENDS_ON", "OWNS", "RUNS", "MEMBER_OF", "PROPOSED"}, {edge["rel"] for edge in sample["edges"]})

    def test_the_sample_starts_from_the_most_depended_on_service_and_is_the_same_every_time(self):
        first = graph_overview.graph_sample(12)
        self.assertTrue(algorithms.load_bearing(1).answer.startswith(f"{first.nodes[0].name} carries the most weight"))
        self.assertLessEqual(len(first.nodes), 12)
        self.assertEqual(graph_overview.graph_sample(12), first)
        self.assertEqual(self.get("/api/graph/sample?limit=12"), first.to_dict())
        # The default, and a limit past the cap, are clamped.
        self.assertLessEqual(len(self.get("/api/graph/sample")["nodes"]), graph_overview.DEFAULT_SAMPLE)
        self.assertLessEqual(len(self.get("/api/graph/sample?limit=1000")["nodes"]), graph_overview.MAX_SAMPLE)

    def test_the_routes_are_cached_and_not_counted_against_the_public_rate_limit(self):
        with mock.patch.object(ask_api, "LIMITER", ask_api.RateLimiter(limit=2, window=60)):
            headers = {"X-Real-IP": "203.0.113.9"}
            for _ in range(5):
                for path in ("/api/graph/stats", "/api/graph/sample"):
                    response = self.http.get(path, headers=headers)
                    self.assertEqual(response.status_code, 200, path)
                    self.assertEqual(response.headers["cache-control"], ask_api.CACHE_CONTROL)
        ask_api.ANSWERS.clear()
        with mock.patch.object(graph_overview, "graph_stats", wraps=graph_overview.graph_stats) as build:
            self.get("/api/graph/stats")
            self.get("/api/graph/stats")
        self.assertEqual(build.call_count, 1)
        self.assertEqual(self.http.get("/api/graph/sample?limit=many").status_code, 422)


if __name__ == "__main__":
    unittest.main()
