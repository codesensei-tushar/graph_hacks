"""The dark graph: every read returns nothing, no connection is opened, writes are logged and dropped."""

import unittest
from unittest import mock

from src.db import falkordb_client


class DarkGraphTest(unittest.TestCase):
    def test_reads_return_empty_results_without_a_connection(self):
        with mock.patch.object(falkordb_client, "client", side_effect=AssertionError("a connection was opened")), falkordb_client.dark_graph():
            db, graph = falkordb_client.connect()
            self.assertIsNone(db)
            self.assertEqual(graph.ro_query("MATCH (n) RETURN n").result_set, [])
            self.assertEqual(graph.query("MATCH (n) RETURN n", {"x": 1}).result_set, [])
            self.assertEqual(graph.query("MATCH (n) RETURN count(n)").result_set, [])
            self.assertTrue(falkordb_client.graph_reads_disabled.get())
        self.assertFalse(falkordb_client.graph_reads_disabled.get())

    def test_a_write_is_logged_and_writes_nothing(self):
        with falkordb_client.dark_graph(), self.assertLogs(falkordb_client.log, level="WARNING") as logged:
            _, graph = falkordb_client.connect()
            result = graph.query("CREATE (t:Ticket {id: $id})", {"id": "x"})
        self.assertEqual(result.result_set, [])
        self.assertEqual(result.nodes_created, 0)
        self.assertIn("nothing was written", logged.output[0])

    def test_the_switch_is_restored_after_an_error(self):
        with self.assertRaises(RuntimeError):
            with falkordb_client.dark_graph():
                raise RuntimeError("boom")
        self.assertFalse(falkordb_client.graph_reads_disabled.get())
