"""HubSpot records to connector records: what becomes an account, its state, and its edges."""

import unittest

from src.connectors.base import Edge, ExternalUser, Install
from src.connectors.graph import RESERVED_PROPS
from src.connectors.hubspot import mapping
from tests.fixtures.hubspot.fake_hubspot import SERVER_MS, load

INSTALL = Install("hubspot", config={"token": "pat-test"})
CONTAINER = INSTALL.container_id("24681357")
NOW = SERVER_MS / 1000
COMPANIES = {record["id"]: record for record in load("companies_search.json")["results"]}
DEALS = {record["id"]: record for record in load("deals_batch_read.json")["results"]}
STAGES = {"closedwon": "Closed Won", "contractsent": "Contract Sent"}


class ScopeAndStateTest(unittest.TestCase):
    def test_customers_and_companies_with_deals_are_accounts_and_leads_are_not(self):
        self.assertEqual([key for key, company in COMPANIES.items() if mapping.in_scope(company)], ["9001", "9002", "9003", "9004", "9005"])
        self.assertFalse(mapping.in_scope(COMPANIES["9100"]))

    def test_open_for_a_customer_or_an_open_deal_closed_for_churned_or_lost_only(self):
        states = {key: mapping.account_state(COMPANIES[key]) for key in ("9001", "9002", "9003", "9004", "9005")}
        # 9001 customer; 9002 an opportunity with an open deal; 9003 moved to "other" (churned);
        # 9004 a lead whose only deal was lost; 9005 a customer with no deal at all.
        self.assertEqual(states, {"9001": "open", "9002": "open", "9003": "closed", "9004": "closed", "9005": "open"})

    def test_a_deal_counts_while_open_or_when_created_or_closed_in_the_last_year(self):
        counted = [key for key, deal in DEALS.items() if mapping.deal_counts(deal, NOW)]
        # 7004 was created and won in 2024.
        self.assertEqual(counted, ["7001", "7002", "7003", "7005", "7006"])
        self.assertTrue(mapping.deal_counts(DEALS["7004"], NOW, window_days=1000))

    def test_parse_time_reads_iso_and_epoch_milliseconds(self):
        self.assertEqual(mapping.parse_time("2026-10-18T00:00:00.000Z"), NOW)
        self.assertEqual(mapping.parse_time(str(SERVER_MS)), NOW)
        self.assertEqual(mapping.parse_time("2026-10-18"), NOW)
        self.assertIsNone(mapping.parse_time(""))
        self.assertIsNone(mapping.parse_time("soon"))


class ItemTest(unittest.TestCase):
    def test_a_company_is_an_account_named_as_hubspot_names_it(self):
        item = mapping.company_item(INSTALL, CONTAINER, COMPANIES["9001"], "24681357", "app.hubspot.com")
        self.assertEqual(
            (item.label, item.kind, item.id, item.external_id, item.container, item.name, item.title),
            ("Service", "account", "hubspot:default:company:9001", "company:9001", "hubspot:default:container:24681357", "Larkspur Freightways", ""),
        )
        self.assertEqual(item.url, "https://app.hubspot.com/contacts/24681357/record/0-2/9001")
        self.assertEqual((item.state, item.status, item.updated_at), ("open", "customer", "2026-10-15T10:00:00.000Z"))
        self.assertEqual(item.summary, "HubSpot company, lifecycle stage customer; 1 open deal; logistics and supply chain; larkspur-freightways.example.")
        self.assertEqual(dict(item.props), {"open_deals": 1, "domain": "larkspur-freightways.example", "industry": "logistics and supply chain"})
        self.assertEqual(item.about, ())

    def test_extra_properties_never_take_a_name_the_graph_writer_reserves(self):
        for company in COMPANIES.values():
            self.assertFalse(set(mapping.company_item(INSTALL, CONTAINER, company).props) & RESERVED_PROPS)

    def test_a_company_without_a_name_falls_back_to_its_domain_then_its_id(self):
        self.assertEqual(mapping.company_name({"id": "1", "properties": {"name": "  Fernhollow   Bakehouse "}}), "Fernhollow Bakehouse")
        self.assertEqual(mapping.company_name({"id": "1", "properties": {"name": "", "domain": "Fernhollow.example"}}), "fernhollow.example")
        self.assertEqual(mapping.company_name({"id": "1", "properties": {}}), "HubSpot company 1")

    def test_without_portal_details_there_is_no_url(self):
        self.assertEqual(mapping.company_item(INSTALL, CONTAINER, COMPANIES["9001"]).url, "")

    def test_a_summary_with_no_stage_or_industry(self):
        company = {"id": "1", "properties": {"hs_num_open_deals": "2"}}
        self.assertEqual(mapping.summary(company), "HubSpot company; 2 open deals.")


