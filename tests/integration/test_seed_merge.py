"""Refreshing a seed in place keeps what the graph learned since.

``seed_company`` merges by id; ``reset_and_seed`` deletes first. Runs on a throwaway
graph. Needs FalkorDB on localhost:6379 (``docker compose up -d``).
"""

import dataclasses
import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api import ops  # noqa: E402
from src.api.v1 import slack_install  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.db import falkordb_client  # noqa: E402
from src.domain.memory import memory_service  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_seed_merge_test{tests.TEST_GRAPH_SUFFIX}"
TOKEN = "seed-test-admin-token"


def _count(query: str, **params) -> int:
    _, graph = falkordb_client.connect()
    return int(graph.query(query, params).result_set[0][0])


class SeedMergeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def setUp(self):
        seed.reset_and_seed(quillfern)
        _, graph = falkordb_client.connect()
        # What a used workspace leaves behind: a memory, and a decision that is not in the seed.
        graph.query("CREATE (:Memory {id: 'mem-1', content: 'billing alerts are noisy', scope: 'org_shared'})")
        graph.query("MATCH (d:Decision {id: 'decision-billing-retries'}) CREATE (:Decision {id: 'decision-live', name: 'Live one'})-[:SUPERSEDES]->(d)")
        graph.query("MATCH (p:Person {id: 'arjun'}) SET p.name = 'Arjun M.'")

    def test_merge_keeps_history_and_refreshes_the_seed(self):
        before = _count("MATCH (n) RETURN count(n)")
        report = seed.seed_company(quillfern)
        self.assertEqual(report.nodes, before, "merging the same seed again adds no nodes")
        self.assertEqual((report.company, report.nodes_created, report.edges_created), ("quillfern", 0, 0))
        self.assertEqual(report.properties_set, 1, "only the renamed person is written back")
        self.assertEqual(seed.seed_company(quillfern).properties_set, 0, "a second merge changes nothing")
        self.assertEqual(_count("MATCH (m:Memory {id: 'mem-1'}) RETURN count(m)"), 1)
        self.assertEqual(_count("MATCH (:Decision {id: 'decision-live'})-[:SUPERSEDES]->(:Decision) RETURN count(*)"), 1)
        self.assertEqual(_count("MATCH (p:Person {id: 'arjun', name: 'Arjun Mehta'}) RETURN count(p)"), 1, "seeded fields are refreshed")
        self.assertEqual(_count("MATCH (d:Decision) WHERE d.decided_at IS NOT NULL RETURN count(d)"), len(quillfern.DECISIONS))
        self.assertEqual(_count("MATCH (:Decision)-[:SUPERSEDES]->(:Decision) RETURN count(*)"), 2, "seeded and live supersessions both stand")

    def test_merge_adds_only_what_the_seed_gained(self):
        _, graph = falkordb_client.connect()
        graph.query("MATCH (d:Decision {id: 'decision-mentions-recall'}) DETACH DELETE d")  # not in Quillfern; a no-op guard
        graph.query("MATCH (d:Decision {id: 'decision-billing-retries'}) DETACH DELETE d")
        report = seed.seed_company(quillfern)
        self.assertEqual(report.nodes_created, 1, "the one missing decision comes back")
        self.assertGreaterEqual(report.edges_created, 5, "with its proposer, approver, service and sources")
        self.assertEqual(_count("MATCH (m:Memory {id: 'mem-1'}) RETURN count(m)"), 1)

    def test_reset_drops_history(self):
        seed.reset_and_seed(quillfern)
        self.assertEqual(_count("MATCH (m:Memory {id: 'mem-1'}) RETURN count(m)"), 0)
        self.assertEqual(_count("MATCH (m:Memory) RETURN count(m)"), len(quillfern.MEMORIES), "only the seed's own memories")
        self.assertEqual(_count("MATCH (d:Decision {id: 'decision-live'}) RETURN count(d)"), 0)

    def test_seeded_memories_are_written_the_way_slack_ingest_writes_them(self):
        _, graph = falkordb_client.connect()
        names = {person["id"]: person["name"] for person in quillfern.PEOPLE}
        for memory in quillfern.MEMORIES:
            with self.subTest(memory=memory["id"]):
                rows = graph.query(
                    """
                    MATCH (u:SlackUser)-[:SAID]->(m:Memory {id: $id})-[:IN_SCOPE]->(s:Scope)
                    RETURN m.text, m.scope, s.id, m.created_at, m.ts, m.channel_id, m.is_forgotten, u.id, u.name
                    """,
                    {"id": memory["id"]},
                ).result_set
                self.assertEqual(
                    rows,
                    [[memory["text"], "org_shared", "org_shared", memory["said_at"], f"{memory['said_at'] / 1000:.6f}", None, False, memory["slack_user"], names[memory["author"]]]],
                )
                mentioned = graph.query("MATCH (:Memory {id: $id})-[:MENTIONS]->(n) RETURN n.id", {"id": memory["id"]}).result_set
                expected = memory_service.mentioned_ids(graph, memory["text"], ["org_shared"])
                self.assertTrue(expected, "every seeded memory names something in the graph")
                self.assertEqual(sorted(row[0] for row in mentioned), sorted(expected))

    def test_a_merge_creates_no_memory_twice_and_keeps_what_slack_changed(self):
        _, graph = falkordb_client.connect()
        graph.query("MATCH (m:Memory {id: 'memory-auth-token-rotation'}) SET m.is_forgotten = true")
        graph.query("MATCH (u:SlackUser {id: 'UMAYA'}) SET u.name = 'maya.o'")
        report = seed.seed_company(quillfern)
        self.assertEqual((report.nodes_created, report.edges_created, report.properties_set), (0, 0, 1), "only Arjun's seeded name")
        self.assertEqual(_count("MATCH (m:Memory {id: 'memory-auth-token-rotation', is_forgotten: true}) RETURN count(m)"), 1)
        self.assertEqual(_count("MATCH (u:SlackUser {id: 'UMAYA', name: 'maya.o'}) RETURN count(u)"), 1)
        self.assertEqual(_count("MATCH (u:SlackUser) RETURN count(u)"), len({memory["slack_user"] for memory in quillfern.MEMORIES}))

    def test_ops_seed_route_merges_behind_the_admin_token(self):
        app = FastAPI()
        ops.install(app)
        client = TestClient(app)
        unset = dataclasses.replace(settings, brain_admin_token="", seed_company="quillfern")
        with mock.patch.object(slack_install, "settings", unset), mock.patch.object(ops, "settings", unset):
            self.assertEqual(client.post("/ops/seed").status_code, 503)
        armed = dataclasses.replace(settings, brain_admin_token=TOKEN, seed_company="quillfern")
        with mock.patch.object(slack_install, "settings", armed), mock.patch.object(ops, "settings", armed):
            self.assertEqual(client.post("/ops/seed").status_code, 403)
            self.assertEqual(client.post("/ops/seed", headers={"Authorization": "Bearer nope"}).status_code, 403)
            response = client.post("/ops/seed", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["company"], "quillfern")
        self.assertEqual(body["mode"], "merge")
        self.assertEqual(_count("MATCH (m:Memory {id: 'mem-1'}) RETURN count(m)"), 1, "the route never deletes")
        self.assertEqual(body["nodes"], _count("MATCH (n) RETURN count(n)"))
        self.assertEqual((body["nodes_created"], body["edges_created"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
