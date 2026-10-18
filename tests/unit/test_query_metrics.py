"""Graph query metrics (src/core/query_metrics.py) and GET /ops/metrics. No database."""

import dataclasses
import logging
import unittest
from unittest import mock

import tests  # noqa: F401  pins the test settings before src loads

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from redis.exceptions import ConnectionError as RedisConnectionError  # noqa: E402
from redis.exceptions import ResponseError  # noqa: E402
from redis.exceptions import TimeoutError as RedisTimeoutError  # noqa: E402

from src.api import ops  # noqa: E402
from src.api.v1 import slack_install  # noqa: E402
from src.core import query_metrics, tenancy  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.db import falkordb_client  # noqa: E402
from src.db.falkordb_client import QueryCall, _GraphProxy  # noqa: E402

TOKEN = "metrics-test-admin-token"


class Result:
    def __init__(self, rows: int) -> None:
        self.result_set = [[i] for i in range(rows)]


class FakeGraph:
    name = "fake_graph"

    def __init__(self, rows: int = 3, fail: Exception | None = None) -> None:
        self.rows, self.fail = rows, fail

    def query(self, q, params=None, timeout=None):
        if self.fail:
            raise self.fail
        return Result(self.rows)

    ro_query = query


def call(duration: float, query: str = "MATCH (n) RETURN n", error=None, rows: int = 0) -> QueryCall:
    return QueryCall("g", "ro_query", query, {"name": "secret user text"}, 2000, duration_ms=duration, result=Result(rows), error=error)


class MetricsTestCase(unittest.TestCase):
    def setUp(self):
        query_metrics.reset()
        query_metrics.install()
        self.addCleanup(query_metrics.uninstall)
        self.addCleanup(query_metrics.reset)


class HookTest(MetricsTestCase):
    def test_every_query_through_the_proxy_is_counted_under_its_tool_and_org(self):
        graph = _GraphProxy(FakeGraph(rows=4))
        with tenancy.org_context(tenancy.legacy_org()), query_metrics.tool_scope("who_owns"):
            graph.ro_query("MATCH (n) RETURN n")
            graph.query("MATCH (n) RETURN n")
        snap = query_metrics.snapshot()
        self.assertEqual(snap["total"]["count"], 2)
        self.assertEqual(snap["tools"]["who_owns"]["rows"], 8)
        self.assertEqual(snap["orgs"][tenancy.legacy_org().id]["count"], 2)

    def test_install_registers_the_hook_once(self):
        query_metrics.install()
        query_metrics.install()
        self.assertEqual(falkordb_client.AFTER_QUERY.count(query_metrics.record), 1)

    def test_timeouts_refusals_and_errors_are_told_apart_and_still_raise(self):
        failures = [
            ResponseError("Query timed out"),
            RedisTimeoutError("Timeout reading from socket"),
            ResponseError("Max pending queries exceeded"),
            RedisConnectionError("refused"),
        ]
        with query_metrics.tool_scope("blast_radius"), self.assertLogs(query_metrics.log, logging.WARNING) as logs:
            for failure in failures:
                with self.assertRaises(type(failure)):
                    _GraphProxy(FakeGraph(fail=failure)).ro_query("MATCH (n) RETURN n")
        stats = query_metrics.snapshot()["tools"]["blast_radius"]
        self.assertEqual((stats["count"], stats["timeouts"], stats["rejected"], stats["errors"]), (4, 2, 1, 1))
        self.assertEqual(stats["rows"], 0)
        # Timeouts and refusals are logged even when fast; a plain error is the caller's to log.
        self.assertEqual(len(logs.records), 3)
        self.assertIn("rejected query", logs.output[2])

    def test_with_no_org_in_context_the_org_is_a_dash(self):
        self.assertIsNone(tenancy.current_org())
        query_metrics.record(call(5))
        self.assertIn(query_metrics.NO_ORG, query_metrics.snapshot()["orgs"])


