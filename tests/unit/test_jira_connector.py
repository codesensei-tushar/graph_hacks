"""JiraConnector without a graph: the allow-list, the backfill and poll cursors, and what each page carries."""

import json
import unittest
from datetime import timedelta
from unittest import mock

from tests.fixtures.jira import CONFIG, NOW, FakeJiraClient, load

import src.connectors.jira as jira  # noqa: E402
from src.connectors import registry  # noqa: E402
from src.connectors.base import CredentialsRejected, Install  # noqa: E402


def _connector(fake: FakeJiraClient, now=NOW) -> jira.JiraConnector:
    connector = jira.JiraConnector()
    connector.client_for = lambda _install: fake
    connector.now = lambda: now
    return connector


def _drain(connector, install, container, cursor=None, limit=20):
    """Fetch until the connector stops asking for more. Returns the pages and the last cursor."""
    pages = []
    for _ in range(limit):
        page = connector.fetch(install, container, cursor)
        pages.append(page)
        cursor = page.next_cursor
        if not page.has_more:
            return pages, cursor
    raise AssertionError("fetch never caught up")


class JiraConnectorTest(unittest.TestCase):
    def setUp(self):
        self.install = Install(source="jira", config=CONFIG)
        self.fake = FakeJiraClient()
        self.connector = _connector(self.fake)
        self.pay, self.plat = self.connector.containers(self.install)

    def test_registered_and_configured_from_env(self):
        self.assertIsInstance(registry.get("jira"), jira.JiraConnector)
        env = {f"CONNECTOR_JIRA_{key.upper()}": value for key, value in CONFIG.items()}
        install = registry.install_from_env(registry.get("jira"), env)
        self.assertEqual((install.key, install.get_list("projects")), ("jira:default", ["PAY", "PLAT"]))
        self.assertIsNone(registry.install_from_env(registry.get("jira"), {**env, "CONNECTOR_JIRA_PROJECTS": ""}))
        self.assertNotIn(CONFIG["api_token"], repr(install))

    def test_containers_are_only_the_allowlisted_projects(self):
        self.assertEqual([(c.name, c.external_id) for c in (self.pay, self.plat)], [("Quillfern Payments", "10000"), ("Quillfern Platform", "10001")])
        lower = Install(source="jira", config={**CONFIG, "projects": "pay, NOPE"})
        self.assertEqual([c.external_id for c in self.connector.containers(lower)], ["10000"])

    def test_check_credentials_names_the_account_and_site(self):
        info = self.connector.check_credentials(self.install)
        self.assertEqual((info.account, info.site), ("Maya Ortiz", "quillfern.atlassian.net"))
        self.fake.rejected = True
        with self.assertRaises(CredentialsRejected):
            self.connector.check_credentials(self.install)

    def test_backfill_reads_ninety_days_newest_first_then_polls_from_the_newest(self):
        self.fake.page_size = 2
        pages, cursor = _drain(self.connector, self.install, self.pay)
        self.assertIn("updated >= -90d ORDER BY updated DESC", self.fake.queries[0][0])
        self.assertEqual([q[1] for q in self.fake.queries], [None, "2", "4"])
        names = [item.name for page in pages for item in page.items]
        # PAY-4 has a security level and PAY-6 is older than 90 days; HR and PLAT issues are other projects.
        self.assertEqual(names, ["PAY-1", "PAY-2", "PAY-3", "PAY-5"])
        self.assertEqual([deleted for page in pages for deleted in page.deleted_ids], ["jira:default:10104"])
        self.assertEqual(json.loads(cursor), {"since": "2026-10-18T03:30:00.000Z"})

    def test_backfill_stops_at_the_cap(self):
        self.fake.page_size = 2
        with mock.patch.object(jira, "MAX_ISSUES_PER_PROJECT", 3):
            pages, cursor = _drain(self.connector, self.install, self.pay)
        self.assertEqual(sum(len(page.items) for page in pages), 3)
        self.assertEqual(self.fake.queries[-1][2], 1)
        self.assertIn("since", json.loads(cursor))

    def test_poll_asks_for_what_changed_since_the_cursor_oldest_first(self):
        later = NOW + timedelta(minutes=10)
        connector = _connector(self.fake, now=later)
        self.fake.now = later
        self.fake.issues.append(_edited(self.fake.issues, "PAY-2", "2026-10-18T06:05:00.000+0000"))
        page = connector.fetch(self.install, self.pay, json.dumps({"since": "2026-10-18T06:00:00.000Z"}))
        # Ten minutes since the cursor, plus five of overlap.
        self.assertIn("updated >= -15m ORDER BY updated ASC", self.fake.queries[-1][0])
        self.assertEqual([item.name for item in page.items], ["PAY-2"])
        self.assertFalse(page.has_more)
        self.assertEqual(json.loads(page.next_cursor), {"since": "2026-10-18T06:05:00.000Z"})

    def test_poll_with_nothing_new_keeps_the_cursor(self):
        page = self.connector.fetch(self.install, self.pay, json.dumps({"since": "2026-10-18T05:59:00.000Z"}))
        self.assertEqual((page.items, page.has_more), ([], False))
        self.assertEqual(json.loads(page.next_cursor), {"since": "2026-10-18T05:59:00.000Z"})

    def test_a_page_of_one_minute_follows_the_token_instead_of_looping(self):
        same = "2026-10-18T05:58:00.000+0000"
        self.fake.issues = [_edited(load("issues.json"), key, same) for key in ("PAY-1", "PAY-2", "PAY-3")]
        self.fake.page_size = 2
        since = "2026-10-18T05:58:00.000Z"
        first = self.connector.fetch(self.install, self.pay, json.dumps({"since": since}))
        state = json.loads(first.next_cursor)
        self.assertTrue(first.has_more)
        self.assertEqual((state["since"], state["token"]), (since, "2"))
        second = self.connector.fetch(self.install, self.pay, first.next_cursor)
        self.assertEqual([item.name for item in second.items], ["PAY-3"])
        self.assertEqual(self.fake.queries[-1][:2], (state["jql"], "2"))
        self.assertEqual(json.loads(second.next_cursor), {"since": since})

    def test_an_unreadable_cursor_starts_the_backfill_over(self):
        self.connector.fetch(self.install, self.pay, "not json")
        self.assertIn("-90d", self.fake.queries[-1][0])

    def test_a_page_carries_each_user_once_and_every_edge_of_its_items(self):
        page = self.connector.fetch(self.install, self.pay, None)
        self.assertEqual(sorted(user.name for user in page.users), ["Arjun Mehta", "Automation for Jira", "Maya Ortiz", "Robin Contractor"])
        edges = {(edge.rel, edge.from_id, edge.to_id) for edge in page.edges}
        self.assertEqual(len(edges), len(page.edges))
        self.assertIn(("BLOCKS", "jira:default:10103", "jira:default:10102"), edges)
        # The other side of a cross-project link comes too: it is only added when PLAT-7 is not in the page.
        self.assertIn(("BLOCKS", "jira:default:10207", "jira:default:10103"), edges)

    def test_people_mapping_fills_hidden_emails(self):
        install = Install(source="jira", config={**CONFIG, "people": "712020:7f3e-arjun=arjun@quillfern.example.com, broken"})
        page = self.connector.fetch(install, self.pay, None)
        self.assertEqual({user.name: user.email for user in page.users}["Arjun Mehta"], "arjun@quillfern.example.com")
        self.assertEqual(jira.people(install), {"712020:7f3e-arjun": "arjun@quillfern.example.com"})


def _edited(issues: list[dict], key: str, updated: str) -> dict:
    issue = json.loads(json.dumps(next(issue for issue in issues if issue["key"] == key)))
    issue["fields"]["updated"] = updated
    return issue


if __name__ == "__main__":
    unittest.main()
