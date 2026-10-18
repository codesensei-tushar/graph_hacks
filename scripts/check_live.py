"""Smoke-check a running instance before a recording, after a seed refresh, or after a deploy.

Read-only, with no token: it sends the same public GET requests the page sends, then three
JSON-RPC POSTs to ``/mcp`` that read and never write (``initialize``, ``tools/list`` and one
``tools/call``). Every example chip the server lists for its company must answer, except
the last, which must be refused; so must the questions README.md's "Try it in two minutes"
tells a judge to paste, and the team page and deep link it names must load. Over MCP, the
tool list must be the page's read catalogue with no writing tool, and ``who_knows`` on
billing must answer with its path. One PASS or FAIL line per check; the exit status is 1
when any check fails.

    python scripts/check_live.py                                # production
    python scripts/check_live.py --base http://localhost:8000
    python scripts/check_live.py --no-mcp                       # GET requests only

The ask routes and ``/mcp`` share 30 calls a minute per address and this makes about 20,
so wait a minute before running it twice.
"""

import argparse
import http.client
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

PRODUCTION = "https://brain.noobsurd.space"
TIMEOUT_SECONDS = 30
ANSWER_CHARS = 100

# README.md, "Try it in two minutes": what a judge pastes first, and whether each should answer.
# Keep this in step with that table.
README_QUESTIONS = (
    ("who owns billing, and who else has worked on it", True),
    ("what if arjun mehta leaves", True),
    ("why did we move the memory layer off postgres", True),
    ("who knows the payments gateway", False),
)
# The same section's team page and deep link. /team/<id> serves the page even for a team the
# graph does not have, so the JSON it fetches is checked too.
README_PAGES = ("/team/payments", "/api/team/payments", "/?q=what%20if%20arjun%20mehta%20leaves")

# MCP over streamable HTTP, as a judge's client reaches it: no token, plain JSON-RPC.
MCP_PATH = "/mcp"
MCP_PROTOCOL = "2025-06-18"
# One call that must answer with its path, the README's first question in MCP form.
MCP_CALL = ("who_knows", {"name": "billing"})

Fetch = Callable[[str], tuple[int, dict | None]]
# ``post(path, JSON-RPC request) -> (status, parsed JSON-RPC reply or None)``.
Post = Callable[[str, dict], tuple[int, dict | None]]


@dataclass(frozen=True)
class Check:
    ok: bool
    label: str
    detail: str

    def line(self) -> str:
        return f"{'PASS' if self.ok else 'FAIL'}  {self.label}  ->  {self.detail}"


def chip_expectations(examples: list[str]) -> list[tuple[str, bool]]:
    """Every chip answers except the last, which names something the graph does not have."""
    return [(question, index < len(examples) - 1) for index, question in enumerate(examples)]


def judge_answer(label: str, want_found: bool, status: int, answer: dict | None) -> Check:
    """Pass when the ask route answered and ``found`` is what the question expects."""
    if status != 200 or answer is None:
        return Check(False, label, f"HTTP {status} {(answer or {}).get('error', '')}".strip())
    text = " ".join(str(answer.get("answer", "")).split())
    found = answer.get("found") is True
    detail = f"{answer.get('tool')}: {text[:ANSWER_CHARS]}"
    if found != want_found:
        detail = f"expected {'an answer' if want_found else 'a refusal'}; {detail}"
    return Check(found == want_found, label, detail)


def rpc_result(status: int, reply: dict | None) -> tuple[dict | None, str]:
    """A JSON-RPC reply's ``result``, or None and why there is none."""
    if status != 200 or not isinstance(reply, dict):
        return None, f"HTTP {status} {(reply or {}).get('error', '')}".strip()
    if "error" in reply:
        error = reply["error"]
        return None, f"JSON-RPC error {error.get('code')}: {error.get('message')}" if isinstance(error, dict) else f"JSON-RPC error {error}"
    result = reply.get("result")
    return (result, "") if isinstance(result, dict) else (None, "no result in the reply")


def mcp_checks(post: Post, read_tools: list[str], write_tools: list[str]) -> list[Check]:
    """``/mcp`` with no token: the handshake, the tool list against the page's catalogue, one call."""

    def ask(request_id: int, method: str, params: dict) -> tuple[dict | None, str]:
        return rpc_result(*post(MCP_PATH, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}))

    hello = {"protocolVersion": MCP_PROTOCOL, "capabilities": {}, "clientInfo": {"name": "check_live", "version": "1"}}
    result, why = ask(1, "initialize", hello)
    if result is None:
        # Nothing else can work; don't spend the rest of the address's budget finding out.
        return [Check(False, "mcp: initialize", why)]
    server = result.get("serverInfo") or {}
    checks = [Check(True, "mcp: initialize", f"{server.get('name')} {result.get('protocolVersion')}")]

    result, why = ask(2, "tools/list", {})
    if result is None:
        checks.append(Check(False, "mcp: tools/list", why))
    else:
        names = [tool.get("name") for tool in result.get("tools") or []]
        missing = sorted(set(read_tools) - set(names))
        extra = sorted(set(names) - set(read_tools))
        writes = sorted(set(names) & set(write_tools))
        problems = [f"{label}: {', '.join(found)}" for label, found in (("writes exposed", writes), ("missing", missing), ("not on the page", extra)) if found]
        if not read_tools:
            problems.append("the page listed no read tools to compare with")
        checks.append(Check(not problems, "mcp: tools/list", f"{len(names)} read tools" + (f"; {'; '.join(problems)}" if problems else ", none writes")))

    tool, arguments = MCP_CALL
    label = f"mcp: tools/call {tool} {arguments['name']}"
    result, why = ask(3, "tools/call", {"name": tool, "arguments": arguments})
    if result is None:
        checks.append(Check(False, label, why))
    else:
        content = result.get("structuredContent") or {}
        steps = len(content.get("path") or [])
        text = " ".join(str(content.get("answer", "")).split())[:ANSWER_CHARS]
        ok = not result.get("isError") and content.get("found") is True and steps > 0
        detail = (
            f"{steps} path steps: {text}"
            if ok
            else f"expected an answer with a path; isError={result.get('isError')} found={content.get('found')} steps={steps} {text}".strip()
        )
        checks.append(Check(ok, label, detail))
    return checks


