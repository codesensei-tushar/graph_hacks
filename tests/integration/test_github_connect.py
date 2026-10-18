"""Connect GitHub from the console, end to end, against a fake GitHub over HTTP.

register the app (stored) -> an org admin clicks Connect -> GitHub's install page -> the setup URL
(state bound to the session and org) -> GitHub sign-in proving the person reaches the installation
-> the installation bound to the org, its account claimed -> the backfill writes that org's graph
only -> webhooks for org A never write org B -> disconnect removes the data. Plus the refusals,
the operator's bind (the legacy org), and the env app's legacy routing.
"""

import dataclasses
import hashlib
import hmac
import json
import time
import unittest
import urllib.parse
from unittest import mock

import tests  # noqa: F401  pins the test graphs before src loads

from cryptography.fernet import Fernet  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api.v1 import console_github, github as github_routes, github_installations, slack_install  # noqa: E402
from src.core import config, scheduler, tenancy  # noqa: E402
from src.db import falkordb_client  # noqa: E402
from src.db.falkordb_client import connect, kv  # noqa: E402
from src.domain import orgs  # noqa: E402
from src.domain.console import session as console_session  # noqa: E402
from src.domain.github import backfill, connections, installations  # noqa: E402
from src.domain.slack import token_crypto  # noqa: E402
from src.integrations.github import app_config, client  # noqa: E402
from tests.fixtures.github_server import CLIENT_ID, CLIENT_SECRET, FakeGitHub, make_repo, user  # noqa: E402

PUBLIC = "https://brain.test"
TOKEN = "operator-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SECRET = "connect-webhook-secret"
COOKIE = "__Host-brain_session"


@dataclasses.dataclass
class FakeSession:
    org: str
    id_hash: str
    role: str = "owner"
    person: str = "person-admin"
    actor: str = "slack:T1:UADMIN"


def _hash(cookie: str) -> str:
    return hashlib.sha256(cookie.encode()).hexdigest()


