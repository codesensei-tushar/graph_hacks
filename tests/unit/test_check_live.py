"""The live smoke check's pass/fail decision, on canned responses: no network, no server."""

import http.client
import io
import json
import re
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from scripts import check_live  # noqa: E402

CHIPS = ["who owns billing", "what breaks if auth goes down", "who knows the payments gateway"]
# The last chip and the last README question both name it.
REFUSED = "payments gateway"
# The page's catalogue: what /mcp must list, and what it must never list.
READ_TOOLS = ["who_owns", "who_knows", "blast_radius"]
WRITES = ["ingest_ticket", "record_decision"]


def _fake(overrides: dict[str, tuple[int, dict | None]] | None = None):
    """A server where every chip and README question answers as expected, unless overridden by path."""
    overrides = overrides or {}
    asked: list[str] = []

    def fetch(path: str):
        asked.append(path)
        if path in overrides:
            return overrides[path]
        if path == "/api/graph/stats":
            return 200, {"company": "quillfern", "nodes": 30, "edges": 70, "labels": {"Service": 9, "Person": 6}}
        if path == "/api/ask/tools":
            return 200, {"company": "quillfern", "examples": CHIPS, "tools": [{"name": name} for name in READ_TOOLS], "writes": WRITES}
        if path.startswith("/api/ask?"):
            question = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["q"][0]
            return 200, {"tool": "who_knows", "found": REFUSED not in question, "answer": f"answer to {question}"}
        return 200, None

    return fetch, asked


def _fake_mcp(overrides: dict[str, tuple[int, dict | None]] | None = None):
    """``/mcp`` with no token: the handshake, the read catalogue, and a cited answer, unless overridden by method."""
    overrides = overrides or {}
    posted: list[tuple[str, str]] = []
    results = {
        "initialize": {"protocolVersion": check_live.MCP_PROTOCOL, "serverInfo": {"name": "company-brain"}},
        "tools/list": {"tools": [{"name": name} for name in READ_TOOLS]},
        "tools/call": {"isError": False, "structuredContent": {"found": True, "answer": "Billing Service is owned by Arjun Mehta.", "path": [{}, {}]}},
    }

    def post(path: str, request: dict):
        method = request["method"]
        posted.append((path, method))
        if method in overrides:
            return overrides[method]
        return 200, {"jsonrpc": "2.0", "id": request["id"], "result": results[method]}

    return post, posted


class _Response:
    """What urlopen hands back: canned JSON, or a read that raises ``error`` as a slow server's does."""

    def __init__(self, payload: dict | None, error: Exception | None = None):
        self.status, self.payload, self.error = 200, payload, error

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self) -> bytes:
        if self.error:
            raise self.error
        return json.dumps(self.payload).encode()


def _urlopen(failures: dict[str, Exception]):
    """``urlopen`` over ``_fake()``'s server, where a path in ``failures`` raises its error mid-read."""
    fetch, _ = _fake()

    def urlopen(request, timeout):
        path = request.full_url.removeprefix("http://check.test")
        return _Response(fetch(path)[1], failures.get(path))

    return urlopen


