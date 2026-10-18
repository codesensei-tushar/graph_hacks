"""The heads-up a new pull request posts to Slack: real FalkorDB, fake GitHub, fake Slack Web API.

Seeds Quillfern and its scripted repos, installs the scripted workspace with #company-brain
as its home channel, and sends ``pull_request`` webhooks through ``handle_github_event``.
The Slack client is the real one, pointed at ``scripts/replay/slack_api.py``.
"""

import dataclasses
import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.core.config import settings  # noqa: E402
from src.db import falkordb_client  # noqa: E402
from src.db.falkordb_client import kv  # noqa: E402
from src.domain.github import alerts, webhooks  # noqa: E402
from src.domain.slack import workspace as workspace_module  # noqa: E402
from src.integrations.slack import client as slack_client_module  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402
from src.seed import quillfern_workspace as script  # noqa: E402
from src.seed.quillfern_workspace import RepoPullRequest  # noqa: E402
from scripts.replay.driver import fake_workspace_from_script, load_github_history, register_fake_workspace  # noqa: E402
from scripts.replay.github_api import FakeGitHubClient, pull_request_payload, repo_payload  # noqa: E402
from scripts.replay.slack_api import FakeSlackChannel, FakeSlackServer  # noqa: E402

GRAPH = f"company_coordination_alerts_test{tests.TEST_GRAPH_SUFFIX}"
HOME = "CBRAIN"
NOTEBOOK, BILLING_INTERNAL = script.REPOS

SHORTER_TOKENS = RepoPullRequest(58, "Shorten session token TTL", "Sessions expire after 15 minutes.", "maya-ortiz", ("services/auth/session.py",), merged=False)
LOUD_TITLE = RepoPullRequest(61, "<!channel> rotate keys & <https://evil.example|click>", "", "maya-ortiz", ("services/auth/session.py",), merged=False)
QUIET_TOGGLE = RepoPullRequest(59, "Toggle session cookies", "", "maya-ortiz", ("services/auth/session.py",), merged=False)
INVOICE_ONLY = RepoPullRequest(62, "Batch invoice retries", "", "lena-fischer", ("workers/invoice/retry.py",), merged=False)
DRAFT = RepoPullRequest(63, "Session audit log", "", "maya-ortiz", ("services/auth/session.py",), merged=False)
FLAKY = RepoPullRequest(64, "Session clock skew", "", "maya-ortiz", ("services/auth/session.py",), merged=False)
HOMELESS = RepoPullRequest(65, "Session renewal header", "", "maya-ortiz", ("services/auth/session.py",), merged=False)
PRIVATE_REFUNDS = RepoPullRequest(8, "Refund ledger cleanup", "", "tomas-reyes", ("services/billing/refunds.py",), merged=False)

NEW_PULL_REQUESTS = (SHORTER_TOKENS, LOUD_TITLE, QUIET_TOGGLE, INVOICE_ONLY, DRAFT, FLAKY, HOMELESS)
REPOS = (
    dataclasses.replace(NOTEBOOK, pull_requests=NOTEBOOK.pull_requests + NEW_PULL_REQUESTS),
    dataclasses.replace(BILLING_INTERNAL, pull_requests=BILLING_INTERNAL.pull_requests + (PRIVATE_REFUNDS,)),
)


def _event(repo, pr: RepoPullRequest, action: str = "opened", **changes) -> dict:
    payload = pull_request_payload(repo, pr)
    payload.update(changes)
    return {"action": action, "installation": {"id": script.INSTALLATION_ID}, "repository": repo_payload(repo), "pull_request": payload}


class CoordinationAlertTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = fake_workspace_from_script()
        cls.fake.add_channel(FakeSlackChannel(id=HOME, name="company-brain", members=[script.BOT_USER_ID]))
        cls.server = FakeSlackServer(cls.fake).start()
        cls.patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
            mock.patch.object(slack_client_module, "settings", dataclasses.replace(settings, slack_api_base_url=cls.server.base_url)),
            mock.patch.object(webhooks, "client_for", return_value=FakeGitHubClient(REPOS, script.INSTALLATION_ID)),
        ]
        for patch in cls.patches:
            patch.start()
        # Clients are cached per token; drop any built before the base URL was patched.
        workspace_module._client_for.cache_clear()
        seed.reset_and_seed(quillfern)
        load_github_history()
        register_fake_workspace()
        workspace_module.set_home_channel(script.TEAM_ID, HOME)

    @classmethod
    def tearDownClass(cls):
        try:
            for pr in (*NEW_PULL_REQUESTS, PRIVATE_REFUNDS):
                kv().delete(alerts.dedupe_key(NOTEBOOK.id, pr.number), alerts.dedupe_key(BILLING_INTERNAL.id, pr.number))
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            cls.server.stop()
            for patch in cls.patches:
                patch.stop()
            workspace_module._client_for.cache_clear()

    def setUp(self):
        for pr in (*NEW_PULL_REQUESTS, PRIVATE_REFUNDS):
            kv().delete(alerts.dedupe_key(NOTEBOOK.id, pr.number), alerts.dedupe_key(BILLING_INTERNAL.id, pr.number))
        self.seen = len(self._posted())

    def _send(self, repo, pr: RepoPullRequest, action: str = "opened", on: bool = True, **changes) -> str:
        with mock.patch.object(alerts, "settings", dataclasses.replace(settings, coordination_alerts=on)):
            return webhooks.handle_github_event("pull_request", _event(repo, pr, action, **changes))

    def _posted(self) -> list[str]:
        return [message.text for message in self.fake.messages if message.channel == HOME and message.user == self.fake.bot_user_id]

    def _new(self) -> list[str]:
        return self._posted()[self.seen :]

    def test_a_new_public_pull_request_posts_one_heads_up_with_its_path(self):
        self.assertEqual(self._send(NOTEBOOK, SHORTER_TOKENS), "pull_request")
        (text,) = self._new()
        self.assertTrue(text.startswith("Heads-up on Shorten session token TTL (https://github.com/quillfern/notebook/pull/58):\n"), text)
        self.assertIn(
            "notebook#58 changes Auth Service. 3 people who own something that depends on it weren't on the pull request. "
            "Arjun Mehta owns Billing Service (1 hop) and worked with Maya Ortiz on notebook#45, so a quick ping is enough.",
            text,
        )
        for line in ("• notebook#58 -[TOUCHES]-> Auth Service", "• Billing Service -[DEPENDS_ON]-> Auth Service", "• Arjun Mehta -[OWNS]-> Billing Service"):
            self.assertIn(line, text)
        ttl = kv().ttl(alerts.dedupe_key(NOTEBOOK.id, 58))
        self.assertGreater(ttl, 7 * 24 * 3600 - 60)
        self.assertLessEqual(ttl, 7 * 24 * 3600)
        # The same pull request again, reopened, or marked ready never posts twice.
        for action in ("opened", "reopened", "ready_for_review"):
            self._send(NOTEBOOK, SHORTER_TOKENS, action)
        self.assertEqual(len(self._new()), 1)

    def test_a_private_repo_never_posts_to_a_shared_channel(self):
        self.assertEqual(self._send(BILLING_INTERNAL, PRIVATE_REFUNDS), "pull_request")
        self.assertEqual(self._new(), [])
        self.assertIsNone(kv().get(alerts.dedupe_key(BILLING_INTERNAL.id, PRIVATE_REFUNDS.number)))

    def test_nothing_is_posted_while_the_flag_is_off(self):
        self._send(NOTEBOOK, QUIET_TOGGLE, on=False)
        self.assertEqual(self._new(), [])
        self.assertIsNone(kv().get(alerts.dedupe_key(NOTEBOOK.id, QUIET_TOGGLE.number)))
        # Ingestion still ran.
        _, graph = falkordb_client.connect()
        self.assertEqual(graph.query("MATCH (p:PullRequest {name: 'notebook#59'})-[:TOUCHES]->(s) RETURN s.name").result_set, [["Auth Service"]])

    def test_a_change_nobody_else_needs_to_hear_about_posts_nothing_and_stays_open_for_later(self):
        # Lena owns the Invoice Worker and nothing depends on it.
        self._send(NOTEBOOK, INVOICE_ONLY)
        self.assertEqual(self._new(), [])
        self.assertIsNone(kv().get(alerts.dedupe_key(NOTEBOOK.id, INVOICE_ONLY.number)))

    def test_a_draft_waits_until_it_is_ready_for_review(self):
        self._send(NOTEBOOK, DRAFT, draft=True)
        self.assertEqual(self._new(), [])
        self._send(NOTEBOOK, DRAFT, "ready_for_review", draft=False)
        self.assertEqual(len(self._new()), 1)

    def test_the_title_cannot_ping_the_channel_or_hide_a_link(self):
        self._send(NOTEBOOK, LOUD_TITLE)
        (text,) = self._new()
        self.assertTrue(text.startswith("Heads-up on &lt;!channel&gt; rotate keys &amp; &lt;https://evil.example|click&gt; ("), text)
        self.assertNotIn("<!channel>", text)

    def test_a_slack_failure_never_breaks_ingestion_and_the_next_event_can_retry(self):
        with mock.patch.object(slack_client_module.SlackClient, "post_message", side_effect=RuntimeError("slack is down")):
            self.assertEqual(self._send(NOTEBOOK, FLAKY), "pull_request")
        self.assertEqual(self._new(), [])
        self.assertIsNone(kv().get(alerts.dedupe_key(NOTEBOOK.id, FLAKY.number)))
        _, graph = falkordb_client.connect()
        self.assertEqual(graph.query("MATCH (p:PullRequest {name: 'notebook#64'}) RETURN count(p)").result_set, [[1]])
        self._send(NOTEBOOK, FLAKY, "reopened")
        self.assertEqual(len(self._new()), 1)

    def test_no_home_channel_posts_nothing_and_stays_open_for_later(self):
        with mock.patch.object(alerts, "list_connected_workspaces", return_value=[]):
            self._send(NOTEBOOK, HOMELESS)
        self.assertEqual(self._new(), [])
        self.assertIsNone(kv().get(alerts.dedupe_key(NOTEBOOK.id, HOMELESS.number)))


if __name__ == "__main__":
    unittest.main()