class GitHubConnectTest(unittest.TestCase):
    current: FakeSession | None = None

    @classmethod
    def setUpClass(cls):
        cls.github = FakeGitHub().start()
        cls.live = dataclasses.replace(
            config.settings, public_url=PUBLIC, brain_admin_token=TOKEN, console_enabled=True, token_encryption_keys=(Fernet.generate_key().decode(),),
            github_app_id="", github_webhook_secret="", github_private_key="", github_private_key_path="", github_public_repo_token="",
        )
        cls.patches = [
            mock.patch.object(app_config, "settings", cls.live),
            mock.patch.object(client, "settings", cls.live),
            mock.patch.object(console_github, "settings", cls.live),
            mock.patch.object(slack_install, "settings", cls.live),
            mock.patch.object(token_crypto, "settings", cls.live),
            mock.patch.object(client, "API", cls.github.base_url),
            mock.patch.object(client, "OAUTH", f"{cls.github.base_url}/login/oauth"),
            mock.patch.object(console_github, "console_admin", cls._admin),
        ]
        for patch in cls.patches:
            patch.start()
        orgs.ensure_control_indexes()
        cls.acme = orgs.create_org("Acme GitHub", "T1:U1")
        cls.rival = orgs.create_org("Rival GitHub", "T2:U2")
        app = FastAPI()
        app.include_router(console_github.api)
        app.include_router(console_github.callbacks)
        app.include_router(github_routes.router)
        app.include_router(github_installations.router)
        app.dependency_overrides[console_session.console_admin] = cls._admin
        app.dependency_overrides[console_session.console_user] = cls._admin
        cls.app = app

    @classmethod
    def tearDownClass(cls):
        try:
            for org in (cls.acme, cls.rival):
                falkordb_client.client().select_graph(org.graph).delete()
                tenancy.control_graph().query("MATCH (o:Org {id: $id}) DETACH DELETE o", {"id": org.id})
            tenancy.control_graph().query("MATCH (n) WHERE n:GitHubApp OR n:GitHubInstallation OR (n:ExternalAccount AND n.key STARTS WITH 'github:') DETACH DELETE n")
            kv().delete(scheduler.QUEUE, scheduler.PAYLOADS, installations.INFLIGHT_KEY)
        finally:
            for patch in reversed(cls.patches):
                patch.stop()
            app_config.forget()
            client._installation_cache = None
            client._tokens.clear()
            cls.github.stop()

    @classmethod
    async def _admin(cls, request=None):
        from fastapi import HTTPException

        if cls.current is None:
            raise HTTPException(status_code=401)
        return cls.current

    def setUp(self):
        control = tenancy.control_graph()
        control.query("MATCH (n) WHERE n:GitHubApp OR n:GitHubInstallation OR (n:ExternalAccount AND n.key STARTS WITH 'github:') DETACH DELETE n")
        for org in (self.acme.ref(), self.rival.ref(), tenancy.legacy_org()):
            with tenancy.org_context(org):
                _, graph = connect()
                graph.query("MATCH (n) WHERE n:Repo OR n:PullRequest OR (n:Ticket AND n.source = 'github') OR n:GitHubUser DETACH DELETE n")
        kv().delete(scheduler.QUEUE, scheduler.PAYLOADS, installations.INFLIGHT_KEY)
        app_config.forget()
        client._tokens.clear()
        client._installation_cache = None
        app_config.store(app_config.AppCredentials(
            app_id=str(self.github.app_id), source="stored", slug="company-brain-test", client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
            private_key=self.github.pem, webhook_secret=SECRET, owner_login="brain-operator", owner_id=str(self.github.app_owner["id"]),
        ))
        self.github.installations.clear()
        self.github.repos.clear()
        self.github.add_installation(501, user(1001, "acme-gh"), [make_repo(7001, "acme-gh/api", private=True, pulls=4), make_repo(7002, "acme-gh/docs", private=False, pulls=2)], ["acme-admin"])
        self.github.add_installation(502, {**user(1002, "rival-gh"), "type": "Organization"}, [make_repo(7101, "rival-gh/core", private=True, pulls=3)], ["rival-admin"])
        self.github.add_installation(503, user(1003, "codesensei-tushar"), [make_repo(7201, "codesensei-tushar/graph_hacks", private=False, pulls=30)], ["codesensei-tushar"])
        self.client = TestClient(self.app, base_url=PUBLIC)
        type(self).current = None

    # --- helpers --------------------------------------------------------------------------------

    def _as(self, org, cookie: str):
        self.client.cookies.set(COOKIE, cookie)
        type(self).current = FakeSession(org=org.id, id_hash=_hash(cookie))

    def _connect(self, org, cookie: str) -> str:
        self._as(org, cookie)
        response = self.client.post("/console/api/github/connect")
        self.assertEqual(response.status_code, 200, response.text)
        url = response.json()["install_url"]
        self.assertTrue(url.startswith("https://github.com/apps/company-brain-test/installations/new?state="))
        return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]

    def _setup(self, state: str, installation_id: int) -> str:
        response = self.client.get("/console/oauth/github/setup", params={"installation_id": installation_id, "setup_action": "install", "state": state}, follow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text)
        location = response.headers["location"]
        self.assertTrue(location.startswith(f"{self.github.base_url}/login/oauth/authorize?"), location)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
        self.assertEqual(query["redirect_uri"], [f"{PUBLIC}/console/oauth/github/verify"])
        return query["state"][0]

    def _verify(self, state: str, login: str, user_id: int) -> str:
        code = self.github.user_code(login, user_id)
        response = self.client.get("/console/oauth/github/verify", params={"code": code, "state": state}, follow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text)
        return response.headers["location"]

    def _connect_all(self, org, cookie: str, installation_id: int, login: str, user_id: int) -> str:
        return self._verify(self._setup(self._connect(org, cookie), installation_id), login, user_id)

    def _drain(self, rounds: int = 300) -> None:
        names = {backfill.REPO_STEP, backfill.REMOVE_JOB, connections.INSTALL_JOB}
        for _ in range(rounds):
            pending = {json.loads(raw)["name"] for raw in (kv().hgetall(scheduler.PAYLOADS) or {}).values()}
            if not pending & names:
                return
            scheduler.run_due(now=time.time() + backfill.MAX_WAIT_SECONDS + 60)
        self.fail("jobs did not finish")

    def _count(self, org, query: str, params: dict | None = None):
        with tenancy.org_context(org):
            _, graph = connect()
            return graph.query(query, params or {}).result_set[0][0]

    def _webhook(self, event: str, payload: dict, delivery: str, secret: str = SECRET):
        kv().delete(github_routes._delivery_key(delivery))
        body = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery, "X-Hub-Signature-256": signature, "Content-Type": "application/json"}
        return self.client.post("/github/webhook", content=body, headers=headers)

    def _issue_event(self, installation_id: int, repo_full_name: str, number: int) -> dict:
        repo = self.github.repos[repo_full_name]["repo"]
        return {"action": "opened", "installation": {"id": installation_id}, "repository": repo,
                "issue": {"number": number, "title": f"Webhook issue {number}", "body": "", "state": "open", "user": user(1500, "reporter")}}

    # --- tests ----------------------------------------------------------------------------------

    def test_connect_backfill_webhooks_and_disconnect(self):
        acme, rival = self.acme.ref(), self.rival.ref()
        self.assertEqual(self._connect_all(acme, "cookie-acme", 501, "acme-admin", 2001), "/console/github?connected=1")
        self.assertEqual(self._connect_all(rival, "cookie-rival", 502, "rival-admin", 2002), "/console/github?connected=1")
        self._drain()

        self.assertEqual(self._count(acme, "MATCH (p:PullRequest) RETURN count(p)"), 6)
        self.assertEqual(self._count(rival, "MATCH (p:PullRequest) RETURN count(p)"), 3)
        self.assertEqual(self._count(tenancy.legacy_org(), "MATCH (p:PullRequest) RETURN count(p)"), 0)
        # Private repo: its own scope, read through the collaborators GitHub lists. Public: org_shared.
        self.assertEqual(self._count(acme, "MATCH (p:PullRequest)-[:IN_REPO]->(:Repo {id: '7001'}) RETURN collect(DISTINCT p.scope)"), ["github_repo_7001"])
        self.assertEqual(self._count(acme, "MATCH (p:PullRequest)-[:IN_REPO]->(:Repo {id: '7002'}) RETURN collect(DISTINCT p.scope)"), ["org_shared"])
        self.assertEqual(self._count(acme, "MATCH (:GitHubUser)-[:CAN_READ]->(:Repo {id: '7001'}) RETURN count(*)"), 2)
        self.assertEqual(orgs.external_account_org("github:1001"), self.acme.id)

        overview = self.client.get("/console/api/github").json()  # as rival, the last session
        [mine] = overview["installations"]
        self.assertEqual((mine["id"], mine["account"], mine["account_type"]), ("502", "rival-gh", "Organization"))
        self.assertEqual(mine["manage_url"], "https://github.com/organizations/rival-gh/settings/installations/502")
        self.assertEqual([(repo["full_name"], repo["status"], repo["pull_requests"]) for repo in mine["repos"]], [("rival-gh/core", "ready", 3)])

        # A webhook for Acme's installation writes Acme's graph, never Rival's.
        self.assertEqual(self._webhook("issues", self._issue_event(501, "acme-gh/api", 90), "d-acme-90").json(), {"ok": True})
        self.assertEqual(self._count(acme, "MATCH (t:Ticket {number: 90}) RETURN count(t)"), 1)
        self.assertEqual(self._count(rival, "MATCH (t:Ticket {number: 90}) RETURN count(t)"), 0)
        self.assertEqual(self._webhook("issues", self._issue_event(502, "rival-gh/core", 91), "d-rival-91").json(), {"ok": True})
        self.assertEqual(self._count(rival, "MATCH (t:Ticket {number: 91}) RETURN count(t)"), 1)
        self.assertEqual(self._count(acme, "MATCH (t:Ticket {number: 91}) RETURN count(t)"), 0)
        # Nobody connected 503: with a registered app its events are dropped, not given to the legacy org.
        dropped = self._webhook("issues", self._issue_event(503, "codesensei-tushar/graph_hacks", 92), "d-none-92")
        self.assertEqual(dropped.json(), {"ok": True, "routed": False})
        self.assertEqual(self._count(tenancy.legacy_org(), "MATCH (t:Ticket {number: 92}) RETURN count(t)"), 0)
        self.assertEqual(self._webhook("issues", self._issue_event(501, "acme-gh/api", 93), "d-bad", secret="wrong").status_code, 401)

        # Disconnect Acme: everything it brought is deleted; Rival keeps its own.
        self._as(acme, "cookie-acme")
        gone = self.client.delete("/console/api/github/installations/501")
        self.assertEqual(gone.status_code, 202)
        self.assertEqual(gone.json()["uninstall_url"], "https://github.com/settings/installations/501")
        self._drain()
        left = self._count(acme, "MATCH (n) WHERE n:Repo OR n:PullRequest OR (n:Ticket AND n.source = 'github') OR n:GitHubUser RETURN count(n)")
        self.assertEqual(left, 0)
        self.assertEqual(self._count(rival, "MATCH (p:PullRequest) RETURN count(p)"), 3)
        self.assertEqual(installations.get(501)["status"], installations.DISCONNECTED)
        self.assertIsNone(orgs.external_account_org("github:1001"))
        # Still installed on GitHub, but disconnected: its events stay dropped.
        self.assertEqual(self._webhook("issues", self._issue_event(501, "acme-gh/api", 94), "d-acme-94").json(), {"ok": True, "routed": False})
        self.assertEqual(self._count(acme, "MATCH (t:Ticket) RETURN count(t)"), 0)
        # Uninstalled on GitHub afterwards: the tombstone is forgotten.
        self._webhook("installation", {"action": "deleted", "installation": {"id": 501, "account": user(1001, "acme-gh")}}, "d-acme-del")
        self.assertIsNone(installations.get(501))

    def test_another_orgs_installation_cannot_be_taken(self):
        acme, rival = self.acme.ref(), self.rival.ref()
        self._connect_all(acme, "cookie-acme", 501, "acme-admin", 2001)
        # Rival types Acme's installation id into the setup URL. Its admin cannot reach it on GitHub.
        self.assertEqual(self._connect_all(rival, "cookie-rival", 501, "rival-admin", 2002), "/console/github?error=github_not_yours")
        # Even someone who can reach it on GitHub cannot move it: the account is Acme's.
        self.github.installations[501]["users"].append("double-agent")
        self.assertEqual(self._connect_all(rival, "cookie-rival", 501, "double-agent", 2003), "/console/github?error=account_claimed")
        self.assertEqual(installations.get(501)["org"], self.acme.id)
        self._drain()
        self.assertEqual(self._count(rival, "MATCH (p:PullRequest) RETURN count(p)"), 0)

    def test_state_is_bound_to_the_session_and_single_use(self):
        acme = self.acme.ref()
        state = self._connect(acme, "cookie-acme")
        # Another browser (another session) with the link.
        self._as(acme, "cookie-someone-else")
        response = self.client.get("/console/oauth/github/setup", params={"installation_id": 501, "state": state}, follow_redirects=False)
        self.assertEqual(response.headers["location"], "/console/github?error=github_state")
        # Spent, even for the right browser now.
        self._as(acme, "cookie-acme")
        response = self.client.get("/console/oauth/github/setup", params={"installation_id": 501, "state": state}, follow_redirects=False)
        self.assertEqual(response.headers["location"], "/console/github?error=github_state")
        # A made-up verify state, and a declined sign-in.
        response = self.client.get("/console/oauth/github/verify", params={"code": "x", "state": "made-up"}, follow_redirects=False)
        self.assertEqual(response.headers["location"], "/console/github?error=github_state")
        second = self._setup(self._connect(acme, "cookie-acme"), 501)
        response = self.client.get("/console/oauth/github/verify", params={"error": "access_denied", "state": second}, follow_redirects=False)
        self.assertEqual(response.headers["location"], "/console/github?error=github_denied")
        self.assertIsNone(installations.get(501))
        # No state at all (repo selection changed on GitHub): back to the console, nothing bound.
        response = self.client.get("/console/oauth/github/setup", params={"installation_id": 501, "setup_action": "update"}, follow_redirects=False)
        self.assertEqual(response.headers["location"], "/console/github?updated=1")

    def test_operator_binds_the_demo_repo_to_the_legacy_org_with_every_pull_request(self):
        listed = TestClient(self.app).get("/ops/github/installations", headers=AUTH).json()["installations"]
        self.assertEqual({item["id"]: item["status"] for item in listed}, {"501": "unbound", "502": "unbound", "503": "unbound"})
        bound = TestClient(self.app).post("/ops/github/installations/503/bind", json={"backfill": "all"}, headers=AUTH)
        self.assertEqual(bound.status_code, 200, bound.text)
        self._drain()
        legacy = tenancy.legacy_org()
        self.assertEqual(self._count(legacy, "MATCH (p:PullRequest {scope: 'org_shared'}) RETURN count(p)"), 30)
        self.assertEqual(installations.get(503)["org"], tenancy.LEGACY_ORG_ID)
        # Its webhooks now reach the legacy org.
        self._webhook("issues", self._issue_event(503, "codesensei-tushar/graph_hacks", 95), "d-legacy-95")
        self.assertEqual(self._count(legacy, "MATCH (t:Ticket {number: 95}) RETURN count(t)"), 1)
        self.assertEqual(TestClient(self.app).post("/ops/github/installations/503/bind", params={"org": self.acme.id}, headers=AUTH).status_code, 409)
        self.assertEqual(TestClient(self.app).post("/ops/github/installations/503/bind", headers={"Authorization": "Bearer nope"}).status_code, 403)
        # And the operator's install link carries a state with no browser session.
        link = TestClient(self.app).post("/ops/github/connect", params={"org": self.acme.id}, headers=AUTH)
        self.assertTrue(link.json()["install_url"].startswith("https://github.com/apps/company-brain-test/installations/new?state="))

    def test_the_env_app_keeps_feeding_the_legacy_org(self):
        env = dataclasses.replace(self.live, github_app_id=str(self.github.app_id), github_webhook_secret="env-secret", github_private_key=self.github.pem)
        with mock.patch.object(app_config, "settings", env):
            # An installation nobody connected: the legacy org's, as before orgs, bound on first sight.
            self._webhook("issues", self._issue_event(503, "codesensei-tushar/graph_hacks", 96), "d-env-96", secret="env-secret")
            self.assertEqual(self._count(tenancy.legacy_org(), "MATCH (t:Ticket {number: 96}) RETURN count(t)"), 1)
            self.assertEqual(installations.get(503)["org"], tenancy.LEGACY_ORG_ID)
            # While a console Connect is in flight, a brand-new installation waits instead.
            installations.connect_started("someone-connecting")
            response = self._webhook("installation", {"action": "created", "installation": {"id": 502, "account": user(1002, "rival-gh")}}, "d-env-new", secret="env-secret")
            self.assertEqual(response.json(), {"ok": True, "routed": False})
            self.assertIsNone(installations.get(502))
            self.assertTrue(scheduler.exists("github:unrouted:502"))


if __name__ == "__main__":
    unittest.main()