class CheckLiveTest(unittest.TestCase):
    def test_every_chip_but_the_last_answers_and_the_readme_questions_and_pages_are_checked(self):
        fetch, asked = _fake()
        header, checks = check_live.run(fetch)
        self.assertEqual(header, "company=quillfern nodes=30 edges=70 Person=6 Service=9")
        self.assertTrue(all(check.ok for check in checks), [check.line() for check in checks if not check.ok])
        self.assertEqual(len(checks), len(CHIPS) + len(check_live.README_QUESTIONS) + len(check_live.README_PAGES))
        self.assertIn("/api/team/payments", asked)
        self.assertTrue(all(path.startswith("/") for path in asked))

    def test_a_chip_refused_or_a_refusal_answered_fails_with_why(self):
        refused = check_live.judge_answer("chip: x", True, 200, {"tool": "decision_pressure", "found": False, "answer": "Nothing in the graph matches “x”."})
        self.assertFalse(refused.ok)
        self.assertEqual(refused.line(), "FAIL  chip: x  ->  expected an answer; decision_pressure: Nothing in the graph matches “x”.")
        answered = check_live.judge_answer("chip: y", False, 200, {"tool": "who_knows", "found": True, "answer": "Billing Service " * 20})
        self.assertFalse(answered.ok)
        self.assertTrue(answered.detail.startswith("expected a refusal; who_knows: Billing Service"))
        self.assertLessEqual(len(answered.detail), len("expected a refusal; who_knows: ") + check_live.ANSWER_CHARS)

    def test_an_error_status_fails_and_says_so(self):
        throttled = check_live.judge_answer("readme: z", True, 429, {"error": "too many questions"})
        self.assertEqual((throttled.ok, throttled.detail), (False, "HTTP 429 too many questions"))
        fetch, _ = _fake({"/api/team/payments": (404, {"error": "no team"})})
        _, checks = check_live.run(fetch)
        self.assertEqual([check.label for check in checks if not check.ok], ["page: /api/team/payments"])

    def test_an_unreachable_server_fails_at_the_first_request(self):
        fetch, asked = _fake({"/api/graph/stats": (0, {"error": "connection refused"})})
        header, checks = check_live.run(fetch)
        self.assertEqual(asked, ["/api/graph/stats"])
        self.assertEqual([check.ok for check in checks], [False])
        self.assertIn("HTTP 0", header)

    def test_a_slow_or_dropped_read_fails_its_line_and_the_rest_still_run(self):
        slow = "/api/ask?" + urllib.parse.urlencode({"q": "what if arjun mehta leaves"})
        failures = {slow: TimeoutError("The read operation timed out"), "/api/team/payments": ConnectionResetError()}
        with mock.patch.object(check_live.urllib.request, "urlopen", _urlopen(failures)):
            _, checks = check_live.run(check_live.http_fetch("http://check.test"))
        self.assertEqual(len(checks), len(CHIPS) + len(check_live.README_QUESTIONS) + len(check_live.README_PAGES))
        self.assertEqual(
            [check.line() for check in checks if not check.ok],
            ["FAIL  readme: what if arjun mehta leaves  ->  HTTP 0 timeout", "FAIL  page: /api/team/payments  ->  HTTP 0"],
        )
        with mock.patch.object(check_live.urllib.request, "urlopen", _urlopen({"/api/team/payments": ConnectionResetError()})):
            self.assertEqual(check_live.http_fetch("http://check.test")("/api/team/payments"), (0, {"error": "ConnectionResetError"}))

    def test_a_body_cut_short_is_a_failure_not_a_traceback(self):
        with mock.patch.object(check_live.urllib.request, "urlopen", _urlopen({"/api/graph/stats": http.client.IncompleteRead(b"{", 40)})):
            header, checks = check_live.run(check_live.http_fetch("http://check.test"))
        self.assertEqual(header, "/api/graph/stats answered HTTP 0")
        self.assertEqual([check.ok for check in checks], [False])

    def test_no_chips_listed_is_a_failure(self):
        fetch, _ = _fake({"/api/ask/tools": (200, {"examples": []})})
        _, checks = check_live.run(fetch)
        self.assertEqual(checks[0].label, "/api/ask/tools")
        self.assertFalse(checks[0].ok)

    def test_mcp_with_no_token_lists_the_read_catalogue_and_answers_with_a_path(self):
        fetch, _ = _fake()
        post, posted = _fake_mcp()
        _, checks = check_live.run(fetch, post)
        self.assertTrue(all(check.ok for check in checks), [check.line() for check in checks if not check.ok])
        self.assertEqual(len(checks), len(CHIPS) + len(check_live.README_QUESTIONS) + len(check_live.README_PAGES) + 3)
        self.assertEqual(posted, [("/mcp", "initialize"), ("/mcp", "tools/list"), ("/mcp", "tools/call")])
        self.assertEqual(
            [check.line() for check in checks[-3:]],
            [
                f"PASS  mcp: initialize  ->  company-brain {check_live.MCP_PROTOCOL}",
                "PASS  mcp: tools/list  ->  3 read tools, none writes",
                "PASS  mcp: tools/call who_knows billing  ->  2 path steps: Billing Service is owned by Arjun Mehta.",
            ],
        )

    def test_a_writing_tool_or_a_missing_one_over_mcp_fails_and_names_it(self):
        listed = {"tools": [{"name": name} for name in [*READ_TOOLS[:-1], "ingest_ticket"]]}
        post, _ = _fake_mcp({"tools/list": (200, {"jsonrpc": "2.0", "id": 2, "result": listed})})
        checks = check_live.mcp_checks(post, READ_TOOLS, WRITES)
        self.assertEqual(
            [check.line() for check in checks if not check.ok],
            ["FAIL  mcp: tools/list  ->  3 read tools; writes exposed: ingest_ticket; missing: blast_radius; not on the page: ingest_ticket"],
        )

    def test_an_mcp_call_without_a_cited_answer_fails(self):
        refused = {"isError": False, "structuredContent": {"found": False, "answer": "Nothing in the graph matches “billing”.", "path": []}}
        post, _ = _fake_mcp({"tools/call": (200, {"jsonrpc": "2.0", "id": 3, "result": refused})})
        failed = [check for check in check_live.mcp_checks(post, READ_TOOLS, WRITES) if not check.ok]
        self.assertEqual([check.label for check in failed], ["mcp: tools/call who_knows billing"])
        self.assertTrue(failed[0].detail.startswith("expected an answer with a path; isError=False found=False steps=0"), failed[0].detail)

    def test_a_json_rpc_error_fails_with_its_message(self):
        post, _ = _fake_mcp({"tools/call": (200, {"jsonrpc": "2.0", "id": 3, "error": {"code": -32602, "message": "Unknown tool"}})})
        failed = [check.line() for check in check_live.mcp_checks(post, READ_TOOLS, WRITES) if not check.ok]
        self.assertEqual(failed, ["FAIL  mcp: tools/call who_knows billing  ->  JSON-RPC error -32602: Unknown tool"])

    def test_a_failed_handshake_stops_the_mcp_checks_there(self):
        post, posted = _fake_mcp({"initialize": (429, {"error": "too many calls from this address; try again in 12 seconds"})})
        checks = check_live.mcp_checks(post, READ_TOOLS, WRITES)
        self.assertEqual([check.line() for check in checks], ["FAIL  mcp: initialize  ->  HTTP 429 too many calls from this address; try again in 12 seconds"])
        self.assertEqual(posted, [("/mcp", "initialize")])

    def test_http_post_sends_json_rpc_like_an_mcp_client_and_reads_an_sse_reply(self):
        sent = []
        reply = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": check_live.MCP_PROTOCOL}}

        class _Sse(_Response):
            headers = {"content-type": "text/event-stream"}

            def read(self) -> bytes:
                return f"event: message\ndata: {json.dumps(reply)}\n\n".encode()

        def urlopen(request, timeout):
            sent.append(request)
            return _Sse(None)

        with mock.patch.object(check_live.urllib.request, "urlopen", urlopen):
            status, parsed = check_live.http_post("http://check.test")("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual((status, parsed), (200, reply))
        request = sent[0]
        self.assertEqual((request.get_method(), request.full_url), ("POST", "http://check.test/mcp"))
        self.assertEqual(request.get_header("Accept"), "application/json, text/event-stream")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(json.loads(request.data)["method"], "initialize")

    def test_no_mcp_sends_get_requests_only(self):
        with mock.patch.object(check_live, "run", return_value=("header", [])) as run, mock.patch("sys.argv", ["check_live.py", "--no-mcp"]):
            self.assertEqual(check_live.main(), 0)
        self.assertIsNone(run.call_args.args[1])
        with mock.patch.object(check_live, "run", return_value=("header", [])) as run, mock.patch("sys.argv", ["check_live.py"]):
            check_live.main()
        self.assertTrue(callable(run.call_args.args[1]))

    def test_the_readme_questions_are_the_ones_in_try_it_in_two_minutes(self):
        readme = (Path(__file__).resolve().parents[2] / "README.md").read_text()
        section = readme.split("## Try it in two minutes", 1)[1].split("\n## ", 1)[0]
        self.assertEqual(re.findall(r"^\| `([^`]+)` \|", section, re.MULTILINE), [question for question, _ in check_live.README_QUESTIONS])
        for path in ("/team/payments", "/?q=what%20if%20arjun%20mehta%20leaves"):
            self.assertIn(path, section)


class _Clock:
    """A clock that only moves when the pacer sleeps."""

    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float):
        self.slept.append(round(seconds, 3))
        self.now += seconds

    def __call__(self) -> float:
        return self.now


