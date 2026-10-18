"""Query metrics against a real FalkorDB: a real timeout, and a catalogue tool named by its frame.

Runs on a throwaway graph. Needs FalkorDB on localhost:6379 (``docker compose up -d``).
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph before src loads

from redis.exceptions import ResponseError  # noqa: E402

from src.core import query_metrics, tenancy  # noqa: E402
from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import catalogue  # noqa: E402
from src.domain.knowledge.mcp_tools import ORG_SHARED  # noqa: E402

GRAPH = f"ops_metrics_test{tests.TEST_GRAPH_SUFFIX}"


class LiveMetricsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patch = mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH)
        cls._patch.start()
        _, graph = falkordb_client.connect()
        try:
            graph.delete()
        except ResponseError:
            pass
        graph.query("UNWIND range(1, 3000) AS i CREATE (:N {i: i})")

    @classmethod
    def tearDownClass(cls):
        _, graph = falkordb_client.connect()
        graph.delete()
        cls._patch.stop()

    def setUp(self):
        query_metrics.reset()
        query_metrics.install()
        self.addCleanup(query_metrics.uninstall)
        self.addCleanup(query_metrics.reset)

    def test_a_query_past_its_timeout_counts_as_a_timeout(self):
        _, graph = falkordb_client.connect()
        with query_metrics.tool_scope("stress"), self.assertLogs(query_metrics.log) as logs, falkordb_client.query_timeout(1):
            with self.assertRaises(ResponseError):
                graph.ro_query("MATCH (a:N), (b:N) WHERE (a.i * b.i) % 7 = 3 RETURN count(*)")
        stats = query_metrics.snapshot()["tools"]["stress"]
        self.assertEqual((stats["count"], stats["timeouts"]), (1, 1))
        self.assertIn("timeout query", logs.output[0])
        self.assertIn("timeout=1:", logs.output[0])

    def test_a_catalogue_tool_is_counted_under_its_name_and_org(self):
        with tenancy.org_context(tenancy.legacy_org()):
            catalogue.get_tool("who_owns").run({"name": "nothing by this name"}, [ORG_SHARED])
        snap = query_metrics.snapshot()
        self.assertIn("who_owns", snap["tools"], snap["tools"])
        self.assertEqual(set(snap["tools"]), {"who_owns"}, "every query the tool ran is the tool's")
        self.assertEqual(snap["orgs"][tenancy.legacy_org().id]["count"], snap["total"]["count"])


if __name__ == "__main__":
    unittest.main()
