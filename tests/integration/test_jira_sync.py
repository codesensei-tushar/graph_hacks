"""Jira into the graph end to end: real FalkorDB, Quillfern seed, fake Jira, Agent 2's graph writer.

Syncs the two allow-listed projects (PLAT first, so a link from PLAT to a PAY ticket that is
not there yet has to be picked up from the PAY side), then checks the tickets, who they are
about, the BLOCKS links, how accounts link to people, and what an asker in Slack and on the
public page each read.
"""

import unittest

from tests.fixtures.jira import CONFIG, FakeJiraClient

import src.connectors.jira as jira  # noqa: E402
from src.connectors import graph as connector_graph  # noqa: E402
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
        self.assertIn("Arjun Mehta -[ASSIGNED]-> PAY-1", cited)
        # Jira hides this account's email, so it stays an account: never linked to the seeded Person by its name.
        arjun = next(step for step in slack.path if step.from_name == "Arjun Mehta" and step.rel == "ASSIGNED")
        self.assertEqual((arjun.from_label, arjun.from_id), ("ExternalUser", "jira:default:user:712020:7f3e-arjun"))
        public = ask_service.who_knows("who knows the billing service", PUBLIC)
        self.assertFalse([step for step in public.path if step.from_name.startswith("PAY-") or step.to_name.startswith("PAY-")])

    def test_a_ticket_resolves_by_its_key(self):
        result = ask_service.ask("who should know about PAY-3", SLACK)
        self.assertTrue(result.found)
        cited = [step.cite() for step in result.path]
        self.assertIn("PAY-3 -[ABOUT]-> Billing Service", cited)
        self.assertIn("Robin Contractor -[ASSIGNED]-> PAY-3", cited)
        self.assertFalse(ask_service.ask("who should know about PAY-3", PUBLIC).found)


ORDER_SCOPE = ORG_INTERNAL

if __name__ == "__main__":
    unittest.main()
