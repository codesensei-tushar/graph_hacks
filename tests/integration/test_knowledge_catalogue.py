"""The tool catalogue against a real FalkorDB seeded with the fictional Quillfern company.

Every tool runs through its ``ToolSpec`` the way the agent will call it, returns a
``ToolResult`` whose path cites real edges, and the approved ticket writer lands the
ticket in the asker's scope and reports who should look at it.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import ask_service, catalogue  # noqa: E402
from src.domain.slack import compute  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_catalogue_test{tests.TEST_GRAPH_SUFFIX}"
ORG = [ask_service.ORG_SHARED]


def _run(name: str, args: dict, scopes=None):
    spec = catalogue.get_tool(name)
    assert spec is not None, name
    return spec.run(args, scopes or ORG)


class KnowledgeCatalogueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def test_who_owns_names_the_owner_and_the_team_with_the_path(self):
        result = _run("who_owns", {"name": "billing"})
        self.assertTrue(result.found)
        self.assertIn("Billing Service is owned by Arjun Mehta", result.answer)
        self.assertIn("Payments team", result.answer)
        cites = [step.cite() for step in result.path]
        self.assertIn("Arjun Mehta -[OWNS]-> Billing Service", cites)
        self.assertIn("Payments -[RUNS]-> Billing Service", cites)
        owner = next(step for step in result.path if step.rel == "OWNS")
        self.assertEqual((owner.from_id, owner.to_id, owner.from_label, owner.to_label), ("arjun", "billing-service", "Person", "Service"))

    def test_who_owns_a_document_walks_to_its_service(self):
        result = _run("who_owns", {"name": "Invoice format spec"})
        self.assertIn("Invoice format spec is about Invoice Worker", result.answer)
        self.assertIn("owned by Lena Fischer", result.answer)
        self.assertEqual(result.path[0].cite(), "Invoice format spec -[DESCRIBES]-> Invoice Worker")

    def test_the_keyword_router_separates_owner_from_everyone_involved(self):
        self.assertEqual(ask_service.ask("who owns billing", ORG).tool, "who_owns")
        self.assertEqual(ask_service.ask("who owns billing, and who else has worked on it", ORG).tool, "who_knows")
        self.assertEqual(ask_service.ask("why did we move the memory layer off postgres", ORG).tool, "decision_trace")
        self.assertEqual(ask_service.ask("what breaks if auth goes down", ORG).tool, "blast_radius")

    def test_unknown_names_are_reported_not_guessed(self):
        for name in ("who_owns", "who_knows", "blast_radius"):
            result = _run(name, {"name": "the espresso machine"})
            self.assertFalse(result.found, name)
            self.assertEqual(result.path, [])
            self.assertEqual(result.candidates, [])
            self.assertIn("Nothing in the graph matches “espresso machine”", result.answer)
        self.assertFalse(_run("decision_trace", {"topic": "espresso"}).found)

    def test_a_name_that_spans_two_nodes_is_refused_with_the_nearest_matches(self):
        result = _run("who_knows", {"name": "who knows the payments gateway"})
        self.assertFalse(result.found)
        self.assertEqual(result.path, [])
        self.assertEqual(result.candidates, ["Billing Service", "API Gateway"])
        self.assertIn("Nothing in the graph matches “payments gateway” as one thing", result.answer)
        self.assertIn("Nearest: Billing Service (known as “payments”), API Gateway (known as “gateway”)", result.answer)
        # The no-model router gives the same refusal, and the dict the formatter reads carries it.
        routed = ask_service.ask("who knows the payments gateway", ORG).to_dict()
        self.assertFalse(routed["found"])
        self.assertEqual(routed["candidates"], ["Billing Service", "API Gateway"])

    def test_a_typo_still_offers_the_nearest_name(self):
        result = _run("who_owns", {"name": "invoce worker"})
        self.assertFalse(result.found)
        self.assertEqual(result.candidates[0], "Invoice Worker")
        self.assertIn("Nearest: Invoice Worker", result.answer)

    def test_the_whole_question_resolves_to_the_longest_phrase(self):
        self.assertEqual(_run("blast_radius", {"name": "what breaks if the auth service goes down"}).path[0].to_name, "Auth Service")
        self.assertEqual(_run("who_knows", {"name": "who is on TKT-102"}).path[0].to_name, "TKT-102")
        decision = _run("decision_trace", {"topic": "why did we move the memory layer off postgres"})
        self.assertTrue(decision.found)
        self.assertTrue(decision.answer.startswith("Move the memory layer off Postgres"))

    def test_every_found_result_cites_at_least_one_edge(self):
        for name, args in (
            ("who_owns", {"name": "auth"}),
            ("who_knows", {"name": "memory layer"}),
            ("decision_trace", {"topic": "memory layer off postgres"}),
            ("blast_radius", {"name": "auth service"}),
        ):
            result = _run(name, args)
            self.assertTrue(result.found, name)
            self.assertTrue(result.path, f"{name} answered without a path: {result.answer}")
            self.assertEqual(result.tool, name)

    def test_approved_ticket_runs_through_the_catalogue(self):
        room = "slack_channel_GCATALOG"
        text = compute.execute_approved_action(compute.TICKET_TOOL_NAME, {"title": "Billing exports time out"}, room)
        self.assertIn("Filed *TKT-104* about Billing Service", text)
        self.assertIn("Arjun Mehta", text)
        self.assertIn("TKT-104 -[ABOUT]-> Billing Service", text)
        _, graph = falkordb_client.connect()
        scope = graph.query("MATCH (t:Ticket {id: 'TKT-104'}) RETURN t.scope").result_set[0][0]
        self.assertEqual(scope, room)
        # Only approved writers run this way.
        with self.assertRaises(ValueError):
            compute.execute_approved_action("who_owns", {"name": "billing"}, room)
        with self.assertRaises(ValueError):
            compute.execute_approved_action("no_such_tool", {}, room)
