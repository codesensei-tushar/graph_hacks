"""Every read tool's queries, EXPLAINed: none plans an All Node Scan unless it is on the allowlist.

Each read tool runs once on Quillfern with its Sales team (and a ticket asked about by key). Every
distinct read query it sent is EXPLAINed, and one whose plan scans every node fails the test unless
its opening is listed in ``ALLOWED`` below. The list only shrinks: fix the query (start from a
labelled, indexed anchor: ``MATCH (s:Service {id: $id}) WITH s MATCH ...``) rather than add to it.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import ask_api, catalogue  # noqa: E402
from src.seed import quillfern, quillfern_sales, seed  # noqa: E402

GRAPH = f"company_query_plans_test{tests.TEST_GRAPH_SUFFIX}"
SCOPES = ["org_shared", "org_internal"]

QUESTIONS = {
    "who_owns": "who owns billing",
    "who_knows": "who knows the auth service",
    "decision_trace": "why did we move the memory layer off postgres",
    "decision_pressure": "is the billing retries decision still the last word",
    "working_groups": "who actually works together",
    "blast_radius": "what breaks if auth goes down",
    "who_should_know": "who should look at the billing exports timing out",
    "if_person_leaves": "what if Maya leaves",
    "onboarding_brief": "I just joined Platform, brief me",
    "reading_list": "what should I read about billing",
    "what_changed": "what changed in billing",
    "connectors": "what connects billing and search",
    "path_between": "how is billing connected to postgres",
    "load_bearing": "which services are load-bearing",
    "risk_map": "where are we at risk",
    "person_profile": "tell me about Arjun",
    "team_dependencies": "what does Payments depend on",
    "decision_health": "how healthy are the Platform decisions",
    "related_memories": "what did anyone say about billing",
    "recall": "what do we remember about invoices",
    "search": "webhook retries",
    "explore": "billing",
    "neighbors": "billing",
    "find": "open tickets about billing",
    "ticket_status": "what is the status of TKT-102",
    "pr_explain": "what did notebook#45 do",
}

# Openings (whitespace collapsed, first 90 characters) of read queries still allowed to plan an
# All Node Scan. Shrink only.
ALLOWED: frozenset[str] = frozenset()


def _opening(query: str) -> str:
    return " ".join(query.split())[:90]


class QueryPlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH), mock.patch.object(seed, "graph_name", return_value=GRAPH)]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        quillfern_sales.seed_sales()
        ask_api.ANSWERS.clear()

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            ask_api.ANSWERS.clear()
            for patch in cls._patches:
                patch.stop()

    def test_no_read_tool_scans_every_node(self):
        seen: dict[str, object] = {}

        def remember(call):
            if call.method == "ro_query" and call.graph == GRAPH:
                seen.setdefault(call.query, call.params)

        falkordb_client.add_query_hook(before=remember)
        try:
            for spec in catalogue.list_tools():
                if spec.needs_approval:
                    continue
                spec.run(ask_api._args_for(spec, QUESTIONS.get(spec.name, "billing")), SCOPES)
        finally:
            falkordb_client.remove_query_hook(remember)
        self.assertGreater(len(seen), 50)
        graph = falkordb_client.client().select_graph(GRAPH)
        scanning = set()
        for query, params in seen.items():
            if query.lstrip().upper().startswith("CALL"):
                continue
            if "All Node Scan" in str(graph.explain(query, params or {})):
                scanning.add(_opening(query))
        self.maxDiff = None
        new = sorted(scanning - ALLOWED)
        self.assertEqual(new, [], "these read queries plan an All Node Scan; start them from a labelled anchor")
        # A fixed query leaves the list: take it off so it cannot come back unnoticed.
        self.assertEqual(sorted(ALLOWED - scanning), [], "these allowed openings no longer scan every node; remove them from ALLOWED")


if __name__ == "__main__":
    unittest.main()
