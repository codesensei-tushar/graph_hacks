"""The public surface's limits with a fake clock: the TTL cache, the question cap, the per-client rate limit. No database."""

import json
import unittest
from unittest import mock

import tests  # noqa: F401  pins the test settings before src loads

from src.domain.knowledge import agent, ask_api, catalogue
from src.domain.knowledge.dto import ToolResult, ToolSpec


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TtlCacheTest(unittest.TestCase):
    def test_a_value_is_reused_until_it_expires(self):
        clock = Clock()
        cache = ask_api.TtlCache(ttl=60, max_entries=8, clock=clock)
        calls = []

        def compute():
            calls.append(clock.now)
            return len(calls)

        self.assertEqual(cache.get_or_compute("q", compute), 1)
        clock.now += 59
        self.assertEqual(cache.get_or_compute("q", compute), 1)
        clock.now += 2
        self.assertEqual(cache.get_or_compute("q", compute), 2)
        self.assertEqual(len(calls), 2)

    def test_the_oldest_entry_goes_first_and_failures_are_not_kept(self):
        cache = ask_api.TtlCache(ttl=60, max_entries=2, clock=Clock())
        for key in ("a", "b", "c"):
            cache.get_or_compute(key, lambda key=key: key.upper())
        self.assertEqual(cache.get_or_compute("a", lambda: "recomputed"), "recomputed")
        self.assertEqual(cache.get_or_compute("c", lambda: "recomputed"), "C")

        def fail():
            raise ask_api.ToolRefused("no")

        with self.assertRaises(ask_api.ToolRefused):
            cache.get_or_compute("bad", fail)
        self.assertEqual(cache.get_or_compute("bad", lambda: "fine"), "fine")

    def test_identical_concurrent_misses_compute_once(self):
        import threading

        cache = ask_api.TtlCache(ttl=60, max_entries=8, clock=Clock())
        started, release = threading.Event(), threading.Event()
        calls = []

        def compute():
            calls.append(1)
            started.set()
            release.wait(5)
            return "answer"

        results = []
        threads = [threading.Thread(target=lambda: results.append(cache.get_or_compute("q", compute))) for _ in range(5)]
        threads[0].start()
        started.wait(5)
        for thread in threads[1:]:
            thread.start()
        release.set()
        for thread in threads:
            thread.join(5)
        self.assertEqual((len(calls), results), (1, ["answer"] * 5))

    def test_a_failure_reaches_every_waiter_and_is_not_kept(self):
        import threading

        cache = ask_api.TtlCache(ttl=60, max_entries=8, clock=Clock())
        started, release = threading.Event(), threading.Event()

        def fail():
            started.set()
            release.wait(5)
            raise ask_api.ToolRefused("no")

        errors = []

        def ask():
            try:
                cache.get_or_compute("q", fail)
            except ask_api.ToolRefused as error:
                errors.append(error)

        first = threading.Thread(target=ask)
        first.start()
        started.wait(5)
        second = threading.Thread(target=ask)
        second.start()
        release.set()
        first.join(5)
        second.join(5)
        self.assertEqual(len(errors), 2)
        self.assertEqual(cache.get_or_compute("q", lambda: "fine"), "fine")


