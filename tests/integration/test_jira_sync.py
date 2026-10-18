"""Jira into the graph end to end: real FalkorDB, Quillfern seed, fake Jira, Agent 2's graph writer.

Syncs the two allow-listed projects (PLAT first, so a link from PLAT to a PAY ticket that is
not there yet has to be picked up from the PAY side), then checks the tickets, who they are
about, the BLOCKS links, how accounts link to people, and what an asker in Slack and on the
public page each read.
"""

import unittest
from unittest import mock

from tests.fixtures.jira import CONFIG, NOW, FakeJiraClient

import src.connectors.jira as jira  # noqa: E402
from src.connectors import graph as connector_graph  # noqa: E402
from src.connectors import registry, sync  # noqa: E402
from src.connectors.base import ORG_INTERNAL, Install  # noqa: E402
from src.db.falkordb_client import connect  # noqa: E402
from src.domain.knowledge import ask_service  # noqa: E402
from src.seed import quillfern  # noqa: E402
from src.seed.seed import reset_and_seed  # noqa: E402

SLACK = ["org_shared", ORG_INTERNAL]
PUBLIC = ["org_shared"]


def _sync(connector, install, order=("PLAT", "PAY")) -> list[dict]:
    containers = {c.url.rsplit("/", 1)[-1]: c for c in connector.containers(install)}
    connector_graph.write_containers(install, list(containers.values()))
    reports = []
    for key in order:
        if key not in containers:
            continue
        cursor = None
        while True:
            page = connector.fetch(install, containers[key], cursor)
            reports.append(connector_graph.write_page(install, page, jira.MAX_ISSUES_PER_PROJECT))
            cursor = page.next_cursor
            if not page.has_more:
                break
    return reports


def _rows(query: str, params: dict | None = None) -> list:
    _, graph = connect()
    return graph.query(query, params or {}).result_set


class JiraSyncTest(unittest.TestCase):
    def setUp(self):
        reset_and_seed(quillfern)
        connector_graph.ensure_indexes()
        self.install = Install(source="jira", config=CONFIG)
        self.fake = FakeJiraClient(page_size=2)
        self.connector = jira.JiraConnector()
        self.connector.client_for = lambda _install: self.fake
        _sync(self.connector, self.install)

    def test_tickets_land_in_their_project_as_org_internal(self):
        rows = _rows(
            """
            MATCH (t:Ticket {source: 'jira'})-[:IN_CONTAINER]->(c:Container)
            RETURN t.name, t.title, t.state, t.status, t.scope, c.name, t.issue_type, t.parent_key
            ORDER BY t.name
            """
        )
        self.assertEqual([row[0] for row in rows], ["PAY-1", "PAY-2", "PAY-3", "PAY-5", "PLAT-7", "PLAT-8"])
        pay2 = next(row for row in rows if row[0] == "PAY-2")
        self.assertEqual(pay2[2:], ["closed", "Done", ORDER_SCOPE, "Quillfern Payments", "Story", "PAY-1"])
        # The security-level issue and the project outside the allow-list never arrive.
        self.assertEqual(_rows("MATCH (t:Ticket) WHERE t.name IN ['PAY-4', 'HR-1'] RETURN t.name"), [])

    def test_components_and_titles_find_the_service(self):
        about = dict(_rows("MATCH (t:Ticket {source: 'jira'})-[:ABOUT]->(s:Service) RETURN t.name, s.id"))
        self.assertEqual(about["PAY-1"], "billing-service")
        self.assertEqual(about["PLAT-7"], "postgres")
        self.assertEqual(about["PLAT-8"], "notification-service")
        # No component: the title's "billing" is the best guess.
        self.assertEqual(about["PAY-5"], "billing-service")

    def test_blocks_links_survive_the_order_projects_sync_in(self):
        blocks = _rows("MATCH (a:Ticket)-[:BLOCKS]->(b:Ticket) RETURN a.name, b.name ORDER BY a.name")
        self.assertEqual(blocks, [["PAY-3", "PAY-2"], ["PLAT-7", "PAY-3"]])
        self.assertEqual(_rows("MATCH (:Ticket)-[r:DEPENDS_ON]->() RETURN count(r)"), [[0]])

    def test_accounts_link_to_a_person_only_by_a_unique_email(self):
        linked = _rows("MATCH (u:ExternalUser)-[:IS]->(p:Person) RETURN u.name, p.id ORDER BY u.name")
        # Maya's email is visible and matches the seeded Person; Arjun's is hidden; Robin's matches nobody.
        self.assertEqual(linked, [["Maya Ortiz", "maya"]])
        install = Install(source="jira", config={**CONFIG, "people": "712020:7f3e-arjun=arjun@quillfern.example.com"})
        _sync(self.connector, install)
        linked = _rows("MATCH (u:ExternalUser)-[:IS]->(p:Person) RETURN u.name, p.id ORDER BY u.name")
        self.assertEqual(linked, [["Arjun Mehta", "arjun"], ["Maya Ortiz", "maya"]])

    def test_a_second_sync_changes_nothing(self):
        before = _rows("MATCH (n) RETURN count(n)")[0][0], _rows("MATCH ()-[r]->() RETURN count(r)")[0][0]
        reports = _sync(self.connector, self.install)
        after = _rows("MATCH (n) RETURN count(n)")[0][0], _rows("MATCH ()-[r]->() RETURN count(r)")[0][0]
        self.assertEqual(before, after)
        self.assertEqual(sum(report["skipped"] for report in reports), 0)

    def test_an_issue_that_gains_a_security_level_is_removed(self):
        pay1 = next(issue for issue in self.fake.issues if issue["key"] == "PAY-1")
        pay1["fields"]["security"] = {"id": "10100", "name": "Finance only"}
        _sync(self.connector, self.install, order=("PAY",))
        self.assertEqual(_rows("MATCH (t:Ticket {name: 'PAY-1'}) RETURN t.id"), [])

    def test_a_project_dropped_from_the_allowlist_is_removed_with_its_tickets(self):
        _sync(self.connector, Install(source="jira", config={**CONFIG, "projects": "PAY"}), order=("PAY",))
        self.assertEqual(_rows("MATCH (t:Ticket) WHERE t.name STARTS WITH 'PLAT-' RETURN t.name"), [])
        self.assertEqual(_rows("MATCH (c:Container) RETURN c.name"), [["Quillfern Payments"]])

    def test_who_knows_reads_jira_in_slack_and_never_on_the_public_page(self):
        slack = ask_service.who_knows("who knows the billing service", SLACK)
        cited = [step.cite() for step in slack.path]
        self.assertIn("PAY-1 -[ABOUT]-> Billing Service", cited)
        self.assertIn("Arjun Mehta (Jira) -[ASSIGNED]-> PAY-1", cited)
        # Jira hides this account's email, so it stays an account: never linked to the seeded Person by its name,
        # and named with its tool so the answer does not fold it into the Person who owns the service.
        arjun = next(step for step in slack.path if step.rel == "ASSIGNED" and step.to_name == "PAY-1")
        self.assertEqual((arjun.from_name, arjun.from_label, arjun.from_id), ("Arjun Mehta (Jira)", "ExternalUser", "jira:default:user:712020:7f3e-arjun"))
        self.assertIn("Ask first: Arjun Mehta (owns it, wrote the Billing runbook)", slack.answer)
        self.assertIn("Arjun Mehta (Jira) (is assigned PAY-1 and PAY-2)", slack.answer)
        public = ask_service.who_knows("who knows the billing service", PUBLIC)
        self.assertFalse([step for step in public.path if step.from_name.startswith("PAY-") or step.to_name.startswith("PAY-")])

    def test_a_ticket_resolves_by_its_key(self):
        result = ask_service.ask("who should know about PAY-3", SLACK)
        self.assertTrue(result.found)
        cited = [step.cite() for step in result.path]
        self.assertIn("PAY-3 -[ABOUT]-> Billing Service", cited)
        self.assertIn("Robin Contractor (Jira) -[ASSIGNED]-> PAY-3", cited)
        # Maya's account links to her Person by its visible email, so she is cited as herself.
        maya = next(step for step in result.path if step.rel == "OPENED" and step.to_name == "PAY-3")
        self.assertEqual((maya.from_name, maya.from_label, maya.from_id), ("Maya Ortiz", "Person", "maya"))
        self.assertFalse(ask_service.ask("who should know about PAY-3", PUBLIC).found)


