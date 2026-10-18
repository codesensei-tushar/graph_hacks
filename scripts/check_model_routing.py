"""Check the model's tool routing on the demo questions, once, by hand.

The only place this project spends model calls on purpose: one structured call per
question (41 in all, fast profile), printing the tool and arguments the model chose
next to what the keyword router would do and what the recording script expects. Run
it after changing a tool description or the routing prompt, read the table, and paste
it into the pull request. It is not a test and must never run in CI.

    python scripts/check_model_routing.py            # needs LLM_API_KEY in .env
    python scripts/check_model_routing.py --dry-run  # keyword router only, no calls

The thread follow-ups get the same thread text the agent would see. Names are not
resolved against a graph here, so the "things named so far" hint is left out; the
production seed and Quillfern would need two different graphs.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import settings  # noqa: E402
from src.domain.knowledge import agent  # noqa: E402
from src.integrations.llm.model import ModelUnavailable  # noqa: E402

BOT = "<@UBOT>"

# (label, question, expected tool, thread the question is asked in). The expected tool can carry the
# arguments it must get, each with the values allowed: blast_radius must not flip its direction.
DEPENDENTS = ("blast_radius", {"direction": (None, "dependents")})
DEPENDENCIES = ("blast_radius", {"direction": ("dependencies",)})
QUESTIONS = [
    ("prod 1", "who owns the memory layer, and who else has worked on it", "who_knows", None),
    ("prod 2", "why do we store memory in FalkorDB instead of a hosted memory API", "decision_trace", None),
    ("prod 3", "why did we cut vector recall", "decision_trace", None),
    ("prod 4", "what breaks if the claude api goes down, one hop", "blast_radius", None),
    (
        "prod 4b",
        "and three hops out",
        "blast_radius",
        [
            {"user": "USARA", "text": f"{BOT} what breaks if the claude api goes down, one hop"},
            {"user": "UBOT", "bot_id": "B1", "text": "If Claude API goes down, these stop with it: LLM gateway (1 hop). 4 more break further out; ask with more hops.\n\n_Tool `blast_radius`, picked by the model. Read from: public channels_"},
        ],
    ),
    ("prod 5", "file a ticket: scheduler jobs run twice after a restart", "ingest_ticket", None),
    ("prod 6", "who owns the linear integration", "who_owns", None),
    ("quill 1", "who owns billing, and who else has worked on it", "who_knows", None),
    ("quill 2", "why did we move the memory layer off Postgres", "decision_trace", None),
    ("quill 3", "why did we move billing retries to the invoice worker", "decision_trace", None),
    ("quill 4", "what breaks if auth goes down", DEPENDENTS, None),
    (
        "quill 5",
        "who should look at it",
        "who_knows",
        [
            {"user": "UKOFI", "text": f"{BOT} ticket: billing exports time out"},
            {"user": "UBOT", "bot_id": "B1", "text": "Filed *TKT-104* about Billing Service. Billing Service is owned by Arjun Mehta.\n\n_Tool `ingest_ticket`, run after your approval. Read from: public channels_"},
        ],
    ),
    ("quill 6", "who knows the payments gateway", "who_knows", None),
    ("quill 7", "how is maya connected to the invoice worker", "path_between", None),
    ("quill 8", "who should know about notebook#45", "who_should_know", None),
    ("quill 9", "who else should review notebook#52", "who_should_know", None),
    # The Sales beat, on Quillfern with its Sales team imported.
    ("sales S1", "who owns renewals, and who else has worked on it", "who_knows", None),
    ("sales S2", "what breaks if hubspot goes down", DEPENDENTS, None),
    ("sales S3", "why did we change pricing", "decision_trace", None),
    ("sales B4", "what if priya shah leaves", "if_person_leaves", None),
    # The any-team tools.
    ("team 1", "what breaks if Arjun leaves", "if_person_leaves", None),
    ("team 2", "what is Arjun the only one who knows", "if_person_leaves", None),
    ("team 3", "I just joined Sales, what should I know", "onboarding_brief", None),
    ("team 4", "brief me on the payments team", "onboarding_brief", None),
    ("team 5", "who connects Sales and Engineering", "connectors", None),
    ("team 6", "what does the payments team own", "onboarding_brief", None),
    # blast_radius the other way: what a service itself depends on.
    ("deps 1", "what does the invoice worker depend on", DEPENDENCIES, None),
    ("deps 2", "what are billing's dependencies", DEPENDENCIES, None),
    # Risk across every team, not one person leaving or one service failing.
    ("risk 1", "what are our riskiest services", "risk_map", None),
    ("risk 2", "where are we one person away from an outage", "risk_map", None),
    # Company-level reads across teams and across decisions.
    ("org 1", "which teams depend on platform", "team_dependencies", None),
    ("org 2", "which decisions are stale", "decision_health", None),
    # reading_list: what to read before a change, and the neighbour it must not take.
    ("read 1", "what should I read before touching the invoice worker", "reading_list", None),
    ("read 2", "which docs describe the auth service", "reading_list", None),
    ("read 3", "I'm about to change billing, which documents should I read first", "reading_list", None),
    (
        "read 4",
        "what should I read before touching it",
        "reading_list",
        [
            {"user": "ULENA", "text": f"{BOT} what breaks if billing goes down"},
            {"user": "UBOT", "bot_id": "B1", "text": "If Billing Service goes down, these stop with it: API Gateway and Invoice Worker (1 hop).\n\n_Tool `blast_radius`, picked by the model. Read from: public channels_"},
        ],
    ),
    ("read 5", "what should I know about billing", "who_knows", None),
    # search: words that name no node. "who knows about refunds" may go to who_knows, which falls back to search.
    ("search 1", "search for webhooks", "search", None),
    ("search 2", "anything about pdf layout", "search", None),
    # what_changed: a dated window around a thing, a team or the company.
    ("changed 1", "what changed this week", "what_changed", None),
    ("changed 2", "what's new in payments", "what_changed", None),
    ("changed 3", "billing is failing, what changed", "what_changed", None),
    ("changed 4", "what did the sales team decide recently", ("what_changed", {"only": ("decisions",)}), None),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="keyword router only; no model calls")
    args = parser.parse_args()
    if not args.dry_run and not settings.llm_enabled:
        print("LLM_API_KEY is not set; nothing to check (use --dry-run for the keyword router alone).", file=sys.stderr)
        return 2

    rows = []
    for label, question, expected, thread in QUESTIONS:
        keywords = agent.choose_with_keywords(question)
        if args.dry_run:
            chosen = keywords
        else:
            try:
                chosen = agent._choose_with_model(question, thread, agent.catalogue.list_tools())
            except (ModelUnavailable, agent.InvalidToolChoice) as error:
                chosen = agent.ToolChoice("(failed)", {"error": str(error)}, "model")
        rows.append((label, question, chosen.tool, chosen.args, keywords, expected))

    header = ("step", "question", "model tool", "model args", "keyword tool", "expected", "ok")
    print("| " + " | ".join(header) + " |")
    print("|" + "|".join(" --- " for _ in header) + "|")
    misses = 0
    for label, question, tool, chosen_args, keywords, expected in rows:
        ok = _matches(tool, chosen_args, expected)
        misses += not ok
        shown_args = ", ".join(f"{key}={value!r}" for key, value in chosen_args.items())
        keyword_ok = "" if _matches(keywords.tool, keywords.args, expected) else " (miss)"
        print(f"| {label} | {question} | {tool} | {shown_args} | {keywords.tool}{keyword_ok} | {_shown(expected)} | {'yes' if ok else 'NO'} |")
    print(f"\n{len(rows) - misses} of {len(rows)} match the recording script.", file=sys.stderr)
    return 1 if misses else 0


def _matches(tool: str, chosen_args: dict, expected) -> bool:
    name, required = (expected, {}) if isinstance(expected, str) else expected
    return tool == name and all(chosen_args.get(key) in allowed for key, allowed in required.items())


def _shown(expected) -> str:
    if isinstance(expected, str):
        return expected
    name, required = expected
    return name + " " + ", ".join(f"{key} in {'/'.join(str(value) for value in allowed)}" for key, allowed in required.items())


if __name__ == "__main__":
    sys.exit(main())
