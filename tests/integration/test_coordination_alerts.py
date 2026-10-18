"""The heads-up a new pull request posts to Slack: real FalkorDB, fake GitHub, fake Slack Web API.

Seeds Quillfern and its scripted repos, installs the scripted workspace with #company-brain
as its home channel, and sends ``pull_request`` webhooks through ``handle_github_event``.
The Slack client is the real one, pointed at ``scripts/replay/slack_api.py``. The new pull
requests live in copies of the scripted repos with ids of their own, so their dedupe keys
never meet the replay's or another checkout's (``TEST_GRAPH_SUFFIX``).
"""

import dataclasses
import unittest
import zlib
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.core import scheduler  # noqa: E402
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
from scripts.replay.github_api import FakeGitHubClient, github_user, pull_request_payload, repo_payload  # noqa: E402
from scripts.replay.slack_api import FakeSlackChannel, FakeSlackServer  # noqa: E402

GRAPH = f"company_coordination_alerts_test{tests.TEST_GRAPH_SUFFIX}"
HOME = "CBRAIN"
TEST_REPO_ID = 990_000 + zlib.crc32(tests.TEST_GRAPH_SUFFIX.encode()) % 9_000
AUTH = ("services/auth/session.py",)


def _pr(number: int, title: str, author: str = "maya-ortiz", files: tuple[str, ...] = AUTH) -> RepoPullRequest:
    return RepoPullRequest(number, title, "", author, files, merged=False)


SHORTER_TOKENS = _pr(58, "Shorten session token TTL")
QUIET_TOGGLE = _pr(59, "Toggle session cookies")
LOUD_TITLE = _pr(61, "<!channel> rotate keys & <https://evil.example|click>")
INVOICE_ONLY = _pr(62, "Batch invoice retries", "lena-fischer", ("workers/invoice/retry.py",))
DRAFT = _pr(63, "Session audit log")
FLAKY = _pr(64, "Session clock skew")
HOMELESS = _pr(65, "Session renewal header")
REQUESTED = _pr(66, "Session cookie flags")
PHISHY = _pr(67, "Fix login [see the incident doc](https://evil.example/phish)")
OUTSIDER = _pr(68, "Drive-by session tweak")
COLLABORATOR = _pr(69, "Session tweak from a collaborator")
DOWN = _pr(70, "Session refresh jitter")
BROKEN = _pr(71, "Session idle timeout")
PRIVATE_REFUNDS = _pr(8, "Refund ledger cleanup", "tomas-reyes", ("services/billing/refunds.py",))

NEW_PULL_REQUESTS = (SHORTER_TOKENS, QUIET_TOGGLE, LOUD_TITLE, INVOICE_ONLY, DRAFT, FLAKY, HOMELESS, REQUESTED, PHISHY, OUTSIDER, COLLABORATOR, DOWN, BROKEN)
NOTEBOOK = dataclasses.replace(script.REPOS[0], id=TEST_REPO_ID, pull_requests=script.REPOS[0].pull_requests + NEW_PULL_REQUESTS)
BILLING_INTERNAL = dataclasses.replace(script.REPOS[1], id=TEST_REPO_ID + 1, pull_requests=script.REPOS[1].pull_requests + (PRIVATE_REFUNDS,))
STRANGER = {"id": 99_999, "login": "drive-by"}


def _event(repo, pr: RepoPullRequest, action: str = "opened", **changes) -> dict:
    payload = pull_request_payload(repo, pr)
    payload.update(changes)
    return {"action": action, "installation": {"id": script.INSTALLATION_ID}, "repository": repo_payload(repo), "pull_request": payload}