def run(fetch: Fetch, post: Post | None = None) -> tuple[str, list[Check]]:
    """The header line and every check, from ``fetch(path) -> (status, parsed JSON or None)``.

    With ``post``, the MCP checks follow the page's; without it every request is a GET."""
    status, stats = fetch("/api/graph/stats")
    if status != 200 or not stats:
        return f"/api/graph/stats answered HTTP {status}", [Check(False, "/api/graph/stats", f"HTTP {status}")]
    labels = " ".join(f"{label}={count}" for label, count in sorted(stats.get("labels", {}).items()))
    header = f"company={stats.get('company')} nodes={stats.get('nodes')} edges={stats.get('edges')} {labels}"

    checks: list[Check] = []
    status, listing = fetch("/api/ask/tools")
    examples = list((listing or {}).get("examples") or [])
    if status != 200 or not examples:
        checks.append(Check(False, "/api/ask/tools", f"HTTP {status}, {len(examples)} example chips"))
    questions = [("chip", *pair) for pair in chip_expectations(examples)] + [("readme", *pair) for pair in README_QUESTIONS]
    for source, question, want_found in questions:
        status, answer = fetch("/api/ask?" + urllib.parse.urlencode({"q": question}))
        checks.append(judge_answer(f"{source}: {question}", want_found, status, answer))
    for path in README_PAGES:
        status, _ = fetch(path)
        checks.append(Check(status == 200, f"page: {path}", f"HTTP {status}"))
    if post is not None:
        read_tools = [tool.get("name") for tool in (listing or {}).get("tools") or []]
        checks.extend(mcp_checks(post, read_tools, list((listing or {}).get("writes") or [])))
    return header, checks


def _exchange(request: urllib.request.Request) -> tuple[int, dict | None]:
    """Send one request and parse its JSON body, or an SSE body's last ``data:`` line.

    A transport failure is status 0 with the reason, never a traceback."""
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status, body = response.status, response.read()
            content_type = (getattr(response, "headers", None) or {}).get("content-type", "")
    except urllib.error.HTTPError as error:
        status, body, content_type = error.code, error.read(), ""
    except urllib.error.URLError as error:
        return 0, {"error": str(error.reason)}
    except TimeoutError:
        # A slow read raises the socket's own timeout, not a URLError.
        return 0, {"error": "timeout"}
    except OSError as error:
        return 0, {"error": str(error) or type(error).__name__}
    except http.client.HTTPException as error:
        # A body cut short (IncompleteRead) is not an OSError.
        return 0, {"error": str(error) or type(error).__name__}
    if content_type.startswith("text/event-stream"):
        # Streamable HTTP may answer as SSE: the reply is the last event's data.
        events = [line[5:].strip() for line in body.decode("utf-8", "replace").splitlines() if line.startswith("data:")]
        body = events[-1].encode() if events else b""
    try:
        return status, json.loads(body)
    except ValueError:
        return status, None


def http_fetch(base: str) -> Fetch:
    def fetch(path: str) -> tuple[int, dict | None]:
        return _exchange(urllib.request.Request(base.rstrip("/") + path, headers={"User-Agent": "check_live"}))

    return fetch


def http_post(base: str) -> Post:
    """JSON-RPC over POST with the headers a streamable-HTTP MCP client sends, and no token."""

    def post(path: str, payload: dict) -> tuple[int, dict | None]:
        headers = {"User-Agent": "check_live", "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        return _exchange(urllib.request.Request(base.rstrip("/") + path, data=json.dumps(payload).encode(), headers=headers, method="POST"))

    return post


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", default=PRODUCTION, help=f"the instance to check (default {PRODUCTION})")
    parser.add_argument("--no-mcp", action="store_true", help="skip the three POSTs to /mcp; send GET requests only")
    args = parser.parse_args()
    header, checks = run(http_fetch(args.base), None if args.no_mcp else http_post(args.base))
    print(f"{args.base}  {header}")
    for check in checks:
        print(check.line())
    failed = sum(not check.ok for check in checks)
    print(f"{len(checks) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
