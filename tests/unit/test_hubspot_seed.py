"""scripts/seed_hubspot.py against a fake account: it only creates, only what it marks, and only once."""

import unittest

from scripts import seed_hubspot
from src.connectors.hubspot.client import HubSpotClient
from tests.fixtures.hubspot.fake_hubspot import load

OWNERS = load("owners.json")["results"] + load("owners_page2.json")["results"]
STRANGER = {"id": "1", "properties": {"name": "Tidewhistle Robotics", "domain": "tidewhistle-robotics.example", "description": "a real customer"}}


class FakeAccount:
    """Just enough of HubSpot's API for the seed: owners, the pipeline, listing and creating records."""

    def __init__(self, owners=OWNERS, companies=()):
        self.owners = owners
        self.records = {"companies": list(companies), "deals": []}
        self.calls: list[tuple[str, str]] = []

    def get(self, path, params=None):
        self.calls.append(("GET", path))
        if path == "/crm/owners/2026-09":
            return {"results": self.owners}
        if path == "/crm/pipelines/2026-09/deals":
            return {"results": [{"id": "default", "stages": [{"id": stage, "label": stage} for stage in seed_hubspot.STAGES]}]}
        return {"results": self.records[path.rsplit("/", 1)[1]]}

    def post(self, path, body=None, params=None):
        self.calls.append(("POST", path))
        kind = path.rsplit("/", 1)[1]
        record = {"id": str(100 + sum(len(records) for records in self.records.values())), "properties": dict(body["properties"]), "associations": body.get("associations")}
        self.records[kind].append(record)
        return record


def _run(account: FakeAccount, apply: bool) -> tuple[dict, list[str]]:
    lines: list[str] = []
    counts = seed_hubspot.run(HubSpotClient("pat-fake", http=account), apply, out=lines.append)
    return counts, lines


class SeedTest(unittest.TestCase):
    def test_a_dry_run_reads_and_writes_nothing(self):
        account = FakeAccount()
        counts, lines = _run(account, apply=False)
        self.assertEqual((counts["companies"], counts["deals"]), (20, 26))
        self.assertEqual({method for method, _ in account.calls}, {"GET"})
        self.assertIn("owner  Dev Patel: HubSpot user 102 has dev@quillfern.example.com; their accounts will link to Dev Patel", lines)
        self.assertEqual(lines[-1], "would create 20 companies and 26 deals; 0 already there; 0 skipped")

    def test_apply_creates_marked_records_once(self):
        account = FakeAccount()
        _run(account, apply=True)
        companies, deals = account.records["companies"], account.records["deals"]
        self.assertEqual((len(companies), len(deals)), (20, 26))
        self.assertEqual({method for method, _ in account.calls}, {"GET", "POST"})
        self.assertEqual({path for method, path in account.calls if method == "POST"}, {"/crm/objects/2026-09/companies", "/crm/objects/2026-09/deals"})
        for record in companies + deals:
            self.assertTrue(record["properties"]["description"].startswith(seed_hubspot.MARKER))
            self.assertNotIn("amount", record["properties"])
        self.assertTrue(all(company["properties"]["domain"].endswith(".example") for company in companies))
        larkspur = next(company for company in companies if company["properties"]["name"] == "Larkspur Freightways")
        self.assertEqual(larkspur["properties"]["hubspot_owner_id"], "102")
        upgrade = next(deal for deal in deals if deal["properties"]["dealname"] == "Larkspur Freightways: Fleet tier upgrade")
        self.assertEqual((upgrade["properties"]["hubspot_owner_id"], upgrade["properties"]["dealstage"]), ("101", "contractsent"))
        self.assertEqual(upgrade["associations"][0]["to"], {"id": larkspur["id"]})

        counts, lines = _run(account, apply=True)
        self.assertEqual((counts["companies"], counts["deals"], counts["existing"]), (0, 0, 46))
        self.assertEqual((len(account.records["companies"]), len(account.records["deals"])), (20, 26))

    def test_a_domain_it_did_not_make_is_skipped_with_its_deals(self):
        account = FakeAccount(companies=[STRANGER])
        counts, lines = _run(account, apply=True)
        self.assertIn("skip   Tidewhistle Robotics: tidewhistle-robotics.example is already in the account and was not made by this script", lines)
        self.assertEqual((counts["companies"], counts["deals"], counts["skipped"]), (19, 25, 2))
        self.assertEqual(account.records["companies"][0], STRANGER)
        self.assertFalse([deal for deal in account.records["deals"] if deal["properties"]["dealname"].startswith("Tidewhistle")])

    def test_without_the_team_emails_the_accounts_go_to_the_accounts_own_users(self):
        account = FakeAccount(owners=[{"id": "7", "email": "founder@startup.example.org", "firstName": "Ada", "lastName": "Lane", "type": "PERSON"}])
        _, lines = _run(account, apply=False)
        self.assertIn("owner  Dev Patel: no HubSpot user has dev@quillfern.example.com; their accounts go to user 7 (Ada Lane), who will not link to Dev Patel", lines)
        self.assertIn("create company Larkspur Freightways [customer] larkspur-freightways.example owner=Dev Patel (HubSpot user 7)", lines)


if __name__ == "__main__":
    unittest.main()