class RateLimiterTest(unittest.TestCase):
    def test_each_client_gets_its_burst_then_the_steady_rate_and_a_retry_time(self):
        clock = Clock()
        limiter = ask_api.RateLimiter(limit=60, window=60, clock=clock, burst=30)
        self.assertTrue(all(limiter.check("203.0.113.7").allowed for _ in range(30)))
        refused = limiter.check("203.0.113.7")
        self.assertEqual((refused.allowed, refused.retry_after), (False, 1))
        self.assertTrue(limiter.check("198.51.100.9").allowed)
        # One call a second is the steady rate: it never runs out.
        for _ in range(120):
            clock.now += 1
            self.assertTrue(limiter.check("203.0.113.7").allowed)
        # Idle long enough, the whole burst is back, and no more than that.
        clock.now += 3600
        self.assertTrue(all(limiter.check("203.0.113.7").allowed for _ in range(30)))
        self.assertFalse(limiter.check("203.0.113.7").allowed)

    def test_without_a_burst_the_limit_is_the_burst_and_the_retry_is_the_refill_time(self):
        clock = Clock()
        limiter = ask_api.RateLimiter(limit=3, window=60, clock=clock)
        self.assertTrue(all(limiter.check("a").allowed for _ in range(3)))
        self.assertEqual(limiter.check("a").retry_after, 20)
        clock.now += 20
        self.assertTrue(limiter.check("a").allowed)

    def test_the_default_budget_covers_ten_rapid_page_views(self):
        # The home page with ?q= makes one counted call; the rest are cached reads.
        limiter = ask_api.RateLimiter(ask_api.RATE_LIMIT, ask_api.RATE_WINDOW_SECONDS, clock=Clock(), burst=ask_api.RATE_BURST)
        self.assertTrue(all(limiter.check("office-nat").allowed for _ in range(10)))
        self.assertGreaterEqual(ask_api.RATE_LIMIT, 60)

    def test_a_heavy_call_spends_its_cost_and_waits_for_enough_tokens(self):
        clock = Clock()
        limiter = ask_api.RateLimiter(limit=60, window=60, clock=clock, burst=12)
        self.assertTrue(limiter.check("a", 5).allowed)
        self.assertTrue(limiter.check("a", 5).allowed)
        refused = limiter.check("a", 5)
        self.assertEqual((refused.allowed, refused.retry_after), (False, 3))
        # Two tokens left: a light call still passes.
        self.assertTrue(limiter.check("a").allowed)
        clock.now += 4
        self.assertTrue(limiter.check("a", 5).allowed)
        # A cost over the burst is charged the whole burst, never refused forever.
        clock.now += 3600
        self.assertTrue(limiter.check("b", 1000).allowed)
        self.assertFalse(limiter.check("b").allowed)

    def test_heavy_tools_and_deep_explores_cost_more_and_questions_are_priced_by_their_route(self):
        heavy = ask_api.HEAVY_COST
        self.assertGreater(heavy, 1)
        for tool in ("path_between", "risk_map", "onboarding_brief"):
            self.assertEqual(ask_api.tool_cost(tool), heavy, tool)
        self.assertEqual(ask_api.tool_cost("who_owns"), 1)
        self.assertEqual(ask_api.tool_cost("explore"), 1)
        self.assertEqual(ask_api.tool_cost("explore", {"depth": 2}), heavy)
        self.assertEqual(ask_api.tool_cost("explore", {"depth": "lots"}), 1)
        self.assertEqual(ask_api.tool_cost(None), 1)
        self.assertEqual(ask_api.question_cost("how is maya connected to the invoice worker"), heavy)
        self.assertEqual(ask_api.question_cost("what are our riskiest services"), heavy)
        self.assertEqual(ask_api.question_cost("who owns billing"), 1)
        self.assertEqual(ask_api.question_cost("who owns billing", tool="risk_map"), heavy)
        self.assertEqual(ask_api.question_cost(""), 1)
        self.assertEqual(ask_api.question_cost("x" * (ask_api.MAX_QUESTION_CHARS + 1)), 1)

    def test_stale_clients_are_dropped_when_the_table_is_full(self):
        clock = Clock()
        limiter = ask_api.RateLimiter(limit=1, window=60, clock=clock, max_clients=2)
        limiter.check("a")
        limiter.check("b")
        clock.now += 61
        limiter.check("c")
        self.assertEqual(set(limiter._buckets), {"c"})

    def test_admit_counts_clients_without_an_address_together(self):
        with mock.patch.object(ask_api, "LIMITER", ask_api.RateLimiter(limit=1, window=60)):
            self.assertTrue(ask_api.admit(None).allowed)
            self.assertFalse(ask_api.admit("").allowed)
            self.assertTrue(ask_api.admit("203.0.113.7").allowed)


COUNTER = {"runs": 0}


