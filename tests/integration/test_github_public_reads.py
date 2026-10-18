"""Which credentials public-repo reads use once a GitHub App is registered, against a fake GitHub.

GITHUB_PUBLIC_REPO_TOKEN first; then the app's installation on its owner's own account (never
another company's); then the app's client id and secret; then nothing.
"""

import dataclasses
import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graphs before src loads

from cryptography.fernet import Fernet  # noqa: E402

from src.core import config, tenancy  # noqa: E402
from src.domain.slack import token_crypto  # noqa: E402
from src.integrations.github import app_config, client  # noqa: E402
from tests.fixtures.github_server import CLIENT_ID, CLIENT_SECRET, PUBLIC_TOKEN, FakeGitHub, make_repo, user  # noqa: E402

REPO = "codesensei-tushar/graph_hacks"


class PublicReadsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.github = FakeGitHub().start()
        cls.live = dataclasses.replace(
            config.settings, token_encryption_keys=(Fernet.generate_key().decode(),), github_public_repo_token="",
            github_app_id="", github_webhook_secret="", github_private_key="", github_private_key_path="",
        )
        cls.patches = [
            mock.patch.object(client, "settings", cls.live),
            mock.patch.object(app_config, "settings", cls.live),
            mock.patch.object(token_crypto, "settings", cls.live),
            mock.patch.object(client, "API", cls.github.base_url),
        ]
        for patch in cls.patches:
            patch.start()

    @classmethod
    def tearDownClass(cls):
        tenancy.control_graph().query("MATCH (a:GitHubApp) DETACH DELETE a")
        for patch in reversed(cls.patches):
            patch.stop()
        app_config.forget()
        client._installation_cache = None
        cls.github.stop()

    def setUp(self):
        tenancy.control_graph().query("MATCH (a:GitHubApp) DETACH DELETE a")
        app_config.forget()
        client._installation_cache = None
        self.github.repos.clear()
        self.github.installations.clear()
        self.github.add_public_repo(make_repo(880001, REPO, private=False))

    def _register(self):
        app_config.store(app_config.AppCredentials(
            app_id=str(self.github.app_id), source="stored", slug="company-brain-test", client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
            private_key=self.github.pem, webhook_secret="w", owner_login="brain-operator", owner_id=str(self.github.app_owner["id"]),
        ))

    def _read(self) -> str:
        client.public_client().get_repo(REPO)
        return self.github.auth_seen[-1]

    def test_no_app_no_token_reads_anonymously(self):
        self.assertEqual(self._read(), "anonymous")

    def test_the_token_comes_first(self):
        self._register()
        with mock.patch.object(client, "settings", dataclasses.replace(self.live, github_public_repo_token=PUBLIC_TOKEN)):
            self.assertEqual(self._read(), "token")

    def test_the_owners_installation_then_client_credentials(self):
        self._register()
        # Another company's installation is never used for public reads.
        self.github.add_installation(41, user(9001, "other-company"), [make_repo(880020, "other-company/app", private=True)], ["someone"])
        self.assertEqual(self._read(), "client")
        self.github.add_installation(31, self.github.app_owner, [make_repo(880010, "brain-operator/notes", private=True)], ["brain-operator"])
        client._installation_cache = None
        self.assertEqual(self._read(), "installation")


if __name__ == "__main__":
    unittest.main()
