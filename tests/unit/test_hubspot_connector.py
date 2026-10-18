"""The HubSpot connector on the contract: the install, the portal, and every page the cursor walks."""

import json
import unittest
from unittest import mock

from src.connectors import registry
from src.connectors.base import Container, CredentialsRejected, Install, InstallInfo
from src.connectors.hubspot import connector as hubspot
from src.connectors.http import HttpError
from tests.fixtures.hubspot.fake_hubspot import SERVER_MS, FakeHubSpot

INSTALL = Install("hubspot", config={"token": "pat-test"})
NOW = SERVER_MS / 1000
LARKSPUR = "hubspot:default:company:9001"


class Clock:
    def __init__(self, now: float = NOW):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _connector(fake: FakeHubSpot, clock: Clock | None = None) -> hubspot.HubSpotConnector:
    return hubspot.HubSpotConnector(client_for=lambda install: fake, clock=clock or Clock())


def _container(connector) -> Container:
    return connector.containers(INSTALL)[0]


def _cursor(page) -> dict:
    return json.loads(page.next_cursor)


class InstallTest(unittest.TestCase):
    def test_the_module_registers_hubspot_and_the_token_comes_from_env(self):
        import src.connectors.hubspot  # noqa: F401

        connector = registry.get("hubspot")
        self.assertIsInstance(connector, hubspot.HubSpotConnector)
        self.assertEqual((connector.source, connector.required, connector.accepts_webhooks), ("hubspot", ("token",), False))
        install = registry.install_from_env(connector, {"CONNECTOR_HUBSPOT_TOKEN": "pat-na1-x"})
        self.assertEqual((install.key, install.get("token")), ("hubspot:default", "pat-na1-x"))
        self.assertIsNone(registry.install_from_env(connector, {"CONNECTOR_HUBSPOT_API_VERSION": "v3"}))
        self.assertNotIn("pat-na1-x", repr(install))

    def test_credentials_and_the_one_portal_container(self):
        connector = _connector(FakeHubSpot())
        self.assertEqual(connector.check_credentials(INSTALL), InstallInfo("HubSpot portal 24681357", "https://app.hubspot.com"))
        self.assertEqual(
            connector.containers(INSTALL),
            [Container("hubspot:default:container:24681357", "HubSpot portal 24681357", "24681357", "https://app.hubspot.com/contacts/24681357/companies", "portal")],
        )

    def test_no_portal_id_is_an_error_not_a_new_container(self):
        fake = FakeHubSpot()
        fake.details = {}
        with self.assertRaises(hubspot.ConnectorError):
            _connector(fake).containers(INSTALL)


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeHubSpot()
        self.connector = _connector(self.fake)
        self.container = _container(self.connector)

    def test_the_first_page_is_every_account_newest_first_with_all_its_edges(self):
        page = self.connector.fetch(INSTALL, self.container, None)
        self.assertEqual([item.name for item in page.items], ["Larkspur Freightways", "Tidewhistle Robotics", "Saltwick Outfitters", "Bramblecote Analytics", "Hollowpine Health"])
        self.assertEqual({item.kind for item in page.items}, {"account"})
        self.assertEqual({item.container for item in page.items}, {"hubspot:default:container:24681357"})
        edges = {(edge.rel, edge.from_id.rsplit(":", 1)[1], edge.to_id.rsplit(":", 1)[1], edge.props.get("stage")) for edge in page.edges}
        self.assertEqual(
            edges,
            {
                ("OWNS", "102", "9001", None), ("WORKED_ON", "102", "9001", "Closed Won"), ("WORKED_ON", "101", "9001", "Contract Sent"),
                ("OWNS", "101", "9002", None), ("WORKED_ON", "101", "9002", "Qualified To Buy"),
                # 7004 (Noor's, won in 2024) is outside the year; 7005's owner has been deactivated.
                ("OWNS", "102", "9003", None),
                ("OWNS", "104", "9004", None), ("WORKED_ON", "104", "9004", "Closed Lost"),
            },
        )
        self.assertEqual([(user.external_id, user.name, user.email) for user in page.users], [
            ("102", "Dev Patel", "dev@quillfern.example.com"),
            ("101", "Priya Shah", "priya@quillfern.example.com"),
            ("104", "Riley Chen", "riley@partner.example.org"),
        ])
        self.assertEqual((page.has_more, page.deleted_ids, page.retry_after), (False, [], None))
        self.assertEqual(_cursor(page), {"mode": "poll", "phase": "companies", "since": SERVER_MS - hubspot.OVERLAP_MS})

    def test_the_backfill_asks_only_for_accounts_newest_first(self):
        self.connector.fetch(INSTALL, self.container, None)
        body = self.fake.searches("companies")[0]
        self.assertEqual(body["sorts"], [{"propertyName": "hs_lastmodifieddate", "direction": "DESCENDING"}])
        self.assertEqual(body["filterGroups"], hubspot.mapping.scope_filters())
        self.assertEqual((body["properties"], body["limit"]), (hubspot.mapping.COMPANY_PROPERTIES, 100))
        self.assertNotIn("after", body)
        self.assertEqual([call for call in self.fake.calls if call[0] == "owners"], [("owners", False)])

    def test_pages_follow_after_and_stop_at_the_cap(self):
        with mock.patch.object(hubspot, "PAGE_SIZE", 2):
            first = self.connector.fetch(INSTALL, self.container, None)
            self.assertEqual(([item.external_id for item in first.items], first.has_more), (["company:9001", "company:9002"], True))
            self.assertEqual(_cursor(first), {"mode": "backfill", "after": "2", "start": SERVER_MS, "count": 2})
            second = self.connector.fetch(INSTALL, self.container, first.next_cursor)
            self.assertEqual([item.external_id for item in second.items], ["company:9003", "company:9004"])
            self.assertEqual(self.fake.searches("companies")[-1]["after"], "2")
            third = self.connector.fetch(INSTALL, self.container, second.next_cursor)
            self.assertEqual(([item.external_id for item in third.items], third.has_more), (["company:9005"], False))
            self.assertEqual(_cursor(third)["mode"], "poll")
            with mock.patch.object(hubspot, "MAX_COMPANIES", 3):
                capped = self.connector.fetch(INSTALL, self.container, first.next_cursor)
        self.assertEqual(([item.external_id for item in capped.items], capped.has_more), (["company:9003"], False))
        self.assertEqual(_cursor(capped), {"mode": "poll", "phase": "companies", "since": SERVER_MS - hubspot.OVERLAP_MS})

    def test_the_people_setting_gives_an_owner_the_email_the_admin_names(self):
        install = Install("hubspot", config={"token": "pat-test", "people": "104=noor@quillfern.example.com,bad"})
        page = self.connector.fetch(install, self.container, None)
        self.assertEqual({user.external_id: user.email for user in page.users}["104"], "noor@quillfern.example.com")
        self.assertEqual({user.external_id: user.email for user in page.users}["102"], "dev@quillfern.example.com")

    def test_an_unreadable_cursor_starts_over(self):
        self.assertEqual(len(self.connector.fetch(INSTALL, self.container, "not json").items), 5)