def _clear_keys() -> None:
    for pr in (*NEW_PULL_REQUESTS, PRIVATE_REFUNDS):
        kv().delete(alerts.dedupe_key(NOTEBOOK.id, pr.number), alerts.dedupe_key(BILLING_INTERNAL.id, pr.number))
    scheduler.cancel_matching(alerts.RETRY_JOB)


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
            mock.patch.object(webhooks, "client_for", return_value=FakeGitHubClient((NOTEBOOK, BILLING_INTERNAL), script.INSTALLATION_ID)),
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
            _clear_keys()
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            cls.server.stop()
            for patch in cls.patches:
                patch.stop()
            workspace_module._client_for.cache_clear()

    def setUp(self):
        _clear_keys()
        self.seen = len(self._posted())

    def _send(self, repo, pr: RepoPullRequest, action: str = "opened", on: bool = True, **changes) -> str:
        with mock.patch.object(alerts, "settings", dataclasses.replace(settings, coordination_alerts=on)):
            return webhooks.handle_github_event("pull_request", _event(repo, pr, action, **changes))

    def _posted(self) -> list[str]:
        return [message.text for message in self.fake.messages if message.channel == HOME and message.user == self.fake.bot_user_id]

    def _new(self) -> list[str]:
        return self._posted()[self.seen :]

    def _key(self, pr: RepoPullRequest, repo=NOTEBOOK):
        return kv().get(alerts.dedupe_key(repo.id, pr.number))

    # --- what it posts ---------------------------------------------------------------------

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

    def test_someone_asked_to_review_is_in_the_loop(self):
        self._send(NOTEBOOK, REQUESTED, requested_reviewers=[github_user("arjun-mehta")])
        (text,) = self._new()
        self.assertIn("notebook#66 changes Auth Service. 2 people who own something that depends on it weren't on the pull request.", text)
        self.assertNotIn("Arjun Mehta owns", text)
        # The request is a graph edge, kept in step with GitHub: gone once it is withdrawn.
        _, graph = falkordb_client.connect()
        requested = "MATCH (:GitHubUser {login: 'arjun-mehta'})-[r:REVIEW_REQUESTED]->(:PullRequest {name: 'notebook#66'}) RETURN count(r)"
        self.assertEqual(graph.query(requested).result_set, [[1]])
        self._send(NOTEBOOK, REQUESTED, "edited", requested_reviewers=[])
        self.assertEqual(graph.query(requested).result_set, [[0]])

    # --- what it never posts ---------------------------------------------------------------

    def test_a_private_repo_never_posts_to_a_shared_channel(self):
        self.assertEqual(self._send(BILLING_INTERNAL, PRIVATE_REFUNDS), "pull_request")
        self.assertEqual(self._new(), [])
        self.assertIsNone(self._key(PRIVATE_REFUNDS, BILLING_INTERNAL))

    def test_nothing_is_posted_while_the_flag_is_off(self):
        self._send(NOTEBOOK, QUIET_TOGGLE, on=False)
        self.assertEqual(self._new(), [])
        self.assertIsNone(self._key(QUIET_TOGGLE))
        # Ingestion still ran.
        _, graph = falkordb_client.connect()
        self.assertEqual(graph.query("MATCH (p:PullRequest {name: 'notebook#59'})-[:TOUCHES]->(s) RETURN s.name").result_set, [["Auth Service"]])

    def test_an_outsider_on_a_public_repo_cannot_make_the_bot_post(self):
        self._send(NOTEBOOK, OUTSIDER, user=STRANGER, author_association="NONE")
        self.assertEqual(self._new(), [])
        self.assertIsNone(self._key(OUTSIDER))
        # GitHub vouching for them as a collaborator is enough, linked to a person or not.
        self._send(NOTEBOOK, COLLABORATOR, user=STRANGER, author_association="COLLABORATOR")
        (text,) = self._new()
        # An account linked to no person can only be compared by pull request.
        self.assertIn("and the graph shows no pull request shared with @drive-by: introduce them before this merges.", text)

    def test_a_change_nobody_else_needs_to_hear_about_posts_nothing_and_stays_open_for_later(self):
        # Lena owns the Invoice Worker and nothing depends on it.
        self._send(NOTEBOOK, INVOICE_ONLY)
        self.assertEqual(self._new(), [])
        self.assertIsNone(self._key(INVOICE_ONLY))

    def test_a_draft_waits_until_it_is_ready_for_review(self):
        self._send(NOTEBOOK, DRAFT, draft=True)
        self.assertEqual(self._new(), [])
        self._send(NOTEBOOK, DRAFT, "ready_for_review", draft=False)
        self.assertEqual(len(self._new()), 1)

    def test_no_home_channel_posts_nothing_and_stays_open_for_later(self):
        _, graph = falkordb_client.connect()
        graph.query("MATCH (w:SlackWorkspace {team_id: $team}) SET w.home_channel_id = NULL", {"team": script.TEAM_ID})
        try:
            self._send(NOTEBOOK, HOMELESS)
        finally:
            workspace_module.set_home_channel(script.TEAM_ID, HOME)
        self.assertEqual(self._new(), [])
        self.assertIsNone(self._key(HOMELESS))

    # --- titles are anyone's text ----------------------------------------------------------

    def test_the_title_cannot_ping_the_channel_or_hide_a_link(self):
        self._send(NOTEBOOK, LOUD_TITLE)
        (text,) = self._new()
        self.assertTrue(text.startswith("Heads-up on &lt;!channel&gt; rotate keys &amp; &lt;https://evil.example|click&gt; ("), text)
        self.assertNotIn("<!channel>", text)

    def test_a_markdown_link_in_the_title_stays_visible_text(self):
        self._send(NOTEBOOK, PHISHY)
        (text,) = self._new()
        self.assertNotIn("<https://evil.example/phish|", text)
        self.assertIn("https://evil.example/phish", text)

    # --- failures --------------------------------------------------------------------------

    def test_slack_saying_no_gives_the_key_back_and_a_retry_posts_it(self):
        scheduled = []
        with mock.patch.object(slack_client_module.SlackClient, "post_message", return_value=None), mock.patch.object(
            alerts.scheduler, "schedule", side_effect=lambda *args, **kwargs: scheduled.append((args, kwargs))
        ):
            self.assertEqual(self._send(NOTEBOOK, DOWN), "pull_request")
        self.assertEqual(self._new(), [])
        self.assertIsNone(self._key(DOWN))
        ((args, kwargs),) = scheduled
        delay, name, payload = args
        self.assertEqual((delay, name), (alerts.RETRY_DELAYS[0], alerts.RETRY_JOB))
        self.assertEqual(kwargs["job_id"], alerts.retry_job_id(NOTEBOOK.id, DOWN.number))
        # The scheduler runs the retry later; by then Slack answers.
        with mock.patch.object(alerts, "settings", dataclasses.replace(settings, coordination_alerts=True)):
            alerts.retry_alert(payload)
        self.assertEqual(len(self._new()), 1)
        self.assertIsNotNone(self._key(DOWN))

    def test_a_slack_error_never_breaks_ingestion(self):
        with mock.patch.object(slack_client_module.SlackClient, "post_message", side_effect=RuntimeError("slack is down")):
            self.assertEqual(self._send(NOTEBOOK, FLAKY), "pull_request")
        self.assertEqual(self._new(), [])
        self.assertIsNone(self._key(FLAKY))
        self.assertTrue(scheduler.exists(alerts.retry_job_id(NOTEBOOK.id, FLAKY.number)))
        _, graph = falkordb_client.connect()
        self.assertEqual(graph.query("MATCH (p:PullRequest {name: 'notebook#64'}) RETURN count(p)").result_set, [[1]])

    def test_a_message_that_cannot_be_built_never_holds_the_key(self):
        with mock.patch.object(alerts, "message", side_effect=RuntimeError("bad path step")):
            self.assertEqual(self._send(NOTEBOOK, BROKEN), "pull_request")
        self.assertIsNone(self._key(BROKEN))
        self._send(NOTEBOOK, BROKEN, "reopened")
        self.assertEqual(len(self._new()), 1)


if __name__ == "__main__":
    unittest.main()
