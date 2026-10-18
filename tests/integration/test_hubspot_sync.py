"""HubSpot through the connector framework, on a real FalkorDB, from a fake portal.

Quillfern with its Sales team imported, and the fixture portal (``tests/fixtures/hubspot``)
synced the way the scheduler does it, one ``sync.sync_step`` at a time. Priya and Dev are
HubSpot users with their Quillfern emails, so their accounts land on their Person; Riley is a
partner's user and stays an account of their own. Then the questions Sales asks in Slack.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.connectors import registry, sync  # noqa: E402
from src.connectors.base import Install  # noqa: E402
from src.connectors.hubspot.connector import HubSpotConnector  # noqa: E402
from src.core import scheduler  # noqa: E402
from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import ask_service, catalogue  # noqa: E402
from src.seed import quillfern, quillfern_sales, seed  # noqa: E402
from tests.fixtures.hubspot.fake_hubspot import SERVER_MS, FakeHubSpot  # noqa: E402

GRAPH = f"company_hubspot_test{tests.TEST_GRAPH_SUFFIX}"
INSTALL = Install("hubspot", config={"token": "pat-fake-never-shown"})
ORG = ["org_shared"]
SLACK = ["org_shared", "org_internal"]


def _rows(query: str, params: dict | None = None) -> list:
    _, g = falkordb_client.connect()
    return g.query(query, params or {}).result_set


def _ask(question: str, scopes=None):
    tool, args = ask_service.route(question)
    return catalogue.get_tool(tool).run(args, scopes or SLACK)


def _cites(result) -> list[str]:
    return [step.cite() for step in result.path]


class HubSpotSyncTest(unittest.TestCase):
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
        registry.register(HubSpotConnector(client_for=lambda install: cls.fake, clock=lambda: SERVER_MS / 1000))

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

    def setUp(self):
        seed.reset_and_seed(quillfern)
        quillfern_sales.seed_sales()
        type(self).fake = FakeHubSpot()
        sync._containers.clear()
        registry.configure(INSTALL)
        self._drain()

    def _drain(self, limit: int = 10) -> list[dict]:
        reports = []
        for _ in range(limit):
            reports.append(sync.sync_step("hubspot"))
            if reports[-1]["status"] != "synced":
                break
        return reports

    def test_the_backfill_writes_each_account_with_its_owner_and_who_worked_it(self):
        rows = _rows("MATCH (s:Service {source: 'hubspot'}) RETURN s.name, s.kind, s.state, s.status, s.scope ORDER BY s.name")
        self.assertEqual(
            rows,
            [
                ["Bramblecote Analytics", "account", "closed", "salesqualifiedlead", "org_internal"],
                ["Hollowpine Health", "account", "open", "customer", "org_internal"],
                ["Larkspur Freightways", "account", "open", "customer", "org_internal"],
                ["Saltwick Outfitters", "account", "closed", "other", "org_internal"],
                ["Tidewhistle Robotics", "account", "open", "opportunity", "org_internal"],
            ],
        )
        links = _rows("MATCH (u:ExternalUser {source: 'hubspot'}) OPTIONAL MATCH (u)-[:IS]->(p:Person) RETURN u.name, p.id ORDER BY u.name")
        self.assertEqual(links, [["Dev Patel", "dev"], ["Priya Shah", "priya"], ["Riley Chen", None]])
        edges = _rows(
            """
            MATCH (who)-[r:OWNS|WORKED_ON]->(s:Service {source: 'hubspot'})
            RETURN labels(who)[0], who.name, type(r), s.name, r.stage, r.evidence ORDER BY s.name, type(r), who.name
            """
        )
        self.assertEqual(
            edges,
            [
                ["ExternalUser", "Riley Chen", "OWNS", "Bramblecote Analytics", None, None],
                ["ExternalUser", "Riley Chen", "WORKED_ON", "Bramblecote Analytics", "Closed Lost", "deal"],
                ["Person", "Dev Patel", "OWNS", "Larkspur Freightways", None, None],
                ["Person", "Dev Patel", "WORKED_ON", "Larkspur Freightways", "Closed Won", "deal"],
                ["Person", "Priya Shah", "WORKED_ON", "Larkspur Freightways", "Contract Sent", "deal"],
                ["Person", "Dev Patel", "OWNS", "Saltwick Outfitters", None, None],
                ["Person", "Priya Shah", "OWNS", "Tidewhistle Robotics", None, None],
                ["Person", "Priya Shah", "WORKED_ON", "Tidewhistle Robotics", "Qualified To Buy", "deal"],
            ],
        )
        status = sync.status()[0]
        self.assertEqual((status["account"], status["containers"][0]["items"], status["containers"][0]["backfilled"]), ("HubSpot portal 24681357", 5, True))

    def test_slack_askers_get_the_owner_and_who_to_ask(self):
        owner = _ask("who owns Larkspur Freightways")
        self.assertEqual(owner.answer, "The Larkspur Freightways account is owned by Dev Patel.")
        self.assertEqual(_cites(owner), ["Dev Patel -[OWNS]-> Larkspur Freightways"])
        knows = _ask("who knows Larkspur Freightways")
        self.assertEqual(knows.answer, "The Larkspur Freightways account is owned by Dev Patel. Ask first: Dev Patel (owns it, worked on it), then Priya Shah (worked on it).")
        self.assertIn("Priya Shah -[WORKED_ON]-> Larkspur Freightways", _cites(knows))
        # A HubSpot user with no Person is named with its tool, so it is never taken for a namesake.
        self.assertEqual(_ask("who owns Bramblecote Analytics").answer, "The Bramblecote Analytics account is owned by Riley Chen (HubSpot).")
        self.assertIn("Riley Chen (HubSpot) (owns it, worked on it)", _ask("who knows Bramblecote Analytics").answer)

    def test_the_public_page_never_sees_hubspot(self):
        for question in ("who owns Larkspur Freightways", "who knows Larkspur Freightways"):
            result = _ask(question, ORG)
            self.assertFalse(result.found, question)
            self.assertNotIn("Dev Patel", result.answer)

    def test_if_dev_leaves_his_accounts_are_the_gap(self):
        result = _ask("what if Dev Patel leaves")
        self.assertIn("Larkspur Freightways account: Dev Patel is its only owner; next in line: Priya Shah.", result.answer)
        # The lost renewal's owner has been deactivated, so nobody else is on Saltwick.
        self.assertIn("Saltwick Outfitters account: Dev Patel is its only owner and the only one who knows it.", result.answer)
        self.assertIn("Dev Patel -[OWNS]-> Saltwick Outfitters", _cites(result))

    def test_a_poll_moves_an_account_to_its_new_owner(self):
        self.fake.companies["9001"]["properties"].update(hubspot_owner_id="101", hs_lastmodifieddate="2026-10-18T00:00:00.000Z")
        self.assertTrue(sync.sync_now("hubspot"))
        reports = self._drain()
        self.assertEqual([report.get("items") for report in reports if report["status"] == "synced"], [1, 0])
        self.assertEqual(_ask("who owns Larkspur Freightways").answer, "The Larkspur Freightways account is owned by Priya Shah.")
        self.assertEqual(_rows("MATCH (:Person {id: 'dev'})-[r]->(:Service {name: 'Larkspur Freightways'}) RETURN type(r)"), [["WORKED_ON"]])

    def test_an_admin_mapping_links_an_owner_whose_login_is_another_email(self):
        # Riley's HubSpot login is a partner address; the admin says Riley is Noor in the brain.
        registry.configure(Install("hubspot", config={"token": "pat-fake-never-shown", "people": "104=Noor@Quillfern.example.com"}))
        sync.sync_now("hubspot", full=True)
        self._drain()
        self.assertEqual(_rows("MATCH (:ExternalUser {external_id: '104'})-[:IS]->(p:Person) RETURN p.id"), [["noor"]])
        self.assertEqual(_ask("who owns Bramblecote Analytics").answer, "The Bramblecote Analytics account is owned by Noor Haddad.")

    def test_an_owner_who_joins_the_brain_later_is_linked_by_the_next_poll(self):
        _rows("CREATE (:Person {id: 'riley', name: 'Riley Chen', email: 'riley@partner.example.org'})")
        sync.sync_now("hubspot")
        self._drain()
        self.assertEqual(_rows("MATCH (:ExternalUser {external_id: '104'})-[:IS]->(p:Person) RETURN p.id"), [["riley"]])
        owner = _ask("who owns Bramblecote Analytics")
        self.assertEqual(owner.answer, "The Bramblecote Analytics account is owned by Riley Chen.")
        self.assertEqual(_cites(owner), ["Riley Chen -[OWNS]-> Bramblecote Analytics"])

    def test_a_second_full_sync_changes_nothing(self):
        def counts():
            return _rows("MATCH (n) WHERE n.source = 'hubspot' OR n.install = 'hubspot:default' RETURN count(n)")[0][0], _rows("MATCH ()-[r {install: 'hubspot:default'}]->() RETURN count(r)")[0][0]

        before = counts()
        sync.sync_now("hubspot", full=True)
        self._drain()
        self.assertEqual(counts(), before)


if __name__ == "__main__":
    unittest.main()