class EdgesAndUsersTest(unittest.TestCase):
    def test_the_owner_owns_and_each_deal_owner_worked_on_it_once_with_the_latest_stage(self):
        edges = mapping.company_edges(INSTALL, COMPANIES["9001"], [DEALS["7001"], DEALS["7002"]], STAGES)
        item = "hubspot:default:company:9001"
        self.assertEqual(
            [(edge.rel, edge.from_id, edge.to_id, dict(edge.props)) for edge in edges],
            [
                ("OWNS", "hubspot:default:user:102", item, {}),
                ("WORKED_ON", "hubspot:default:user:101", item, {"evidence": "deal", "stage": "Contract Sent"}),
                ("WORKED_ON", "hubspot:default:user:102", item, {"evidence": "deal", "stage": "Closed Won"}),
            ],
        )
        renewal = {"id": "7009", "properties": {"dealstage": "contractsent", "hubspot_owner_id": "102", "createdate": "2026-10-01T00:00:00Z"}}
        latest = mapping.company_edges(INSTALL, COMPANIES["9001"], [DEALS["7001"], renewal], STAGES)
        self.assertEqual([dict(edge.props) for edge in latest if edge.rel == "WORKED_ON"], [{"evidence": "deal", "stage": "Contract Sent"}])

    def test_no_owner_no_edge_and_an_unknown_stage_reads_as_its_id(self):
        self.assertEqual(mapping.company_edges(INSTALL, COMPANIES["9005"], []), [])
        edges = mapping.company_edges(INSTALL, COMPANIES["9002"], [DEALS["7003"]], {})
        self.assertEqual(edges[-1], Edge("WORKED_ON", "hubspot:default:user:101", "hubspot:default:company:9002"))
        self.assertEqual(edges[-1].props["stage"], "qualifiedtobuy")

    def test_an_owner_is_an_external_user_with_a_lowercased_email(self):
        owners = {owner["id"]: owner for owner in load("owners.json")["results"] + load("owners_archived.json")["results"]}
        self.assertEqual(
            mapping.owner_user(INSTALL, owners["103"]),
            ExternalUser("hubspot:default:user:103", "hubspot", "103", "Noor Haddad", "noor@quillfern.example.com"),
        )
        # A user with no name reads as their email.
        self.assertEqual(mapping.owner_user(INSTALL, owners["105"]).name, "sam@quillfern.example.com")

    def test_only_active_owners_hold_an_edge(self):
        edges = mapping.company_edges(INSTALL, COMPANIES["9003"], [DEALS["7005"]], STAGES, active={"101", "102", "103", "104"})
        # Saltwick's lost renewal belongs to a user who has since been deactivated.
        self.assertEqual([(edge.rel, edge.from_id) for edge in edges], [("OWNS", "hubspot:default:user:102")])
        self.assertEqual(mapping.company_edges(INSTALL, COMPANIES["9003"], [], active=set()), [])

    def test_the_owners_a_page_names_in_first_seen_order(self):
        companies = [COMPANIES[key] for key in ("9001", "9002", "9005")]
        self.assertEqual(mapping.owner_ids(companies, [DEALS["7002"], DEALS["7005"]]), ["102", "101", "105"])


class WhatIsNeverReadTest(unittest.TestCase):
    def test_no_amount_contact_engagement_or_free_text_property_is_ever_asked_for(self):
        forbidden = {"amount", "amount_in_home_currency", "hs_acv", "hs_arr", "hs_mrr", "hs_tcv", "hs_forecast_amount", "hs_forecast_probability",
                     "description", "dealname", "notes_last_contacted", "phone", "address", "annualrevenue", "num_associated_contacts"}
        self.assertFalse(forbidden & set(mapping.COMPANY_PROPERTIES))
        self.assertFalse(forbidden & set(mapping.DEAL_PROPERTIES))

    def test_the_search_filters_are_customers_or_any_deal_changed_since(self):
        self.assertEqual(
            mapping.scope_filters(5),
            [
                {"filters": [{"propertyName": "lifecyclestage", "operator": "IN", "values": ["customer", "evangelist"]},
                             {"propertyName": "hs_lastmodifieddate", "operator": "GTE", "value": "5"}]},
                {"filters": [{"propertyName": "num_associated_deals", "operator": "GT", "value": "0"},
                             {"propertyName": "hs_lastmodifieddate", "operator": "GTE", "value": "5"}]},
            ],
        )
        self.assertEqual([len(group["filters"]) for group in mapping.scope_filters()], [1, 1])


if __name__ == "__main__":
    unittest.main()