class PacerTest(unittest.TestCase):
    def test_a_429_waits_its_retry_after_once_then_answers(self):
        clock = _Clock()
        replies = [(429, {"error": "Too many questions", "retry_after": "2"}), (200, {"found": True, "answer": "ok"})]
        asked: list[str] = []

        def fetch(path):
            asked.append(path)
            return replies.pop(0)

        fetch = check_live.Pacer(sleep=clock.sleep, clock=clock).wrap(fetch)
        self.assertEqual(fetch("/api/ask?q=x"), (200, {"found": True, "answer": "ok"}))
        self.assertEqual(asked, ["/api/ask?q=x", "/api/ask?q=x"])
        # Retry-After, then the pace already covered by that wait.
        self.assertEqual(clock.slept, [2.0])

    def test_a_long_retry_after_is_capped_and_a_second_429_is_reported(self):
        clock = _Clock()
        fetch = check_live.Pacer(sleep=clock.sleep, clock=clock).wrap(lambda path: (429, {"retry_after": "60"}))
        status, body = fetch("/api/ask?q=x")
        self.assertEqual((status, clock.slept), (429, [check_live.MAX_RETRY_SECONDS]))
        self.assertEqual(check_live.judge_answer("chip: x", True, status, body).line(), "FAIL  chip: x  ->  HTTP 429")
        self.assertEqual([check_live.retry_wait(body) for body in ({}, None, {"retry_after": "soon"}, {"retry_after": "-4"})], [1.0, 1.0, 1.0, 0.0])

    def test_requests_are_paced_across_fetch_and_post(self):
        clock = _Clock()
        pacer = check_live.Pacer(sleep=clock.sleep, clock=clock)
        fetch, _ = _fake()
        post, posted = _fake_mcp()
        _, checks = check_live.run(pacer.wrap(fetch), pacer.wrap(post))
        self.assertTrue(all(check.ok for check in checks))
        requests = 2 + len(CHIPS) + len(check_live.README_QUESTIONS) + len(check_live.README_PAGES) + len(posted)
        # Every request after the first waits one pace, the budget's refill.
        self.assertEqual(clock.slept, [check_live.PACE_SECONDS] * (requests - 1))

    def test_http_fetch_reads_a_429s_retry_after(self):
        error = urllib.error.HTTPError("http://check.test/api/ask?q=x", 429, "Too Many Requests", {"Retry-After": "3"}, io.BytesIO(b'{"error": "Too many questions"}'))
        with mock.patch.object(check_live.urllib.request, "urlopen", side_effect=error):
            self.assertEqual(check_live.http_fetch("http://check.test")("/api/ask?q=x"), (429, {"error": "Too many questions", "retry_after": "3"}))

    def test_main_paces_what_it_sends(self):
        with mock.patch.object(check_live, "run", return_value=("header", [])) as run, mock.patch("sys.argv", ["check_live.py"]):
            check_live.main()
        fetch, post = run.call_args.args
        self.assertEqual((fetch.__qualname__, post.__qualname__), ("Pacer.wrap.<locals>.paced", "Pacer.wrap.<locals>.paced"))
