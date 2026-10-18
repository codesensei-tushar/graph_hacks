"""The Jira REST client over JsonClient: the endpoints it calls, paging, and that the token never shows."""

import base64
import io
import json
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import tests  # noqa: F401  pins the test settings before src loads

from src.connectors import http  # noqa: E402
from src.connectors.base import CredentialsRejected  # noqa: E402
from src.connectors.jira.client import JiraClient, site_problems  # noqa: E402

SITE = "https://quillfern.atlassian.net"
TOKEN = "fake-token-not-real"


class _Response(io.BytesIO):
    def __init__(self, body: object):
        super().__init__(json.dumps(body).encode())
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class JiraClientTest(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.answers = []
        patcher = mock.patch.object(http, "_urlopen", side_effect=self._open)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = JiraClient(SITE, "maya@quillfern.example.com", TOKEN)

    def _open(self, request, timeout=None):
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return _Response(answer)

    def test_search_follows_next_page_token_up_to_the_limit(self):
        self.answers = [
            {"issues": [{"id": "1"}, {"id": "2"}], "nextPageToken": "t2", "isLast": False},
            {"issues": [{"id": "3"}, {"id": "4"}], "nextPageToken": "t3", "isLast": False},
        ]
        found = list(self.client.search("project = 10000 ORDER BY updated DESC", limit=3))
        self.assertEqual([issue["id"] for issue in found], ["1", "2", "3"])
        first, second = (json.loads(request.data) for request in self.requests)
        self.assertEqual(self.requests[0].full_url, f"{SITE}/rest/api/3/search/jql")
        self.assertEqual(self.requests[0].get_method(), "POST")
        self.assertNotIn("nextPageToken", first)
        self.assertEqual(second["nextPageToken"], "t2")
        self.assertEqual(second["maxResults"], 1)
        self.assertIn("security", first["fields"])

    def test_basic_auth_with_email_and_token(self):
        self.answers = [{"displayName": "Maya Ortiz"}]
        self.assertEqual(self.client.myself()["displayName"], "Maya Ortiz")
        expected = "Basic " + base64.b64encode(f"maya@quillfern.example.com:{TOKEN}".encode()).decode()
        self.assertEqual(self.requests[0].get_header("Authorization"), expected)
        self.assertEqual(self.requests[0].full_url, f"{SITE}/rest/api/3/myself")

    def test_projects_asks_only_for_allowlisted_keys(self):
        self.answers = [{"values": [{"id": "10000", "key": "PAY"}], "isLast": True}]
        self.assertEqual([p["key"] for p in self.client.projects(["PAY", "not a key", "PLAT"])], ["PAY"])
        self.assertIn("keys=PAY&keys=PLAT", self.requests[0].full_url)
        self.assertNotIn("not", self.requests[0].full_url)
        self.assertEqual(self.client.projects([]), [])

    def test_all_projects_pages_by_start_at_up_to_the_limit(self):
        self.answers = [
            {"values": [{"id": "1", "key": "AA"}, {"id": "2", "key": "BB"}], "isLast": False},
            {"values": [{"id": "3", "key": "CC"}, {"id": "4", "key": "DD"}], "isLast": False},
        ]
        self.assertEqual([p["key"] for p in self.client.all_projects(limit=3)], ["AA", "BB", "CC"])
        first, second = (urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query) for request in self.requests)
        self.assertNotIn("keys", first)
        self.assertEqual((first["startAt"], first["maxResults"], first["orderBy"]), (["0"], ["3"], ["key"]))
        self.assertEqual((second["startAt"], second["maxResults"]), (["2"], ["1"]))

    def test_all_projects_stops_on_the_last_page_or_an_empty_one(self):
        self.answers = [{"values": [{"id": "1", "key": "AA"}], "isLast": True}]
        self.assertEqual(len(self.client.all_projects()), 1)
        self.answers = [{"values": [{"id": "1", "key": "AA"}], "isLast": False}, {"values": [], "isLast": False}]
        self.assertEqual(len(self.client.all_projects()), 1)
        self.assertEqual(len(self.requests), 3)

    def test_missing_project_is_none(self):
        self.answers = [urllib.error.HTTPError(f"{SITE}/rest/api/3/project/NOPE", 404, "Not Found", {}, io.BytesIO(b'{"errorMessages":["No project"]}'))]
        self.assertIsNone(self.client.project("NOPE"))

    def test_rejected_token_never_reaches_the_message_or_repr(self):
        self.answers = [urllib.error.HTTPError(f"{SITE}/rest/api/3/myself", 401, "Unauthorized", {}, io.BytesIO(b""))]
        with self.assertRaises(CredentialsRejected) as caught:
            self.client.myself()
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertNotIn(TOKEN, repr(self.client))
        self.assertNotIn(TOKEN, repr(self.client.http))

    def test_base_url_must_be_an_https_origin(self):
        self.assertEqual(site_problems(SITE), [])
        for bad in ("http://quillfern.atlassian.net", "https://quillfern.atlassian.net/jira", "", "quillfern.atlassian.net"):
            self.assertTrue(site_problems(bad), bad)
        with self.assertRaises(ValueError):
            JiraClient("http://quillfern.atlassian.net", "a@b.c", TOKEN)


if __name__ == "__main__":
    unittest.main()
