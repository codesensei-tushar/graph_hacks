"""Pull requests that name Jira tickets: REFERENCES, CLOSES for "fixes KEY", and only for synced projects."""

import unittest

from tests.fixtures.jira import CONFIG, FakeJiraClient, load
from tests.integration.test_jira_sync import _rows, _sync

import src.connectors.jira as jira  # noqa: E402
from src.connectors import graph as connector_graph  # noqa: E402
from src.connectors.base import Install  # noqa: E402
from src.domain.github import ingest, repos  # noqa: E402
from src.seed import quillfern  # noqa: E402
from src.seed.seed import reset_and_seed  # noqa: E402

REPO = {"id": 5150, "full_name": "quillfern/notebook", "private": False, "html_url": "https://github.com/quillfern/notebook"}
AUTHOR = {"id": 11, "login": "arjun-mehta"}


def _pr(number: int, title: str, body: str = "", branch: str = "main") -> dict:
    return {"number": number, "title": title, "body": body, "state": "open", "user": AUTHOR, "head": {"ref": branch}}


def _links(number: int) -> list[list[str]]:
    return _rows(
        "MATCH (:PullRequest {id: $id})-[r:REFERENCES|CLOSES]->(t:Ticket) RETURN type(r), t.name ORDER BY type(r), t.name",
        {"id": ingest.pull_request_id(REPO["id"], number)},
    )


class JiraPullRequestLinksTest(unittest.TestCase):
    def setUp(self):
        reset_and_seed(quillfern)
        connector_graph.ensure_indexes()
        repos.upsert_repo(REPO, 7)
        self.fake = FakeJiraClient()
        self.connector = jira.JiraConnector()
        self.connector.client_for = lambda _install: self.fake
        self.install = Install(source="jira", config=CONFIG)

    def test_title_branch_and_body_reference_and_fixes_closes(self):
        _sync(self.connector, self.install)
        ingest.upsert_pull_request(
            REPO,
            _pr(41, "PAY-2: make renewal retries idempotent", "Fixes PAY-3. Waits on PLAT-7, like ENG-12 in Linear and UTF-8.", "feature/pay-1-retries"),
        )
        self.assertEqual(_links(41), [["CLOSES", "PAY-3"], ["REFERENCES", "PAY-1"], ["REFERENCES", "PAY-2"], ["REFERENCES", "PLAT-7"]])

    def test_an_edited_description_drops_the_link_it_no_longer_names(self):
        _sync(self.connector, self.install)
        ingest.upsert_pull_request(REPO, _pr(42, "Retries", "See PAY-2 and PLAT-7"))
        ingest.upsert_pull_request(REPO, _pr(42, "Retries", "See PAY-2"))
        self.assertEqual(_links(42), [["REFERENCES", "PAY-2"]])

    def test_a_ticket_synced_after_the_pull_request_links_back(self):
        self.fake.issues = [issue for issue in load("issues.json") if issue["key"] != "PAY-5"]
        _sync(self.connector, self.install)
        ingest.upsert_pull_request(REPO, _pr(43, "Document refund retries", "closes PAY-5"))
        self.assertEqual(_links(43), [])
        self.fake.issues = load("issues.json")
        _sync(self.connector, self.install)
        self.assertEqual(_links(43), [["CLOSES", "PAY-5"]])

    def test_without_jira_nothing_is_linked_or_kept(self):
        ingest.upsert_pull_request(REPO, _pr(44, "PAY-2: retries"))
        self.assertEqual(_links(44), [])
        self.assertEqual(_rows("MATCH (p:PullRequest {id: $id}) RETURN p.ticket_references", {"id": ingest.pull_request_id(REPO["id"], 44)}), [[None]])

    def test_keys_count_only_for_synced_projects(self):
        referenced, closed = ingest.jira_keys(_pr(1, "OPS-4 and PAY-12", "fixes: PLAT-3", "pay-9_hotfix"), {"PAY", "PLAT"})
        self.assertEqual((referenced, closed), (["PAY-12", "PAY-9"], ["PLAT-3"]))


if __name__ == "__main__":
    unittest.main()
