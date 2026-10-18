"""GitHub App ingestion end to end: real FalkorDB, fake GitHub.

Seeds Company Brain into the throwaway ``company_test`` graph, backfills one private
repo, and checks that pull requests land on the right services and that only linked
collaborators can read the repo, and only from a DM.
"""

import dataclasses
import hashlib
import hmac
import json
import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from fastapi.testclient import TestClient  # noqa: E402

from src.api.v1 import github as github_routes  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.db.falkordb_client import connect, kv  # noqa: E402
from src.domain.github import account_link, backfill, repos, webhooks  # noqa: E402
from src.domain.knowledge import ask_service  # noqa: E402
from src.domain.memory.read_scope import resolve_brain_read_container_tags  # noqa: E402
from src.domain.memory.writeback import SlackMemoryScope  # noqa: E402
from src.integrations.github import app_config  # noqa: E402
from src.integrations.github.client import GitHubUserIdentity  # noqa: E402
from scripts.replay.driver import run_github_jobs  # noqa: E402
from src.seed import company_brain  # noqa: E402
from src.seed.seed import reset_and_seed  # noqa: E402

REPO = {"id": 4242, "full_name": "codesensei-tushar/graph_hacks", "private": True, "html_url": "https://github.com/x"}
TUSHAR = {"id": 1, "login": "codesensei-tushar"}
ALICE = {"id": 2, "login": "alice-dev"}
MALLORY = {"id": 3, "login": "mallory"}
SCOPE = "github_repo_4242"
SECRET = "test-webhook-secret"


class FakeGitHub:
    def __init__(self, installation_id: int = 7):
        self.installation_id = installation_id
        self.collaborators = [TUSHAR, ALICE]

    def list_repositories(self):
        return [REPO]

    def list_collaborators(self, _full_name):
        return list(self.collaborators)

    def list_issues(self, _full_name, _limit):
        return [
            {
                "number": 1,
                "title": "Memory search misses notes saved in DMs",
                "body": "search brain skips user scopes",
                "state": "open",
                "user": TUSHAR,
                "assignees": [ALICE],
            },
            {"number": 2, "title": "a PR shows up in issues too", "pull_request": {}, "user": ALICE},
        ]

    def list_pull_requests(self, _full_name, _limit):
        return [{"number": 3, "title": "Search DM notes", "body": "Closes #1", "state": "open", "user": ALICE}]

    def list_pull_request_files(self, _full_name, _number):
        return [{"filename": "src/domain/memory/search_brain.py"}, {"filename": "tests/unit/test_x.py"}]

    def list_pull_request_reviews(self, _full_name, _number):
        return [{"user": TUSHAR, "state": "APPROVED"}]


def _slack_user_is(slack_user_id: str, person_id: str) -> None:
    _, graph = connect()
    graph.query(
        "MERGE (u:SlackUser {id: $u}) WITH u MATCH (p:Person {id: $p}) MERGE (u)-[:IS]->(p)",
        {"u": slack_user_id, "p": person_id},
    )


def _dm(slack_user_id: str) -> SlackMemoryScope:
    return SlackMemoryScope(kind="dm", channel_id=f"D{slack_user_id}", channel_type="im", user_id=slack_user_id, slack_user_id=slack_user_id)


