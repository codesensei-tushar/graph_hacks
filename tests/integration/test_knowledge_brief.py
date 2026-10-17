"""``onboarding_brief`` on a real FalkorDB: Quillfern with its Sales team imported.

The brief composes the team page and who_knows, so every assertion here is a read those
already make. The setup adds a team with nothing in it and a private Payments ticket.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import ask_service, catalogue, resolve  # noqa: E402
from src.seed import quillfern, quillfern_sales, seed  # noqa: E402

GRAPH = f"company_brief_test{tests.TEST_GRAPH_SUFFIX}"
ORG = ["org_shared"]
PRIVATE = "slack_channel_C7"


def _cites(result) -> list[str]:
    return [step.cite() for step in result.path]


def _brief(question: str, scopes=None):
    tool, args = ask_service.route(question)
    assert tool == "onboarding_brief", (question, tool)
    return catalogue.get_tool(tool).run(args, scopes or ORG)


class OnboardingBriefTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        quillfern_sales.seed_sales()
        _, graph = falkordb_client.connect()
        graph.query(
            """
            MATCH (billing:Service {id: 'billing-service'})
            CREATE (:Team {id: 'legal', name: 'Legal'}),
                   (:Ticket {id: 'TKT-150', name: 'TKT-150', title: 'Refund exports drop the currency', scope: $private})-[:ABOUT]->(billing)
            """,
            {"private": PRIVATE},
        )

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def test_an_engineering_team(self):
        result = _brief("brief me on the payments team")
        self.assertTrue(result.found)
        self.assertEqual(
            result.answer,
            "The Payments team is Arjun Mehta (Billing engineer) and Lena Fischer (Backend engineer). "
            "It runs 2 services: Billing Service (owned by Arjun Mehta) and Invoice Worker (owned by Lena Fischer). "
            "Standing decision: *Retry renewal webhooks inside the billing service* (20 Sep 2026). "
            # Lena owns the Invoice Worker and worked on Billing: summed over both, she outweighs Billing's owner.
            "Ask first: Lena Fischer (Invoice Worker and Billing Service), then Arjun Mehta (Billing Service) and Tomas Reyes (Billing Service). "
            "Open ticket: TKT-102 (“Annual plan renewals charge twice on retry”, about Billing Service).",
        )
        cites = _cites(result)
        for cite in (
            "Arjun Mehta -[MEMBER_OF]-> Payments",
            "Payments -[RUNS]-> Billing Service",
            "Arjun Mehta -[OWNS]-> Billing Service",
            "Retry renewal webhooks inside the billing service -[ABOUT]-> Billing Service",
            "Tomas Reyes -[WORKED_ON]-> Billing Service",
            "TKT-102 -[ABOUT]-> Billing Service",
        ):
            self.assertIn(cite, cites)
        self.assertEqual(len(cites), len(set(cites)), cites)

    def test_a_sales_team_reads_by_kind_with_the_decision_it_replaced(self):
        result = _brief("I just joined Sales, what should I know")
        self.assertEqual(
            result.answer,
            "Sales: Renewals, pricing and the enterprise accounts that pay for Quillfern. "
            "The Sales team is Dev Patel (Account manager), Noor Haddad (Sales operations) and Priya Shah (Head of sales). "
            "It runs 1 system: HubSpot (owned by Noor Haddad); 1 process: Renewals (run by Priya Shah); "
            "1 account: Enterprise accounts (owned by Dev Patel); 1 playbook: Pricing playbook (owned by Priya Shah). "
            "Standing decision: *Sell annual plans only at renewal* (1 Sep 2026), which replaced *Discount monthly plans at renewal* (2 Mar 2026). "
            "Ask first: Priya Shah (Pricing playbook and Renewals), then Noor Haddad (HubSpot and Pricing playbook) and Dev Patel (Enterprise accounts and Renewals).",
        )
        cites = _cites(result)
        self.assertIn("Sell annual plans only at renewal -[SUPERSEDES]-> Discount monthly plans at renewal", cites)
        self.assertIn("Sell annual plans only at renewal -[ABOUT]-> Renewals", cites)
        self.assertIn("Sales -[RUNS]-> Enterprise accounts", cites)
        self.assertEqual(len(cites), len(set(cites)), cites)

    def test_someone_outside_the_team_can_be_who_to_ask(self):
        result = _brief("get me up to speed on data")
        self.assertIn(
            "Standing decision: *Move the memory layer off Postgres* (15 Sep 2026), which replaced *Keep notebook links in Postgres join tables* (1 Jun 2026).",
            result.answer,
        )
        # Sara (Platform) owns the Graph Store that Data runs; Maya (Platform) worked on the Memory Layer.
        self.assertIn("Ask first: Kofi Mensah (Memory Layer and Search Service), then Sara Lindqvist (Graph Store) and Maya Ortiz (Memory Layer).", result.answer)
        self.assertIn("Maya Ortiz -[WORKED_ON]-> Memory Layer", _cites(result))

    def test_every_phrasing_routes_to_the_brief(self):
        expected = _brief("brief me on the payments team").answer
        for question in ("I'm new to the payments team", "what should I know about the payments team", "onboard me to payments", "get me up to speed on payments"):
            with self.subTest(question=question):
                self.assertEqual(_brief(question).answer, expected)

    def test_an_empty_team_and_a_team_not_in_the_graph(self):
        empty = _brief("brief me on the legal team")
        self.assertEqual(empty.answer, "The Legal team has no members in the graph yet. Legal runs nothing in the graph yet.")
        self.assertEqual(empty.path, [])
        miss = _brief("I just joined Marketing, what should I know")
        self.assertFalse(miss.found)
        self.assertEqual(miss.answer, "Nothing in the graph matches “marketing”. Name a team that exists in the graph.")
        self.assertEqual(miss.answer, resolve.describe_miss(resolve.resolve_name("I just joined Marketing, what should I know", ("Team",), ORG), "team"))

    def test_a_private_ticket_shows_only_to_a_reader_who_can_see_it(self):
        public = _brief("brief me on the payments team")
        self.assertNotIn("TKT-150", public.answer + " ".join(_cites(public)))
        private = _brief("brief me on the payments team", ORG + [PRIVATE])
        self.assertIn(
            "Open tickets: TKT-102 (“Annual plan renewals charge twice on retry”, about Billing Service) "
            "and TKT-150 (“Refund exports drop the currency”, about Billing Service).",
            private.answer,
        )
        self.assertIn("TKT-150 -[ABOUT]-> Billing Service", _cites(private))


if __name__ == "__main__":
    unittest.main()