class PollTest(unittest.TestCase):
    SINCE = SERVER_MS - 24 * 3600 * 1000  # 2026-10-17

    def setUp(self):
        self.fake = FakeHubSpot()
        self.connector = _connector(self.fake)
        self.container = _container(self.connector)
        self.cursor = json.dumps({"mode": "poll", "phase": "companies", "since": self.SINCE})

    def test_a_poll_sends_accounts_changed_since_then_deals_changed_since(self):
        companies = self.connector.fetch(INSTALL, self.container, self.cursor)
        # Nothing in scope changed on the 17th; Mossgarth changed, but it is a lead with no deal.
        self.assertEqual((companies.items, companies.has_more), ([], True))
        # Every active owner comes once a cycle, so a Person who arrived since is linked now.
        self.assertEqual([user.external_id for user in companies.users], ["101", "102", "103", "104"])
        self.assertEqual(_cursor(companies), {"mode": "poll", "phase": "deals", "since": self.SINCE, "next": SERVER_MS - hubspot.OVERLAP_MS})
        body = self.fake.searches("companies")[-1]
        self.assertEqual(body["sorts"][0]["direction"], "ASCENDING")
        self.assertEqual(body["filterGroups"], hubspot.mapping.scope_filters(self.SINCE))

        deals = self.connector.fetch(INSTALL, self.container, companies.next_cursor)
        self.assertEqual((deals.items, deals.has_more), ([], False))
        self.assertEqual(_cursor(deals), {"mode": "poll", "phase": "companies", "since": SERVER_MS - hubspot.OVERLAP_MS})
        self.assertEqual(self.fake.searches("deals")[-1]["filterGroups"], [{"filters": [{"propertyName": "hs_lastmodifieddate", "operator": "GTE", "value": str(self.SINCE)}]}])

    def test_a_company_that_changed_is_sent_with_every_edge_it_has(self):
        self.fake.companies["9001"]["properties"].update(hubspot_owner_id="101", hs_lastmodifieddate="2026-10-17T12:00:00.000Z")
        page = self.connector.fetch(INSTALL, self.container, self.cursor)
        self.assertEqual([item.id for item in page.items], [LARKSPUR])
        self.assertEqual(
            sorted((edge.rel, edge.from_id) for edge in page.edges),
            [("OWNS", "hubspot:default:user:101"), ("WORKED_ON", "hubspot:default:user:101"), ("WORKED_ON", "hubspot:default:user:102")],
        )

    def test_a_deal_that_changed_resends_its_company(self):
        self.fake.deals["7006"]["properties"].update(hubspot_owner_id="103", hs_lastmodifieddate="2026-10-17T12:00:00.000Z")
        cursor = json.dumps({"mode": "poll", "phase": "deals", "since": self.SINCE, "next": SERVER_MS})
        page = self.connector.fetch(INSTALL, self.container, cursor)
        self.assertEqual([item.name for item in page.items], ["Bramblecote Analytics"])
        self.assertEqual(sorted((edge.rel, edge.from_id) for edge in page.edges), [("OWNS", "hubspot:default:user:104"), ("WORKED_ON", "hubspot:default:user:103")])
        self.assertEqual([user.external_id for user in page.users], ["104", "103"])
        self.assertEqual(_cursor(page), {"mode": "poll", "phase": "companies", "since": SERVER_MS})

    def test_the_window_never_moves_back(self):
        cursor = json.dumps({"mode": "poll", "phase": "deals", "since": self.SINCE, "next": self.SINCE - 1})
        self.assertEqual(_cursor(self.connector.fetch(INSTALL, self.container, cursor))["since"], self.SINCE)


