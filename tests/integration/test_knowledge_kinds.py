"""Service nodes of every kind: a Sales team merged onto the Quillfern graph.

Engineering answers stay word for word; a process, system, playbook, policy and account
read as what they are; accounts never appear in a blast radius or a load-bearing ranking.
The Sales rows here are a test fixture in the shape the team importer and the Sales seed
use (``seed_company`` reads these attributes), not the demo seed itself.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import algorithms, ask_service  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_kinds_test{tests.TEST_GRAPH_SUFFIX}"
ORG = ["org_shared"]


def sales(**overrides) -> SimpleNamespace:
    team = dict(
        COMPANY=quillfern.COMPANY,
        PEOPLE=[
            {"id": "priya", "name": "Priya Shah", "role": "Head of sales"},
            {"id": "dev", "name": "Dev Patel", "role": "Account manager"},
        ],
        TEAMS=[{"id": "sales", "name": "Sales", "members": ["priya", "dev"]}],
        SERVICES=[
            {"id": "renewals", "name": "Renewals", "kind": "process", "team": "sales", "aliases": "renewals, renewal process"},
            {"id": "hubspot", "name": "HubSpot", "kind": "system", "team": "sales", "aliases": "hubspot, crm"},
            {"id": "pricing-playbook", "name": "Pricing playbook", "kind": "playbook", "team": "sales", "aliases": "pricing playbook"},
            {
                "id": "refund-policy", "name": "Refund policy", "kind": "policy", "team": "sales", "aliases": "refunds, refund policy",
                "summary": "Annual plans are refundable within 30 days of renewal",
            },
            {"id": "enterprise-accounts", "name": "Enterprise accounts", "kind": "account", "team": "sales", "aliases": "enterprise accounts"},
        ],
        DEPENDS_ON=[("renewals", "hubspot"), ("pricing-playbook", "renewals"), ("renewals", "billing-service")],
        WORK=[
            ("priya", "OWNS", "renewals"), ("priya", "OWNS", "pricing-playbook"), ("priya", "OWNS", "refund-policy"),
            ("dev", "OWNS", "hubspot"), ("dev", "OWNS", "enterprise-accounts"), ("dev", "WORKED_ON", "renewals"),
        ],
        DOCUMENTS=[], TICKETS=[], DECISIONS=[], CHATS=[],
    )
    team.update(overrides)
    return SimpleNamespace(**team)


class KindsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        seed.seed_company(sales())

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def test_the_loader_writes_kind_and_old_services_read_as_services(self):
        _, graph = falkordb_client.connect()
        rows = dict(graph.ro_query("MATCH (s:Service) RETURN s.id, s.kind").result_set)
        self.assertEqual(rows["renewals"], "process")
        self.assertEqual(rows["enterprise-accounts"], "account")
        self.assertEqual(rows["billing-service"], "service")

    def test_the_loader_refuses_an_unknown_kind_or_an_account_in_a_dependency(self):
        with self.assertRaises(ValueError):
            seed.seed_company(sales(SERVICES=[{"id": "x", "name": "X", "kind": "department", "team": "sales"}], DEPENDS_ON=[], WORK=[]))
        with self.assertRaises(ValueError):
            seed.seed_company(sales(DEPENDS_ON=[("renewals", "enterprise-accounts")]))

    def test_owners_read_by_kind_and_engineering_answers_are_unchanged(self):
        self.assertTrue(ask_service.ask("who owns billing", ORG).answer.startswith("Billing Service is owned by Arjun Mehta."))
        renewals = ask_service.ask("who runs the renewals process", ORG)
        self.assertEqual(renewals.tool, "who_owns")
        # One sentence: "is run by Priya Shah. Run by the Sales team." repeated itself.
        self.assertTrue(renewals.answer.startswith("The Renewals process is run by Priya Shah for the Sales team."), renewals.answer)
        self.assertIn("Priya Shah -[OWNS]-> Renewals", [step.cite() for step in renewals.path])
        self.assertTrue(ask_service.ask("who handles hubspot", ORG).answer.startswith("The HubSpot system is owned by Dev Patel."))
        self.assertTrue(ask_service.ask("who is responsible for enterprise accounts", ORG).answer.startswith("The Enterprise accounts is owned by Dev Patel."))
        knows = ask_service.ask("who knows renewals", ORG)
        self.assertTrue(knows.answer.startswith("The Renewals process is run by Priya Shah. Ask first: Priya Shah (owns it), then Dev Patel (worked on it)."), knows.answer)

    def test_a_policy_answer_says_what_the_policy_is(self):
        result = ask_service.ask("what is the policy on refunds", ORG)
        self.assertEqual(result.tool, "who_owns")
        self.assertEqual(result.answer, "The Refund policy is owned by Priya Shah. Run by the Sales team. Annual plans are refundable within 30 days of renewal.")

    def test_blast_radius_reads_by_kind_and_crosses_teams(self):
        hubspot = ask_service.ask("what breaks if hubspot goes down", ORG)
        self.assertEqual(hubspot.tool, "blast_radius")
        self.assertEqual(
            hubspot.answer,
            "If the HubSpot system goes down, the Renewals process stalls (1 hop) and the Pricing playbook no longer applies (2 hops). "
            "Depth 2 reached 2 things that depend on it; nothing new past depth 2. "
            "Among them, the Renewals process is the main connector: 1 of the 2 reaches the HubSpot system through it.",
        )
        self.assertIn("Renewals -[DEPENDS_ON]-> HubSpot", [step.cite() for step in hubspot.path])
        one_hop = algorithms.blast_radius("hubspot", ORG, depth=1)
        self.assertEqual(
            one_hop.answer,
            "If the HubSpot system goes down, the Renewals process stalls (1 hop). "
            "1 more depends on it further out (Pricing playbook at depth 2); ask with more hops. "
            "Among them, the Renewals process is the main connector: 1 of the 2 reaches the HubSpot system through it.",
        )
        stalls = ask_service.ask("what stops if renewals is down", ORG)
        self.assertEqual(stalls.tool, "blast_radius")
        self.assertEqual(
            stalls.answer,
            "If the Renewals process stalls, the Pricing playbook no longer applies (1 hop). Depth 1 reached 1 thing that depends on it; nothing new past depth 1.",
        )
        # A sales process that leans on an engineering service shows up in that service's blast radius,
        # after the services, which keep their engineering wording.
        billing = algorithms.blast_radius("billing", ORG)
        self.assertTrue(
            billing.answer.startswith(
                "If Billing Service goes down, these stop with it: API Gateway and Invoice Worker (1 hop). "
                "The Renewals process stalls (1 hop) and the Pricing playbook no longer applies (2 hops). "
                "Depth 2 reached 4 things that depend on it; nothing new past depth 2."
            ),
            billing.answer,
        )
        self.assertEqual(ask_service.route("what depends on hubspot")[0], "blast_radius")

    def test_accounts_are_never_in_dependency_answers(self):
        account = algorithms.blast_radius("enterprise accounts", ORG)
        self.assertTrue(account.found)
        self.assertEqual(account.path, [])
        self.assertEqual(account.answer, "Enterprise accounts is an account: nothing depends on it. Ask who owns it or who knows it instead.")
        ranked = algorithms.load_bearing(10)
        self.assertNotIn("Enterprise accounts", ranked.answer)
        self.assertIn("HubSpot", ranked.answer)

    def test_paths_cross_kinds(self):
        result = algorithms.path_between("Priya Shah", "hubspot", ORG)
        self.assertTrue(result.found)
        self.assertIn("Priya Shah reaches HubSpot", result.answer)
        self.assertIn("Renewals -[DEPENDS_ON]-> HubSpot", [step.cite() for step in result.path])
