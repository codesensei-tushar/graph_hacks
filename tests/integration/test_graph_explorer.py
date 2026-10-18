"""The graph explorer on the Quillfern graph: its three routes, the public scope, the rate limit and cache, and the page.

One ticket is scoped to a private channel and is about Billing Service: the explorer reads what
a public channel reads, so it never shows that ticket, never resolves it, and never walks to it.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from fastapi.testclient import TestClient  # noqa: E402

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import algorithms, ask_api, explorer  # noqa: E402
from src.main import app  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_graph_explorer_test{tests.TEST_GRAPH_SUFFIX}"
PRIVATE = "slack_channel_C9"


class GraphExplorerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        _, graph = falkordb_client.connect()
        graph.query(
            """
            MATCH (billing:Service {id: 'billing-service'})
            CREATE (:Ticket {id: 'TKT-900', name: 'TKT-900', title: 'Private refund bug', scope: $private})-[:ABOUT]->(billing)
            """,
            {"private": PRIVATE},
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

    def test_a_node_by_id_comes_with_its_neighbourhood_drawn_and_what_to_follow(self):
        view = self.get("/api/explore?node=billing-service")
        self.assertEqual((view["tool"], view["found"], view["depth"], view["scopes"]), ("explore", True, 1, ["org_shared"]))
        self.assertEqual(view["focus"], {"id": "billing-service", "name": "Billing Service", "label": "Service", "kind": "service"})
        self.assertTrue(view["answer"].startswith("Billing Service has 13 direct connections: owned by Arjun Mehta;"), view["answer"])
        ids = {node["id"] for node in view["nodes"]}
        self.assertIn("billing-service", ids)
        self.assertIn({"source": "arjun", "rel": "OWNS", "target": "billing-service"}, view["edges"])
        self.assertIn({"from": "Billing Service", "rel": "DEPENDS_ON", "to": "Auth Service"}, view["path"])
        self.assertEqual(len(view["edges"]), len(view["path"]))
        follows = {(follow["rel"], follow["direction"]): (follow["phrase"], follow["count"]) for follow in view["follows"]}
        self.assertEqual(follows[("OWNS", "in")], ("owned by", 1))
        self.assertEqual(follows[("DEPENDS_ON", "out")], ("depends on", 2))
        self.assertEqual(follows[("DEPENDS_ON", "in")], ("depended on by", 2))
        self.assertEqual(follows[("ABOUT", "in")], ("decisions and tickets about it", 2))
        self.assertEqual(view["memories"][0]["learned_at"], "2026-09-20")

    def test_a_name_resolves_as_the_ask_box_resolves_it_and_depth_two_looks_further(self):
        team = self.get("/api/explore?node=the%20platform%20team")
        self.assertEqual(team["focus"]["id"], "platform")
        self.assertTrue(team["answer"].startswith("The Platform team has 7 direct connections"), team["answer"])
        near = self.get("/api/explore?node=billing")
        far = self.get("/api/explore?node=billing&depth=2")
        self.assertEqual(far["depth"], 2)
        self.assertGreater(len(far["nodes"]), len(near["nodes"]))
        self.assertIn("Two hops out there are", far["answer"])
        self.assertEqual(self.get("/api/explore?node=billing&depth=9")["depth"], 2)

    def test_a_miss_names_the_nearest_nodes_with_their_ids(self):
        miss = self.get("/api/explore?node=the%20payments%20gateway")
        self.assertFalse(miss["found"])
        self.assertIsNone(miss["focus"])
        self.assertTrue(miss["answer"].startswith("Nothing in the graph matches “payments gateway” as one thing."), miss["answer"])
        self.assertEqual(
            miss["candidates"][:2],
            [
                {"id": "billing-service", "name": "Billing Service", "label": "Service", "via": "payments"},
                {"id": "api-gateway", "name": "API Gateway", "label": "Service", "via": "gateway"},
            ],
        )
        empty = self.get("/api/explore?node=")
        self.assertEqual((empty["found"], empty["answer"]), (False, "Type the name of a service, person, team, document, ticket or decision."))
        self.assertEqual(self.http.get("/api/explore?node=" + "x" * 301).status_code, 400)
        self.assertEqual(self.http.get("/api/explore/neighbors?relation=OWNS&node=" + "x" * 301).status_code, 400)

    def test_neighbors_walks_one_relationship_and_agrees_with_blast_radius(self):
        walked = self.get("/api/explore/neighbors?node=auth-service&relation=DEPENDS_ON&direction=in&depth=2")
        self.assertEqual((walked["tool"], walked["found"], walked["relation"], walked["direction"], walked["depth"]), ("neighbors", True, "DEPENDS_ON", "in", 2))
        self.assertEqual(walked["focus"]["id"], "auth-service")
        blast = algorithms.blast_radius("what breaks if auth goes down", ["org_shared"], 2)
        broken = {node_id for step in blast.path for node_id in (step.from_id, step.to_id)}
        self.assertEqual({node["id"] for node in walked["nodes"]}, broken)
        refused = self.get("/api/explore/neighbors?node=auth-service&relation=LIKES")
        self.assertFalse(refused["found"])
        self.assertTrue(refused["answer"].startswith("“LIKES” is not a relationship neighbors follows."), refused["answer"])
        self.assertIsNone(refused["relation"])
        missing = self.get("/api/explore/neighbors?node=the%20payments%20gateway&relation=OWNS")
        self.assertEqual([candidate["id"] for candidate in missing["candidates"][:2]], ["billing-service", "api-gateway"])

    def test_the_explorer_reads_what_a_public_channel_reads(self):
        billing = self.get("/api/explore?node=billing-service")
        self.assertNotIn("TKT-900", {node["id"] for node in billing["nodes"]})
        self.assertNotIn("TKT-900", billing["answer"])
        self.assertFalse(self.get("/api/explore?node=TKT-900")["found"])
        about = self.get("/api/explore/neighbors?node=billing-service&relation=ABOUT&direction=in")
        self.assertEqual({node["id"] for node in about["nodes"]} - {"billing-service"}, {"TKT-102", "decision-billing-retries"})
        # The tools themselves see it with the channel's scope: the explorer's limit is its scope, not the data.
        self.assertTrue(explorer.explore_view("TKT-900", 1, scopes=("org_shared", PRIVATE)).found)
        starters = self.get("/api/explore/start")["starters"]
        self.assertEqual(starters[0], {"id": "billing-service", "name": "Billing Service", "label": "Service", "kind": "service"})
        self.assertEqual([node["label"] for node in starters], ["Service", "Service", "Team", "Team", "Person", "Person", "Decision", "Decision"])

    def test_routes_share_the_public_rate_limit_and_cache(self):
        with mock.patch.object(ask_api, "LIMITER", ask_api.RateLimiter(limit=2, window=60)):
            headers = {"X-Real-IP": "203.0.113.9"}
            self.assertEqual(self.http.get("/api/explore?node=billing", headers=headers).status_code, 200)
            self.assertEqual(self.http.get("/api/explore/start", headers=headers).status_code, 200)
            throttled = self.http.get("/api/explore/neighbors?node=billing&relation=OWNS", headers=headers)
            self.assertEqual(throttled.status_code, 429)
            self.assertIn("Retry-After", throttled.headers)
        with mock.patch.object(explorer, "explore_view", wraps=explorer.explore_view) as build:
            self.get("/api/explore?node=auth-service")
            self.get("/api/explore?node=auth-service")
        self.assertEqual(build.call_count, 1)

    def test_the_page_is_served_with_its_scripts_and_linked_from_the_landing_page(self):
        page = self.http.get("/explore?node=billing-service")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["content-type"].split(";")[0], "text/html")
        for asset in ("/static/explorer.js", "/static/explorer.css", "/static/graph.js", "/static/shell.js", "/static/style.css"):
            self.assertIn(asset, page.text)
            self.assertEqual(self.http.get(asset.split("?")[0]).status_code, 200, asset)
        self.assertIn('href="/explore"', self.http.get("/").text)
        self.assertEqual(self.http.get("/api/explore/nowhere").status_code, 404)
