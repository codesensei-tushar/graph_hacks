"""Smoke-check a running instance before a recording, after a seed refresh, or after a deploy.

Read-only: it sends the same public GET requests the page sends, with no token, and never
a POST. Every example chip the server lists for its company must answer, except the last,
which must be refused; so must the questions README.md's "Try it in two minutes" tells a
judge to paste, and the team page and deep link it names must load. One PASS or FAIL line
per check; the exit status is 1 when any check fails.

    python scripts/check_live.py                                # production
    python scripts/check_live.py --base http://localhost:8000

The ask routes allow 30 calls a minute per address and this makes about 20, so wait a
minute before running it twice.
"""

import argparse
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

Fetch = Callable[[str], tuple[int, dict | None]]


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


def run(fetch: Fetch) -> tuple[str, list[Check]]:
    """The header line and every check, from ``fetch(path) -> (status, parsed JSON or None)``."""
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
    return header, checks


def http_fetch(base: str) -> Fetch:
    def fetch(path: str) -> tuple[int, dict | None]:
        request = urllib.request.Request(base.rstrip("/") + path, headers={"User-Agent": "check_live"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                status, body = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, body = error.code, error.read()
        except urllib.error.URLError as error:
            return 0, {"error": str(error.reason)}
        try:
            return status, json.loads(body)
        except ValueError:
            return status, None

    return fetch


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", default=PRODUCTION, help=f"the instance to check (default {PRODUCTION})")
    args = parser.parse_args()
    header, checks = run(http_fetch(args.base))
    print(f"{args.base}  {header}")
    for check in checks:
        print(check.line())
    failed = sum(not check.ok for check in checks)
    print(f"{len(checks) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
