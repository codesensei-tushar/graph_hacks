"""CRM questions over a synced HubSpot portal: customers, opportunities, deals by stage, the
pipeline, accounts by owner, and the Sales team (which runs HubSpot) tied to every synced account.

Quillfern with its Sales team, and the fixture portal (``tests/fixtures/hubspot``) synced the way
the scheduler does it. Dev and Priya are linked to their Persons; Riley is an unlinked HubSpot user.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.connectors import registry, sync  # noqa: E402
from src.connectors.base import Install  # noqa: E402
from src.connectors.hubspot.connector import HubSpotConnector  # noqa: E402
from src.core import scheduler  # noqa: E402
from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import ask_service, brief, catalogue, find, team_page  # noqa: E402
from src.seed import quillfern, quillfern_sales, seed  # noqa: E402
from tests.fixtures.hubspot.fake_hubspot import SERVER_MS, FakeHubSpot  # noqa: E402

GRAPH = f"company_crm_questions_test{tests.TEST_GRAPH_SUFFIX}"
INSTALL = Install("hubspot", config={"token": "pat-fake-never-shown"})
ORG = ["org_shared"]
SLACK = ["org_shared", "org_internal"]


def _ask(question: str, scopes=None):
    tool, args = ask_service.route(question)
    return catalogue.get_tool(tool).run(args, scopes or SLACK)


def _cites(result) -> list[str]:
    return [step.cite() for step in result.path]


class CrmQuestionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
            mock.patch.object(scheduler, "schedule"),
            mock.patch.object(sync, "MORE_DELAY_SECONDS", 0.0),
        ]
        for patch in cls._patches:
            patch.start()
        cls._registered = registry.get("hubspot")
        cls.fake = FakeHubSpot()
        registry.register(HubSpotConnector(client_for=lambda install: cls.fake, clock=lambda: SERVER_MS / 1000))
        seed.reset_and_seed(quillfern)
        quillfern_sales.seed_sales()
        registry.get("hubspot")._directories.clear()
        sync._containers.clear()
        registry.configure(INSTALL)
        for _ in range(10):
            if sync.sync_step("hubspot")["status"] != "synced":
                break

    @classmethod
    def tearDownClass(cls):
        registry.unconfigure("hubspot")
        if cls._registered is not None:
            registry.register(cls._registered)
        else:
            registry._connectors.pop("hubspot", None)
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in reversed(cls._patches):
                patch.stop()

    def test_our_customers(self):
        for question in ("show me our customers", "who are our customers"):
            with self.subTest(question=question):
                result = _ask(question)
                self.assertEqual(result.tool, "find")
                self.assertEqual(result.answer, "2 customer accounts: Hollowpine Health and Larkspur Freightways.")

    def test_open_opportunities(self):
        self.assertEqual(_ask("open opportunities").answer, "1 open opportunity account: Tidewhistle Robotics.")

    def test_deals_in_a_stage(self):
        result = _ask("deals in contract sent")
        self.assertEqual(result.answer, "1 account with a deal in contract sent: Larkspur Freightways.")
        self.assertEqual(_cites(result), ["Priya Shah -[WORKED_ON]-> Larkspur Freightways"])
        self.assertEqual(_ask("which deals are in qualified to buy?").answer, "1 account with a deal in qualified to buy: Tidewhistle Robotics.")

    def test_closed_won_is_a_deal_stage_not_the_closed_state(self):
        self.assertEqual(_ask("which deals are closed won").answer, "1 account with a deal in closed won: Larkspur Freightways.")
        self.assertIn("Bramblecote Analytics", _ask("deals in closed lost").answer)

    def test_the_open_pipeline_leaves_closed_deals_out(self):
        result = _ask("show me the sales pipeline")
        self.assertIn("Larkspur Freightways", result.answer)
        self.assertIn("Tidewhistle Robotics", result.answer)
        self.assertNotIn("Bramblecote", result.answer)  # its only deal is closed lost
        self.assertTrue(result.answer.startswith("2 accounts with an open deal:"), result.answer)

    def test_accounts_by_owner_include_an_unlinked_hubspot_user(self):
        self.assertEqual(_ask("which accounts does Dev Patel own").answer, "3 accounts owned by Dev Patel: Enterprise accounts, Larkspur Freightways and Saltwick Outfitters.")
        riley = _ask("which accounts does Riley Chen own")
        self.assertTrue(riley.answer.startswith("1 account owned by Riley Chen (HubSpot): Bramblecote Analytics."), riley.answer)
        self.assertIn("2 closed accounts", find.find("Service", {"kind": "account", "state": "closed"}, SLACK).answer)

    def test_the_public_reader_sees_no_accounts(self):
        for question in ("show me our customers", "open opportunities", "deals in contract sent", "show me the sales pipeline"):
            with self.subTest(question=question):
                self.assertNotIn("Larkspur", _ask(question, ORG).answer)

    def test_the_team_that_runs_hubspot_holds_every_synced_account(self):
        page = team_page.team_page("sales", SLACK)
        self.assertEqual(page.synced_via, ["HubSpot"])
        self.assertEqual([account.name for account in page.synced], ["Bramblecote Analytics", "Hollowpine Health"])
        self.assertEqual(page.synced[0].owners, ["Riley Chen (HubSpot)"])
        self.assertIn("Riley Chen (HubSpot) -[OWNS]-> Bramblecote Analytics", [step.cite() for step in page.path])
        result = brief.onboarding_brief("brief me on the sales team", SLACK)
        self.assertIn(
            "Through HubSpot, which it runs, it has 2 more accounts: Bramblecote Analytics (owned by Riley Chen (HubSpot)) and Hollowpine Health.",
            result.answer,
        )
        public = team_page.team_page("sales", ORG)
        self.assertEqual((public.synced, public.synced_total), ([], 0))
        self.assertNotIn("Through HubSpot", brief.onboarding_brief("brief me on the sales team", ORG).answer)

    def test_a_team_that_does_not_run_hubspot_has_none(self):
        self.assertEqual(team_page.team_page("payments", SLACK).synced_total, 0)


if __name__ == "__main__":
    unittest.main()