class CallerTest(unittest.TestCase):
    def test_the_tool_scope_wins_and_nests(self):
        with query_metrics.tool_scope("outer"):
            with query_metrics.tool_scope("inner"):
                self.assertEqual(query_metrics.caller(), "inner")
            self.assertEqual(query_metrics.caller(), "outer")

    def test_a_function_named_like_a_catalogue_tool_names_the_tool(self):
        # Frames outside src/ are not callers, so run the walk from a function compiled as if in src/.
        code = compile("def _run_who_owns():\n    return caller()\n", "/app/src/domain/knowledge/catalogue_fake.py", "exec")
        namespace = {"caller": query_metrics.caller, "__name__": "src.domain.knowledge.catalogue_fake"}
        exec(code, namespace)
        self.assertEqual(namespace["_run_who_owns"](), "who_owns")

    def test_otherwise_the_nearest_src_function_names_it(self):
        code = compile("def sync_batch():\n    return caller()\n", "/app/src/connectors/sync_fake.py", "exec")
        namespace = {"caller": query_metrics.caller, "__name__": "src.connectors.sync_fake"}
        exec(code, namespace)
        self.assertEqual(namespace["sync_batch"](), "connectors.sync_fake:sync_batch")


class SlowLogTest(MetricsTestCase):
    def test_a_slow_query_is_logged_without_parameters_or_string_literals(self):
        query = "MATCH (p:Person)\n   WHERE p.name = 'Priya Rao' OR p.id = $name RETURN p"
        with self.assertLogs(query_metrics.log, logging.WARNING) as logs:
            query_metrics.record(call(250.0, query))
        line = logs.output[0]
        self.assertIn("slow query 250 ms", line)
        self.assertIn("WHERE p.name = ? OR p.id = $name RETURN p", line)
        self.assertNotIn("Priya", line)
        self.assertNotIn("secret user text", line)
        self.assertEqual(query_metrics.snapshot()["total"]["slow"], 1)

    def test_a_fast_query_is_not_logged(self):
        with mock.patch.object(query_metrics.log, "warning") as warning:
            query_metrics.record(call(199.0))
        warning.assert_not_called()

    def test_the_shape_is_cut_and_handles_escaped_quotes(self):
        self.assertEqual(query_metrics.shape("RETURN 'it\\'s', \"a \\\"b\\\"\""), "RETURN ?, ?")
        self.assertEqual(len(query_metrics.shape("RETURN " + "x" * 500)), 200)


class BoundsTest(MetricsTestCase):
    def test_percentiles_over_the_window(self):
        for duration in range(1, 101):
            query_metrics.record(call(float(duration)))
        total = query_metrics.snapshot()["total"]
        self.assertEqual((total["p50_ms"], total["p95_ms"], total["max_ms"]), (50.0, 95.0, 100.0))

    def test_the_window_keeps_the_latest_durations(self):
        with mock.patch.object(query_metrics, "WINDOW", 10):
            query_metrics.reset()
            for duration in range(1, 101):
                query_metrics.record(call(float(duration)))
        total = query_metrics.snapshot()["total"]
        self.assertEqual((total["count"], total["window"], total["p50_ms"]), (100, 10, 95.0))

    def test_keys_beyond_the_limit_are_counted_under_other(self):
        with mock.patch.object(query_metrics, "MAX_KEYS", 3):
            for n in range(6):
                with query_metrics.tool_scope(f"tool_{n}"):
                    query_metrics.record(call(1.0))
        tools = query_metrics.snapshot()["tools"]
        self.assertEqual(sorted(tools), ["_other", "tool_0", "tool_1"])
        self.assertEqual(tools["_other"]["count"], 4)

    def test_an_empty_registry_has_no_percentiles(self):
        self.assertIsNone(query_metrics.snapshot()["total"]["p95_ms"])


class RouteTest(MetricsTestCase):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(ops.router)
        self.client = TestClient(app)

    def test_metrics_need_the_admin_token(self):
        unset = dataclasses.replace(settings, brain_admin_token="")
        with mock.patch.object(slack_install, "settings", unset):
            self.assertEqual(self.client.get("/ops/metrics").status_code, 503)
        armed = dataclasses.replace(settings, brain_admin_token=TOKEN)
        with mock.patch.object(slack_install, "settings", armed):
            self.assertEqual(self.client.get("/ops/metrics").status_code, 403)
            self.assertEqual(self.client.get("/ops/metrics", headers={"Authorization": "Bearer nope"}).status_code, 403)
            with query_metrics.tool_scope("who_knows"):
                query_metrics.record(call(12.0, rows=2))
            response = self.client.get("/ops/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["tools"]["who_knows"]["count"], 1)
        self.assertEqual(body["tools"]["who_knows"]["p95_ms"], 12.0)
        self.assertEqual(body["slow_ms"], query_metrics.SLOW_MS)
        self.assertNotIn("MATCH", response.text, "no query text in the metrics")


if __name__ == "__main__":
    unittest.main()