class GitHubIngestTest(unittest.TestCase):
    def setUp(self):
        reset_and_seed(company_brain)
        self.fake = FakeGitHub()
        patcher = mock.patch.object(backfill, "client_for", return_value=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        backfill.backfill_installation(7)
        _slack_user_is("UTUSHAR", "tushar")
        _, graph = connect()
        graph.query("CREATE (:Person {id: 'p-sam', name: 'Sam'})")
        _slack_user_is("USAM", "p-sam")

    def test_backfill_writes_scoped_nodes_and_links(self):
        _, graph = connect()
        pr = graph.query("MATCH (p:PullRequest {id: 'github:4242:pr:3'}) OPTIONAL MATCH (p)-[:TOUCHES]->(s:Service) RETURN p.scope, collect(s.id)").result_set[
            0
        ]
        self.assertEqual(pr[0], SCOPE)
        self.assertEqual(pr[1], ["cb-memory"])
        closes = graph.query("MATCH (:PullRequest {id: 'github:4242:pr:3'})-[:CLOSES]->(t:Ticket) RETURN t.id").result_set
        self.assertEqual(closes, [["github:4242:issue:1"]])
        # The PR listed by the issues API is not a ticket.
        self.assertEqual(graph.query("MATCH (t:Ticket {id: 'github:4242:issue:2'}) RETURN t").result_set, [])
        # The seeded Person.github login links the GitHub user automatically.
        linked = graph.query("MATCH (:GitHubUser {login: 'codesensei-tushar'})-[:IS]->(p:Person) RETURN p.id").result_set
        self.assertEqual(linked, [["tushar"]])

    def test_who_knows_uses_pull_requests_only_inside_the_repo_scope(self):
        public = ask_service.who_knows("memory layer", ["org_shared"])
        self.assertNotIn("alice-dev", public.answer)
        private = ask_service.who_knows("memory layer", ["org_shared", SCOPE])
        self.assertIn("@alice-dev", private.answer)
        self.assertIn("graph_hacks#3", [step.to_name for step in private.path])
        # A GitHub account with no linked Person is labelled as what it is.
        alice = next(step for step in private.path if step.from_name == "@alice-dev")
        self.assertEqual(alice.from_label, "GitHubUser")

    def test_dm_reads_repo_only_for_linked_collaborators(self):
        self.assertIn(SCOPE, resolve_brain_read_container_tags(_dm("UTUSHAR")))
        self.assertNotIn(SCOPE, resolve_brain_read_container_tags(_dm("USAM")))
        private_channel = SlackMemoryScope(kind="private_channel", channel_id="G1", channel_type="group", user_id="UTUSHAR")
        self.assertNotIn(SCOPE, resolve_brain_read_container_tags(private_channel))
        shared = SlackMemoryScope(kind="shared", channel_id="C1", channel_type="channel")
        self.assertNotIn(SCOPE, resolve_brain_read_container_tags(shared))

    def test_removed_collaborator_loses_access(self):
        self.fake.collaborators = [ALICE]
        backfill.sync_collaborators(self.fake, REPO)
        self.assertEqual(repos.readable_github_scopes_for_slack_user("UTUSHAR"), [])

    def test_public_repo_goes_to_org_shared(self):
        public_repo = {**REPO, "id": 99, "full_name": "codesensei-tushar/public", "private": False}
        self.assertEqual(repos.upsert_repo(public_repo, 7), "org_shared")

    def test_link_requires_a_verified_github_sign_in_and_is_one_time(self):
        url = account_link.create_github_link_url("USAM")
        state = url.split("state=")[1].split("&")[0]

        def exchange(_code, _redirect):
            return GitHubUserIdentity(id=ALICE["id"], login=ALICE["login"])

        result = account_link.complete_github_link(state, "code", exchange=exchange)
        self.assertTrue(result.ok)
        self.assertIn(SCOPE, repos.readable_github_scopes_for_slack_user("USAM"))
        self.assertFalse(account_link.complete_github_link(state, "code", exchange=exchange).ok)
        self.assertFalse(account_link.complete_github_link("made-up-state", "code", exchange=exchange).ok)

    def test_uninstall_removes_the_repo_and_its_nodes(self):
        webhooks.handle_github_event("installation", {"action": "deleted", "installation": {"id": 7}})
        # The removal is batched scheduler jobs.
        run_github_jobs()
        _, graph = connect()
        left = graph.query("MATCH (n) WHERE n:Repo OR n:PullRequest OR (n:Ticket AND n.source = 'github') RETURN count(n)").result_set[0][0]
        self.assertEqual(left, 0)
        self.assertEqual(repos.readable_github_scopes_for_slack_user("UTUSHAR"), [])


class GitHubWebhookRouteTest(unittest.TestCase):
    def setUp(self):
        reset_and_seed(company_brain)
        configured = dataclasses.replace(settings, github_app_id="1", github_webhook_secret=SECRET, github_private_key="unused")
        for target in (mock.patch.object(app_config, "settings", configured), mock.patch.object(webhooks, "client_for", return_value=FakeGitHub())):
            target.start()
            self.addCleanup(target.stop)
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(github_routes.router)
        self.client = TestClient(app)

    def _post(self, event: str, payload: dict, delivery: str, secret: str = SECRET):
        body = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery, "X-Hub-Signature-256": signature, "Content-Type": "application/json"}
        return self.client.post("/github/webhook", content=body, headers=headers)

    def test_signed_issue_event_is_ingested_once(self):
        delivery = "delivery-issue-10"
        kv().delete(github_routes._delivery_key(delivery))
        payload = {
            "action": "opened",
            "installation": {"id": 7},
            "repository": REPO,
            "issue": {"number": 10, "title": "Slack events API returns 401", "body": "", "state": "open", "user": MALLORY},
        }
        self.assertEqual(self._post("issues", payload, delivery).status_code, 200)
        _, graph = connect()
        rows = graph.query("MATCH (t:Ticket {id: 'github:4242:issue:10'})-[:ABOUT]->(s:Service) RETURN t.scope, s.id").result_set
        self.assertEqual(rows, [[SCOPE, "cb-slack"]])
        self.assertEqual(self._post("issues", payload, delivery).json().get("duplicate"), True)

    def test_bad_signature_is_rejected(self):
        response = self._post("issues", {"action": "opened"}, "delivery-bad", secret="wrong")
        self.assertEqual(response.status_code, 401)

    def test_ping(self):
        self.assertEqual(self._post("ping", {"zen": "hi"}, "delivery-ping").json(), {"ok": True, "pong": True})


if __name__ == "__main__":
    unittest.main()
