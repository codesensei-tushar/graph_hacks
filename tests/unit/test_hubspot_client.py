"""The HubSpot client: which paths it calls on each API version, how it pages, and what it sends."""

import unittest

from src.connectors.hubspot.client import HubSpotClient
from tests.fixtures.hubspot.fake_hubspot import load


class FakeJson:
    """Answers each (method, path, after) with a fixture and records the call."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls: list[tuple] = []

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, params, body))
        after = (params or {}).get("after") or (body.get("after") if isinstance(body, dict) else None)
        answer = self.answers.get((method, path, after), self.answers.get((method, path)))
        return answer, {"date": "Sun, 18 Oct 2026 00:00:00 GMT"}

    def get(self, path, params=None):
        return self.request("GET", path, params=params)[0]

    def post(self, path, body=None, params=None):
        return self.request("POST", path, params=params, body=body)[0]


def _client(answers: dict, version: str = "2026-09") -> tuple[HubSpotClient, FakeJson]:
    http = FakeJson(answers)
    return HubSpotClient("pat-never-shown", version, http=http), http


class DatedPathsTest(unittest.TestCase):
    def test_account_owners_and_stages(self):
        client, http = _client(
            {
                ("GET", "/account-info/2026-09/details"): load("account_details.json"),
                ("GET", "/crm/owners/2026-09", None): load("owners.json"),
                ("GET", "/crm/owners/2026-09", "3"): load("owners_page2.json"),
                ("GET", "/crm/pipelines/2026-09/deals"): load("pipelines_deals.json"),
            }
        )
        self.assertEqual(client.account_details()["portalId"], 24681357)
        self.assertEqual([owner["id"] for owner in client.owners()], ["101", "102", "103", "104"])
        self.assertEqual(
            [call[2] for call in http.calls if call[1] == "/crm/owners/2026-09"],
            [{"limit": 100, "archived": "false"}, {"limit": 100, "archived": "false", "after": "3"}],
        )
        self.assertEqual(client.deal_stages()["closedwon"], "Closed Won")

    def test_search_returns_the_page_the_next_after_and_the_headers(self):
        page = load("companies_search.json") | {"paging": {"next": {"after": "6"}}}
        client, http = _client({("POST", "/crm/objects/2026-09/companies/search"): page})
        results, after, headers = client.search("companies", {"limit": 100})
        self.assertEqual((len(results), after, headers["date"]), (6, "6", "Sun, 18 Oct 2026 00:00:00 GMT"))
        self.assertEqual(http.calls[0][:2], ("POST", "/crm/objects/2026-09/companies/search"))
        _, last, _ = _client({("POST", "/crm/objects/2026-09/companies/search"): load("companies_search.json")})[0].search("companies", {})
        self.assertIsNone(last)

    def test_batch_reads_go_a_hundred_ids_at_a_time(self):
        client, http = _client({("POST", "/crm/objects/2026-09/deals/batch/read"): load("deals_batch_read.json")})
        ids = [str(number) for number in range(250)]
        client.batch_read("deals", ids, ["dealstage"])
        bodies = [call[3] for call in http.calls]
        self.assertEqual([len(body["inputs"]) for body in bodies], [100, 100, 50])
        self.assertEqual(bodies[0]["properties"], ["dealstage"])
        self.assertEqual(bodies[2]["inputs"][-1], {"id": "249"})

    def test_associations_map_each_record_to_its_linked_ids(self):
        client, http = _client({("POST", "/crm/associations/2026-09/companies/deals/batch/read"): load("associations_companies_deals.json")})
        linked = client.associations("companies", "deals", ["9001", "9002", "9003", "9004"])
        self.assertEqual(linked, {"9001": ["7001", "7002"], "9002": ["7003"], "9003": ["7004", "7005"], "9004": ["7006"]})
        self.assertEqual(http.calls[0][3], {"inputs": [{"id": "9001"}, {"id": "9002"}, {"id": "9003"}, {"id": "9004"}]})


class PagingTest(unittest.TestCase):
    def test_owners_stop_when_the_paging_cursor_repeats(self):
        page = load("owners.json")  # always answers "after": "3"
        client, http = _client({("GET", "/crm/owners/2026-09"): page})
        self.assertEqual(len(client.owners()), 6)
        self.assertEqual(len(http.calls), 2)

    def test_associations_follow_each_records_own_paging(self):
        first = {"results": [{"from": {"id": "9001"}, "to": [{"toObjectId": 1}], "paging": {"next": {"after": "a1"}}}, {"from": {"id": "9002"}, "to": [{"toObjectId": 3}]}]}
        second = {"results": [{"from": {"id": "9001"}, "to": [{"toObjectId": 2}]}]}
        path = "/crm/associations/2026-09/companies/deals/batch/read"
        http = FakeJson({})
        answers = iter([first, second])
        http.request = lambda method, p, params=None, body=None: (http.calls.append((method, p, params, body)) or next(answers), {})
        client = HubSpotClient("pat-fake", http=http)
        self.assertEqual(client.associations("companies", "deals", ["9001", "9002"]), {"9001": ["1", "2"], "9002": ["3"]})
        self.assertEqual([call[3] for call in http.calls], [{"inputs": [{"id": "9001"}, {"id": "9002"}]}, {"inputs": [{"id": "9001", "after": "a1"}]}])
        self.assertEqual({call[1] for call in http.calls}, {path})


class LegacyPathsTest(unittest.TestCase):
    def test_v3_uses_the_numbered_paths(self):
        client, http = _client({}, version="v3")
        client.account_details()
        client.owners(archived=True)
        client.deal_stages()
        client.search("deals", {})
        client.batch_read("companies", ["1"], [])
        client.associations("deals", "companies", ["1"])
        self.assertEqual(
            [call[1] for call in http.calls],
            [
                "/account-info/v3/details", "/crm/v3/owners", "/crm/v3/pipelines/deals", "/crm/v3/objects/deals/search",
                "/crm/v3/objects/companies/batch/read", "/crm/v4/associations/deals/companies/batch/read",
            ],
        )
        self.assertEqual(http.calls[1][2]["archived"], "true")


class TokenTest(unittest.TestCase):
    def test_the_token_is_a_bearer_header_for_api_hubapi_com_and_never_in_repr(self):
        client = HubSpotClient("pat-na1-secret")
        self.assertEqual(client.http.base_url, "https://api.hubapi.com")
        self.assertEqual(client.http._headers["Authorization"], "Bearer pat-na1-secret")
        self.assertNotIn("secret", repr(client))
        self.assertNotIn("secret", repr(client.http))


if __name__ == "__main__":
    unittest.main()