class JiraCappedBackfillTest(unittest.TestCase):
    """Through the framework's own sync step: a project that reaches the cap stops walking older
    pages and polls from the ``poll_cursor`` the backfill gave."""

    def setUp(self):
        reset_and_seed(quillfern)
        connector_graph.ensure_indexes()
        self.install = Install(source="jira", config={**CONFIG, "projects": "PAY"})
        self.fake = FakeJiraClient(page_size=2)
        connector = registry.get("jira")
        for patch in (
            mock.patch.object(connector, "client_for", lambda _install: self.fake),
            mock.patch.object(connector, "now", lambda: NOW),
            mock.patch.object(sync, "MORE_DELAY_SECONDS", 0.0),
            mock.patch.object(sync, "MAX_ITEMS_PER_CONTAINER", 2),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        sync._containers.clear()
        registry.configure(self.install)
        self.addCleanup(registry.unconfigure, "jira")
        self.addCleanup(sync._containers.clear)

    def _steps(self) -> list[str]:
        statuses = []
        for _ in range(10):
            statuses.append(sync.sync_step("jira")["status"])
            if statuses[-1] != "synced":
                return statuses
        raise AssertionError(f"sync never went idle: {statuses}")

    def test_a_capped_backfill_polls_from_the_poll_cursor_and_reads_no_older_page(self):
        self.assertEqual(self._steps(), ["synced", "idle"])
        container = "jira:default:container:10000"
        state = connector_graph.get_state(container)
        # Two of PAY's four recent issues fill the cap; the backfill ends on its first page.
        self.assertEqual(_rows("MATCH (t:Ticket {source: 'jira'}) RETURN t.name ORDER BY t.name"), [["PAY-1"], ["PAY-2"]])
        self.assertEqual((state["cursor"], state["has_more"], state["capped"], state["backfilled"]), ('{"since": "2026-10-18T03:30:00.000Z"}', False, True, True))
        self.assertNotIn("poll_cursor", state)

        connector_graph.set_state(container, self.install, due_at=0)
        self.assertEqual(self._steps(), ["synced", "idle"])
        # One backfill page, then a poll for what changed since PAY-1 (03:30, 150 minutes before NOW, plus
        # the overlap): never the backfill's next page, whose issues the cap would only drop.
        self.assertEqual(
            [(jql.split(" AND ", 1)[1], token) for jql, token, _size in self.fake.queries],
            [("updated >= -90d ORDER BY updated DESC", None), ("updated >= -155m ORDER BY updated ASC", None)],
        )


ORDER_SCOPE = ORG_INTERNAL

if __name__ == "__main__":
    unittest.main()
