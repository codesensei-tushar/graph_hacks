"""The graph generation: changing writes through ``connect()``'s proxy bump it, reads and no-op
writes do not, and within one question Redis is read once per graph."""

import unittest
from unittest import mock

from src.db import falkordb_client
from src.db.falkordb_client import DarkGraph, _GraphProxy


class FakeKv:
    def __init__(self):
        self.values: dict[str, int] = {}
        self.gets = 0

    def get(self, key):
        self.gets += 1
        return self.values.get(key)

    def incr(self, key):
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]


class Result:
    def __init__(self, **stats):
        self.result_set = []
        for stat in falkordb_client._CHANGES:
            setattr(self, stat, stats.get(stat, 0))


class FakeGraph:
    name = "g_test"

    def __init__(self, result):
        self.result = result
        self.deleted = False

    def query(self, q, params=None, timeout=None):
        return self.result

    def ro_query(self, q, params=None, timeout=None):
        return self.result

    def delete(self):
        self.deleted = True


class GraphGenerationTest(unittest.TestCase):
    def setUp(self):
        self.kv = FakeKv()
        patch = mock.patch.object(falkordb_client, "kv", return_value=self.kv)
        patch.start()
        self.addCleanup(patch.stop)

    def generation(self):
        return falkordb_client.generation("g_test")

    def test_a_changing_write_bumps_it_and_a_read_or_a_no_op_write_does_not(self):
        self.assertEqual(self.generation(), 0)
        _GraphProxy(FakeGraph(Result())).query("MATCH (n) SET n.x = n.x")
        _GraphProxy(FakeGraph(Result(nodes_created=1))).ro_query("MATCH (n) RETURN n")
        self.assertEqual(self.generation(), 0)
        for stat in ("nodes_created", "nodes_deleted", "relationships_deleted", "properties_set", "labels_added"):
            _GraphProxy(FakeGraph(Result(**{stat: 1}))).query("...")
        self.assertEqual(self.generation(), 5)

    def test_deleting_the_graph_bumps_it_and_the_proxy_keeps_the_graph_name(self):
        inner = FakeGraph(Result())
        proxy = _GraphProxy(inner)
        self.assertEqual(proxy.name, "g_test")
        proxy.delete()
        self.assertTrue(inner.deleted)
        self.assertEqual(self.generation(), 1)

    def test_a_snapshot_reads_redis_once_until_a_write(self):
        with falkordb_client.generation_snapshot():
            self.assertEqual([self.generation() for _ in range(3)], [0, 0, 0])
            self.assertEqual(self.kv.gets, 1)
            _GraphProxy(FakeGraph(Result(properties_set=2))).query("...")
            self.assertEqual(self.generation(), 1)
            self.assertEqual(self.kv.gets, 2)
        self.generation()
        self.assertEqual(self.kv.gets, 3)

    def test_a_dark_graph_never_bumps_it(self):
        DarkGraph("g_test").query("CREATE (n)")
        self.assertEqual(self.generation(), 0)

    def test_redis_down_means_no_generation_and_no_failure(self):
        with mock.patch.object(falkordb_client, "kv", side_effect=ConnectionError("down")):
            self.assertIsNone(falkordb_client.generation("g_test"))
            falkordb_client.bump_generation("g_test")


class CacheKeyTest(unittest.TestCase):
    def test_only_public_answers_are_cached(self):
        from src.domain.knowledge import ask_api

        ask_api.ANSWERS.clear()
        with (
            mock.patch.object(falkordb_client, "graph_name", return_value="g_test"),
            mock.patch.object(falkordb_client, "generation", return_value=7),
            mock.patch.object(ask_api, "_answer", side_effect=lambda *args: object()) as answer,
        ):
            first = ask_api.answer_question("who owns billing")
            self.assertIs(ask_api.answer_question("who owns billing"), first)
            ask_api.answer_question("who owns billing", scopes=("org_shared", "org_internal"))
            ask_api.answer_question("who owns billing", scopes=("org_shared", "org_internal"))
            self.assertEqual(answer.call_count, 3)
            self.assertEqual(ask_api.cache_key("x"), ("g_test", 7, "x"))
        ask_api.ANSWERS.clear()


if __name__ == "__main__":
    unittest.main()
