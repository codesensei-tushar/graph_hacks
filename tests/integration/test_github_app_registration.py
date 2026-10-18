"""Registering the GitHub App from the operator page, against a fake GitHub over HTTP.

operator token -> manifest + state -> (GitHub creates the app) -> /ops/github/app/created?code&state
-> POST /app-manifests/{code}/conversions -> the app stored encrypted in the control graph ->
the app's JWT is accepted by (fake) GitHub. Plus: env credentials win, the state is single use,
a second registration is refused, nothing is stored without an encryption key.
"""

import dataclasses
import json
import unittest
import urllib.parse
from unittest import mock

import tests  # noqa: F401  pins the test graphs before src loads

from cryptography.fernet import Fernet  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api.v1 import github_app as routes  # noqa: E402
from src.api.v1 import slack_install  # noqa: E402
from src.core import config, tenancy  # noqa: E402
from src.domain.github import app_registration  # noqa: E402
from src.domain.slack import token_crypto  # noqa: E402
from src.integrations.github import app_config, client  # noqa: E402
from tests.fixtures.github_server import APP_SLUG, CLIENT_SECRET, WEBHOOK_SECRET, FakeGitHub  # noqa: E402

PUBLIC = "https://brain.test"
TOKEN = "operator-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class GitHubAppRegistrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.github = FakeGitHub().start()
        cls.live = dataclasses.replace(
            config.settings, public_url=PUBLIC, brain_admin_token=TOKEN, token_encryption_keys=(Fernet.generate_key().decode(),),
            github_app_id="", github_webhook_secret="", github_private_key="", github_private_key_path="",
        )
        cls.patches = [
            mock.patch.object(app_registration, "settings", cls.live),
            mock.patch.object(app_config, "settings", cls.live),
            mock.patch.object(slack_install, "settings", cls.live),
            mock.patch.object(token_crypto, "settings", cls.live),
            mock.patch.object(client, "API", cls.github.base_url),
            mock.patch.object(client, "OAUTH", f"{cls.github.base_url}/login/oauth"),
        ]
        for patch in cls.patches:
            patch.start()
        app = FastAPI()
        app.include_router(routes.router)
        cls.client = TestClient(app, base_url=PUBLIC)

    @classmethod
    def tearDownClass(cls):
        tenancy.control_graph().delete()
        for patch in reversed(cls.patches):
            patch.stop()
        app_config.forget()
        cls.github.stop()

    def setUp(self):
        tenancy.control_graph().query("MATCH (a:GitHubApp) DETACH DELETE a")
        app_config.forget()
        self.github.manifest_codes = {"manifest-code"}

    def _start(self, **body) -> tuple[str, dict]:
        response = self.client.post("/ops/github/app/manifest", json=body, headers=AUTH)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        state = urllib.parse.parse_qs(urllib.parse.urlparse(data["post_url"]).query)["state"][0]
        return state, data

    def test_page_allows_a_form_to_github_only(self):
        response = self.client.get("/ops/github/app")
        self.assertEqual(response.status_code, 200)
        self.assertIn("form-action https://github.com", response.headers["content-security-policy"])
        self.assertIn("script-src 'self'", response.headers["content-security-policy"])
        self.assertNotIn("<script>", response.text)

    def test_routes_need_the_operator_token(self):
        self.assertEqual(self.client.get("/ops/github/app/status").status_code, 403)
        self.assertEqual(self.client.post("/ops/github/app/manifest", json={}).status_code, 403)
        self.assertEqual(self.client.post("/ops/github/app/manifest", json={}, headers={"Authorization": "Bearer nope"}).status_code, 403)

    def test_manifest_is_read_only_and_points_at_this_deployment(self):
        state, data = self._start()
        self.assertTrue(data["post_url"].startswith("https://github.com/settings/apps/new?state="))
        self.assertGreaterEqual(len(state), 40)
        manifest = json.loads(data["manifest"])
        self.assertEqual(manifest["name"], "Company Brain")
        self.assertEqual(manifest["hook_attributes"], {"url": f"{PUBLIC}/github/webhook", "active": True})
        self.assertEqual(manifest["redirect_url"], f"{PUBLIC}/ops/github/app/created")
        self.assertEqual(manifest["setup_url"], f"{PUBLIC}/console/oauth/github/setup")
        self.assertIn(f"{PUBLIC}/console/oauth/github/verify", manifest["callback_urls"])
        self.assertIn(f"{PUBLIC}/github/oauth/callback", manifest["callback_urls"])
        self.assertTrue(manifest["public"])
        self.assertEqual(set(manifest["default_permissions"].values()), {"read"})
        self.assertEqual(set(manifest["default_permissions"]), {"metadata", "contents", "pull_requests", "issues", "members"})
        # GitHub refuses a subscription to these; every app gets them anyway.
        self.assertNotIn("installation", manifest["default_events"])
        self.assertNotIn("installation_repositories", manifest["default_events"])
        self.assertEqual(set(manifest["default_events"]), {"pull_request", "pull_request_review", "issues", "repository", "member"})

    def test_organization_owner_and_bad_input(self):
        _state, data = self._start(owner_org="acme-inc", name="Acme Brain")
        self.assertTrue(data["post_url"].startswith("https://github.com/organizations/acme-inc/settings/apps/new?state="))
        self.assertEqual(json.loads(data["manifest"])["name"], "Acme Brain")
        bad = self.client.post("/ops/github/app/manifest", json={"owner_org": "../evil"}, headers=AUTH)
        self.assertEqual((bad.status_code, bad.json()["error"]), (400, "bad_owner"))
        bad = self.client.post("/ops/github/app/manifest", json={"name": "x" * 40}, headers=AUTH)
        self.assertEqual((bad.status_code, bad.json()["error"]), (400, "bad_name"))

    def test_register_end_to_end_stores_the_app_encrypted(self):
        state, _ = self._start()
        response = self.client.get("/ops/github/app/created", params={"code": "manifest-code", "state": state})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(f"https://github.com/apps/{APP_SLUG}/installations/new", response.text)
        self.assertNotIn("PRIVATE KEY", response.text)
        self.assertNotIn(WEBHOOK_SECRET, response.text)

        rows = tenancy.control_graph().query("MATCH (a:GitHubApp) RETURN properties(a)").result_set
        self.assertEqual(len(rows), 1)
        stored = json.dumps(rows[0][0])
        for secret in ("PRIVATE KEY", WEBHOOK_SECRET, CLIENT_SECRET):
            self.assertNotIn(secret, stored)
        self.assertTrue(rows[0][0]["secret_enc"].startswith("fernet:"))

        creds = app_config.current()
        self.assertEqual((creds.source, creds.app_id, creds.slug, creds.webhook_secret, creds.client_secret), ("stored", "90210", APP_SLUG, WEBHOOK_SECRET, CLIENT_SECRET))
        self.assertNotIn(WEBHOOK_SECRET, repr(creds))
        # The stored key signs a JWT GitHub accepts.
        self.assertEqual(client.get_app()["slug"], APP_SLUG)

        status = self.client.get("/ops/github/app/status", headers=AUTH).json()
        self.assertTrue(status["configured"])
        self.assertEqual(status["app"]["install_url"], f"https://github.com/apps/{APP_SLUG}/installations/new")
        self.assertNotIn(WEBHOOK_SECRET, json.dumps(status))

        # A second registration would strand the first app's installations.
        again = self.client.post("/ops/github/app/manifest", json={}, headers=AUTH)
        self.assertEqual((again.status_code, again.json()["error"]), (409, "already_registered"))

    def test_state_is_single_use_and_required(self):
        state, _ = self._start()
        self.assertEqual(self.client.get("/ops/github/app/created", params={"code": "manifest-code", "state": "made-up"}).status_code, 400)
        self.assertFalse(app_config.stored_exists())
        self.assertEqual(self.client.get("/ops/github/app/created", params={"code": "manifest-code", "state": state}).status_code, 200)
        tenancy.control_graph().query("MATCH (a:GitHubApp) DETACH DELETE a")
        self.github.manifest_codes = {"manifest-code"}
        replay = self.client.get("/ops/github/app/created", params={"code": "manifest-code", "state": state})
        self.assertEqual(replay.status_code, 400)
        self.assertFalse(app_config.stored_exists())

    def test_a_refused_code_stores_nothing(self):
        state, _ = self._start()
        response = self.client.get("/ops/github/app/created", params={"code": "spent-code", "state": state})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(app_config.stored_exists())

    def test_no_encryption_key_no_registration(self):
        plain = dataclasses.replace(self.live, token_encryption_keys=(), token_encryption_key="")
        with mock.patch.object(token_crypto, "settings", plain):
            response = self.client.post("/ops/github/app/manifest", json={}, headers=AUTH)
        self.assertEqual((response.status_code, response.json()["error"]), (409, "no_encryption_key"))

    def test_env_credentials_win(self):
        state, _ = self._start()
        self.client.get("/ops/github/app/created", params={"code": "manifest-code", "state": state})
        env = dataclasses.replace(self.live, github_app_id="123", github_webhook_secret="env-secret", github_private_key="-----BEGIN-----\\nkey\\n-----END-----", github_app_slug="env-app")
        with mock.patch.object(app_config, "settings", env), mock.patch.object(app_registration, "settings", env):
            creds = app_config.current()
            self.assertEqual((creds.source, creds.app_id, creds.webhook_secret, creds.slug), ("env", "123", "env-secret", "env-app"))
            self.assertEqual(creds.private_key, "-----BEGIN-----\nkey\n-----END-----")
            refused = self.client.post("/ops/github/app/manifest", json={}, headers=AUTH)
            self.assertEqual(refused.json()["error"], "env_configured")


if __name__ == "__main__":
    unittest.main()