def _counted(args: dict, scopes: list[str]) -> ToolResult:
    COUNTER["runs"] += 1
    return ToolResult(tool="probe_count", answer=f"run {COUNTER['runs']}")


COUNT_SPEC = ToolSpec(
    name="probe_count",
    description="Counts its runs.",
    args_schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    run=_counted,
)


class AnswerLimitsTest(unittest.TestCase):
    def setUp(self):
        ask_api.ANSWERS.clear()
        COUNTER["runs"] = 0
        catalogue.register(COUNT_SPEC)
        self.keywords = mock.patch.object(agent, "choose_with_keywords", return_value=agent.ToolChoice("probe_count", {"name": "billing"}, "keywords"))
        self.keywords.start()
        self.graph = mock.patch.object(ask_api.falkordb_client, "graph_name", return_value="company_limits_probe")
        self.graph.start()

    def tearDown(self):
        self.graph.stop()
        self.keywords.stop()
        catalogue._TOOLS.pop("probe_count", None)
        ask_api.ANSWERS.clear()

    def test_the_cap_counts_the_question_after_whitespace_is_collapsed(self):
        at_cap = "b" * ask_api.MAX_QUESTION_CHARS
        self.assertEqual(ask_api.answer_question(at_cap + "   ").tool, "probe_count")
        with self.assertRaisesRegex(ask_api.QuestionTooLong, "limited to 300 characters; this one has 301"):
            ask_api.answer_question(at_cap + "x")
        self.assertEqual(COUNTER["runs"], 1)

    def test_the_same_question_runs_once_and_a_different_one_or_tool_runs_again(self):
        self.assertEqual(ask_api.answer_question("who owns billing").answer, "run 1")
        self.assertEqual(ask_api.answer_question("  who owns   billing ").answer, "run 1")
        self.assertEqual(ask_api.answer_question("who owns invoices").answer, "run 2")
        self.assertEqual(ask_api.answer_question("who owns billing", tool="probe_count").answer, "run 3")
        with mock.patch.object(ask_api.falkordb_client, "graph_name", return_value="another_graph"):
            self.assertEqual(ask_api.answer_question("who owns billing").answer, "run 4")

    def test_model_picked_answers_are_never_cached(self):
        ask_api.answer_question("who owns billing", use_model=True)
        ask_api.answer_question("who owns billing", use_model=True)
        self.assertEqual(COUNTER["runs"], 2)

    def test_the_answer_expires_with_the_cache(self):
        clock = Clock()
        with mock.patch.object(ask_api, "ANSWERS", ask_api.TtlCache(ttl=60, max_entries=8, clock=clock)):
            ask_api.answer_question("who owns billing")
            clock.now += 61
            self.assertEqual(ask_api.answer_question("who owns billing").answer, "run 2")

    def test_the_public_proof_is_computed_once_per_period(self):
        report = object()
        with mock.patch.object(ask_api, "run_proof", return_value=report) as run:
            self.assertIs(ask_api.public_proof(), report)
            self.assertIs(ask_api.public_proof(), report)
        run.assert_called_once_with()


class McpCallCostTest(unittest.TestCase):
    def test_a_body_is_priced_by_the_tools_it_calls(self):
        from src.api import mcp as mcp_api

        def call(name, arguments=None):
            return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}

        heavy = ask_api.HEAVY_COST
        self.assertEqual(mcp_api.call_cost(json.dumps(call("path_between")).encode()), heavy)
        self.assertEqual(mcp_api.call_cost(json.dumps(call("explore", {"name": "billing", "depth": 2})).encode()), heavy)
        self.assertEqual(mcp_api.call_cost(json.dumps(call("who_owns")).encode()), 1)
        self.assertEqual(mcp_api.call_cost(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()), 1)
        self.assertEqual(mcp_api.call_cost(json.dumps([call("risk_map"), call("who_owns")]).encode()), heavy + 1)
        for junk in (b"", b"not json", b"[]", b'{"method": "tools/call", "params": "x"}'):
            self.assertEqual(mcp_api.call_cost(junk), 1, junk)
