"""``resolve.load_rows`` on a real FalkorDB: kept between questions, read again after any write."""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import resolve  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_resolve_cache_test{tests.TEST_GRAPH_SUFFIX}"


class ResolveRowsCacheTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH), mock.patch.object(seed, "graph_name", return_value=GRAPH)]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def test_a_new_node_resolves_on_the_next_question(self):
        _, graph = falkordb_client.connect()
        before = resolve.load_rows(graph, resolve.DEFAULT_LABELS, ["org_shared"])
        self.assertIs(resolve.load_rows(graph, resolve.DEFAULT_LABELS, ["org_shared"]), before)
        self.assertFalse(resolve.resolve_name("who owns the ledger service").found)
        graph.query("CREATE (:Service {id: 'ledger', name: 'Ledger Service', aliases: 'ledger'})")
        try:
            self.assertEqual(resolve.resolve_name("who owns the ledger service").entity.id, "ledger")
        finally:
            graph.query("MATCH (s:Service {id: 'ledger'}) DELETE s")
        self.assertFalse(resolve.resolve_name("who owns the ledger service").found)

    def test_rows_match_an_all_node_scan(self):
        _, graph = falkordb_client.connect()
        scopes = ["org_shared", "org_internal"]
        scanned = graph.ro_query(
            f"""
            MATCH (n) WHERE labels(n)[0] IN $labels AND n.name IS NOT NULL AND {resolve.SCOPED.format(var="n")}
            RETURN n.id, labels(n)[0]
            """,
            {"labels": list(resolve.DEFAULT_LABELS), "scopes": scopes},
        ).result_set
        self.assertEqual([(row.id, row.label) for row in resolve.load_rows(graph, resolve.DEFAULT_LABELS, scopes)], [tuple(row) for row in scanned])


if __name__ == "__main__":
    unittest.main()
