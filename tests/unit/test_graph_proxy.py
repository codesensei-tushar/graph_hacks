"""The one wrapper every query passes through: the context's timeout and the before/after hooks. No database."""

import dataclasses
import unittest

import tests  # noqa: F401  pins the test settings before src loads

from src.db import falkordb_client  # noqa: E402
from src.db.falkordb_client import _GraphProxy, query_timeout  # noqa: E402
from src.domain.knowledge import mcp_tools, tool_guard  # noqa: E402


class FakeGraph:
    name = "fake_graph"

    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self.fail = fail

    def query(self, q, params=None, timeout=None):
        return self._record("query", q, params, timeout)

    def ro_query(self, q, params=None, timeout=None):
        return self._record("ro_query", q, params, timeout)

    def _record(self, method, q, params, timeout):
        self.calls.append((method, q, params, timeout))
        if self.fail:
            raise self.fail
        return f"rows of {q}"

    def delete(self):
        return "deleted"


class TimeoutTest(unittest.TestCase):
    def test_a_query_takes_the_contexts_timeout_unless_it_names_its_own(self):
        fake = FakeGraph()
        graph = _GraphProxy(fake)
        graph.ro_query("A")
        with query_timeout(2000):
            graph.ro_query("B", {"x": 1})
            graph.query("C")
            graph.ro_query("D", timeout=50)
        graph.query("E")
        self.assertEqual(
            [(method, q, timeout) for method, q, _params, timeout in fake.calls],
            [("ro_query", "A", None), ("ro_query", "B", 2000), ("query", "C", 2000), ("ro_query", "D", 50), ("query", "E", None)],
        )
        self.assertEqual(fake.calls[1][2], {"x": 1})

    def test_nested_blocks_keep_the_shorter_timeout_and_none_changes_nothing(self):
        with query_timeout(2000):
            with query_timeout(5000):
                self.assertEqual(falkordb_client.query_timeout_ms.get(), 2000)
            with query_timeout(500):
                self.assertEqual(falkordb_client.query_timeout_ms.get(), 500)
            with query_timeout(None):
                self.assertEqual(falkordb_client.query_timeout_ms.get(), 2000)
        self.assertIsNone(falkordb_client.query_timeout_ms.get())

    def test_everything_else_is_the_graphs_own(self):
        graph = _GraphProxy(FakeGraph())
        self.assertEqual((graph.name, graph.delete()), ("fake_graph", "deleted"))

    def test_the_public_guard_sets_the_short_timeout_and_a_plain_guard_does_not(self):
        with tool_guard.guard("ask", "q", timeout_ms=tool_guard.PUBLIC_QUERY_TIMEOUT_MS):
            self.assertEqual(falkordb_client.query_timeout_ms.get(), 2000)
        with tool_guard.guard("slack", "q"):
            self.assertIsNone(falkordb_client.query_timeout_ms.get())

    def test_mcp_calls_run_under_the_public_timeout(self):
        seen = []
        original = mcp_tools.catalogue.get_tool("who_owns")

        def run(args, scopes):
            seen.append(falkordb_client.query_timeout_ms.get())
            raise RuntimeError("stop here")

        mcp_tools.catalogue.register(dataclasses.replace(original, run=run))
        try:
            with self.assertRaises(RuntimeError):
                mcp_tools.call_tool("who_owns", {"name": "billing"})
        finally:
            mcp_tools.catalogue.register(original)
        self.assertEqual(seen, [tool_guard.PUBLIC_QUERY_TIMEOUT_MS])


class HookTest(unittest.TestCase):
    def setUp(self):
        self.seen: list[tuple] = []
        self.before = lambda call: self.seen.append(("before", call.method, call.query, call.timeout, call.duration_ms))
        self.after = lambda call: self.seen.append(("after", call.method, call.graph, call.result, type(call.error).__name__ if call.error else None))
        falkordb_client.add_query_hook(before=self.before, after=self.after)
        self.addCleanup(falkordb_client.remove_query_hook, self.before)
        self.addCleanup(falkordb_client.remove_query_hook, self.after)

    def test_hooks_see_each_query_before_and_after_with_its_result_or_error(self):
        _GraphProxy(FakeGraph()).ro_query("A")
        with query_timeout(2000):
            with self.assertRaises(ValueError):
                _GraphProxy(FakeGraph(fail=ValueError("boom"))).query("B")
        self.assertEqual(
            self.seen,
            [
                ("before", "ro_query", "A", None, None),
                ("after", "ro_query", "fake_graph", "rows of A", None),
                ("before", "query", "B", 2000, None),
                ("after", "query", "fake_graph", None, "ValueError"),
            ],
        )

    def test_after_hooks_get_the_duration(self):
        durations = []
        falkordb_client.add_query_hook(after=lambda call: durations.append(call.duration_ms))
        self.addCleanup(falkordb_client.AFTER_QUERY.pop)
        _GraphProxy(FakeGraph()).ro_query("A")
        self.assertEqual(len(durations), 1)
        self.assertGreaterEqual(durations[0], 0)

    def test_a_hook_that_raises_is_logged_and_never_fails_the_query(self):
        def broken(call):
            raise RuntimeError("metrics are down")

        falkordb_client.add_query_hook(before=broken, after=broken)
        self.addCleanup(falkordb_client.remove_query_hook, broken)
        with self.assertLogs("src.db.falkordb_client", "ERROR") as logged:
            self.assertEqual(_GraphProxy(FakeGraph()).ro_query("A"), "rows of A")
        self.assertEqual(len(logged.output), 2)
        self.assertEqual([entry[0] for entry in self.seen], ["before", "after"])


if __name__ == "__main__":
    unittest.main()