class DirectoryTest(unittest.TestCase):
    def test_owners_and_stages_are_reused_for_ten_minutes(self):
        fake, clock = FakeHubSpot(), Clock()
        connector = _connector(fake, clock)
        container = _container(connector)
        connector.fetch(INSTALL, container, None)
        connector.fetch(INSTALL, container, None)
        self.assertEqual(sum(call[0] == "owners" for call in fake.calls), 1)
        clock.now += hubspot.DIRECTORY_SECONDS + 1
        connector.fetch(INSTALL, container, None)
        self.assertEqual(sum(call[0] == "owners" for call in fake.calls), 2)

    def test_without_stage_labels_the_stage_ids_still_read(self):
        fake = FakeHubSpot()
        fake.deal_stages = mock.Mock(side_effect=HttpError(403, "missing scope"))
        connector = _connector(fake)
        page = connector.fetch(INSTALL, _container(connector), None)
        self.assertIn("contractsent", {edge.props.get("stage") for edge in page.edges})

    def test_a_rejected_token_is_raised_as_is(self):
        fake = FakeHubSpot()
        fake.search = mock.Mock(side_effect=CredentialsRejected("POST /crm/objects/2026-09/companies/search answered 401"))
        connector = _connector(fake)
        with self.assertRaises(CredentialsRejected):
            connector.fetch(INSTALL, _container(connector), None)


if __name__ == "__main__":
    unittest.main()
