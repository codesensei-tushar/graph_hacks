"""Connecting Jira and HubSpot from the console over OAuth, end to end, against fake vendors over HTTP.

connect -> the vendor's authorize URL -> callback (state bound to the session and org) -> code
exchange -> the install stored, its tokens encrypted, its vendor account claimed -> a sync that
reads through ``oauth.bearer_for`` -> an expired token refreshed under the kv lock, the rotated
refresh token saved before it is used. Plus every way a callback is refused.

The vendors are ``tests/fixtures/oauth_servers.py`` on 127.0.0.1; the orgs are real orgs in the
test control graph, each with its own graph. Package A's session is faked: ``_admin_session``
returns the session the test sets.
"""

import dataclasses
import hashlib
import importlib
import time
import logging
import os
import threading
import unittest
import urllib.parse
from unittest import mock

import tests  # noqa: F401  pins the test graphs before src loads

from cryptography.fernet import Fernet  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api.v1 import console_oauth  # noqa: E402
from src.connectors import graph as connector_graph  # noqa: E402
from src.connectors import oauth, registry, store, sync  # noqa: E402
from src.connectors.base import ConnectorError, CredentialsRejected  # noqa: E402
from src.connectors.hubspot import client as hubspot_client  # noqa: E402
from src.connectors.hubspot import oauth as hubspot_oauth  # noqa: E402
from src.connectors.hubspot.connector import HubSpotConnector  # noqa: E402
from src.connectors.jira import JiraConnector  # noqa: E402
from src.connectors.jira import oauth as jira_oauth  # noqa: E402
from src.core import config, scheduler, tenancy  # noqa: E402
from src.db import falkordb_client  # noqa: E402
from src.domain import orgs  # noqa: E402
from src.domain.slack import token_crypto  # noqa: E402
from tests.fixtures.oauth_servers import CLIENT, CLOUD_ID, HUB_ID, FakeVendors  # noqa: E402

PUBLIC = "https://brain.test"
# Package A's real dependency, before the test class replaces it with a fake.
REAL_ADMIN_SESSION = console_oauth._admin_session
COOKIE = "__Host-brain_session"


@dataclasses.dataclass
class FakeSession:
    org: str
    id_hash: str
    person: str = "person-maya"
    role: str = "owner"
    csrf: str = "csrf-token"
    operator: bool = False


def _hash(cookie: str) -> str:
    return hashlib.sha256(cookie.encode()).hexdigest()


class _Captured(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


async def _fake_admin_session(request):
    """Stands in for package A's async ``console_admin``."""
    return ConsoleOAuthTest._session()


class ConsoleOAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vendors = FakeVendors().start()
        base = cls.vendors.base_url
        live = dataclasses.replace(config.settings, console_enabled=True, public_url=PUBLIC, token_encryption_keys=(Fernet.generate_key().decode(),))
        jira_spec = dataclasses.replace(jira_oauth.SPEC, token_url=f"{base}/oauth/token")
        hubspot_spec = dataclasses.replace(hubspot_oauth.SPEC, token_url=f"{base}/oauth/2026-09/token")
        cls._patches = [
            mock.patch.object(console_oauth, "settings", live),
            mock.patch.object(oauth, "settings", live),
            mock.patch.object(token_crypto, "settings", live),
            mock.patch.dict(os.environ, {
                "JIRA_OAUTH_CLIENT_ID": CLIENT["jira"][0], "JIRA_OAUTH_CLIENT_SECRET": CLIENT["jira"][1],
                "HUBSPOT_OAUTH_CLIENT_ID": CLIENT["hubspot"][0], "HUBSPOT_OAUTH_CLIENT_SECRET": CLIENT["hubspot"][1],
            }),
            mock.patch.object(jira_oauth, "API_ROOT", base),
            mock.patch.object(jira_oauth, "SPEC", jira_spec),
            mock.patch.object(JiraConnector, "oauth", jira_spec),
            mock.patch.object(hubspot_client, "BASE_URL", base),
            mock.patch.object(hubspot_oauth, "INTROSPECT_URL", f"{base}/oauth/2026-09/token/introspect"),
            mock.patch.object(hubspot_oauth, "SPEC", hubspot_spec),
            mock.patch.object(HubSpotConnector, "oauth", hubspot_spec),
            mock.patch.object(console_oauth, "_admin_session", _fake_admin_session),
            mock.patch.object(scheduler, "schedule"),
            mock.patch.object(sync, "MORE_DELAY_SECONDS", 0.0),
        ]
        for patch in cls._patches:
            patch.start()
        cls._registered = {source: registry.get(source) for source in ("jira", "hubspot")}
        registry.register(JiraConnector())
        registry.register(HubSpotConnector())
        cls.acme = orgs.create_org("Acme OAuth", "T1:U1")
        cls.rival = orgs.create_org("Rival OAuth", "T2:U2")
        app = FastAPI()
        app.include_router(console_oauth.router)
        cls.client = TestClient(app, base_url=PUBLIC)

    @classmethod
    def tearDownClass(cls):
        try:
            for org in (cls.acme, cls.rival):
                falkordb_client.client().select_graph(org.graph).delete()
            tenancy.control_graph().delete()
        finally:
            for source, connector in cls._registered.items():
                if connector is not None:
                    registry.register(connector)
                else:
                    registry._connectors.pop(source, None)
            for patch in reversed(cls._patches):
                patch.stop()
            cls.vendors.stop()

    current: FakeSession | None = None

    @classmethod
    def _session(cls):
        if cls.current is None:
            raise HTTPException(status_code=401)
        return cls.current

    def setUp(self):
        oauth._cache.clear()
        self.vendors.codes.update({"jira-code": "jira", "hubspot-code": "hubspot"})
        self.cookie = "cookie-" + self.id().rsplit(".", 1)[-1]
        type(self).current = FakeSession(org=self.acme.id, id_hash=_hash(self.cookie))
        self.logs = _Captured()
        logging.getLogger().addHandler(self.logs)
        self.addCleanup(logging.getLogger().removeHandler, self.logs)
        self.addCleanup(self._release_accounts)

    def _release_accounts(self):
        for key in (f"jira:{CLOUD_ID}", f"hubspot:{HUB_ID}"):
            for org in (self.acme, self.rival):
                orgs.release_external_account(key, org.id)
        for org in (self.acme, self.rival):
            with tenancy.org_context(org.id):
                for row in store.list_installs():
                    store.delete(row.id)
                    registry.drop(org.id, row.source, row.install)

    # --- the flow -------------------------------------------------------------------------------

    def _connect(self, source: str, cookie: str | None = None, **headers) -> object:
        sent = {"X-CSRF-Token": "csrf-token", "Origin": PUBLIC, "Cookie": f"{COOKIE}={cookie or self.cookie}", **headers}
        return self.client.post(f"/console/api/connectors/{source}/connect", headers={k: v for k, v in sent.items() if v is not None})

    def _authorize(self, source: str) -> dict:
        answer = self._connect(source)
        self.assertEqual(answer.status_code, 200, answer.text)
        self.vendors.codes[f"{source}-code"] = source  # the vendor issues a new code each time
        url = urllib.parse.urlsplit(answer.json()["authorize_url"])
        return {"url": url, "query": dict(urllib.parse.parse_qsl(url.query))}

    def _callback(self, source: str, cookie: str | None = None, **query):
        return self.client.get(f"/console/oauth/{source}/callback", params=query, headers={"Cookie": f"{COOKIE}={cookie or self.cookie}"}, follow_redirects=False)

    def _connected(self, source: str) -> str:
        state = self._authorize(source)["query"]["state"]
        answer = self._callback(source, code=f"{source}-code", state=state)
        self.assertEqual(answer.status_code, 303)
        self.assertEqual(answer.headers["location"], f"/console?connected={source}")
        return self._install_id(self.acme.id, source)

    def _install_id(self, org: str, source: str) -> str:
        with tenancy.org_context(org):
            rows = store.list_installs(source)
        self.assertEqual(len(rows), 1)
        return rows[0].id

    def _raw_secret(self, org: str, install_id: str) -> str:
        with tenancy.org_context(org):
            _, graph = falkordb_client.connect()
            return graph.query("MATCH (c:ConnectorInstall {id: $id}) RETURN c.secret_enc", {"id": install_id}).result_set[0][0]

    def _sync(self, org: str, install) -> int:
        """What sync_step does for one install, until every container is caught up. Returns pages written."""
        pages = 0
        with tenancy.org_context(org):
            connector = registry.get(install.source)
            sync._containers.pop(install.key, None)
            containers = sync._current_containers(connector, install)
            self.assertTrue(containers)
            for container in containers:
                for _ in range(20):
                    report = sync._fetch_one(connector, install, container, connector_graph.get_state(container.id), 0)
                    self.assertEqual(report["status"], "synced", report)
                    pages += 1
                    if not connector_graph.get_state(container.id).get("has_more"):
                        break
        return pages

    def _count(self, org: str, query: str) -> int:
        with tenancy.org_context(org):
            _, graph = falkordb_client.connect()
            return graph.query(query).result_set[0][0]

    def _expire(self, org: str, install) -> str:
        """Make the stored access token stale; returns the refresh token it had."""
        secret = store.read_secret(org, install.key)
        store.write_secret(org, install.key, {**secret, "expires_at": 0})
        oauth._cache.clear()
        return secret["refresh_token"]

    def assertNoSecretsLogged(self):
        for line in self.logs.lines:
            for marker in ("-access-", "-refresh-", "jira-code", "hubspot-code", "client-secret"):
                self.assertNotIn(marker, line)

    # --- Jira -----------------------------------------------------------------------------------

    def test_jira_connect_sync_and_refresh(self):
        authorize = self._authorize("jira")
        self.assertEqual(authorize["url"].netloc, "auth.atlassian.com")
        query = authorize["query"]
        self.assertEqual(query["audience"], "api.atlassian.com")
        self.assertEqual(query["prompt"], "consent")
        self.assertEqual(query["scope"], "read:jira-work read:jira-user offline_access")
        self.assertEqual(query["redirect_uri"], f"{PUBLIC}/console/oauth/jira/callback")
        self.assertEqual(query["client_id"], CLIENT["jira"][0])

        answer = self._callback("jira", code="jira-code", state=query["state"])
        self.assertEqual(answer.status_code, 303)
        location = answer.headers["location"]
        self.assertEqual(location, "/console?connected=jira")
        install_id = self._install_id(self.acme.id, "jira")
        self.assertRegex(install_id, r"^jira:[0-9a-f]{12}$")

        with tenancy.org_context(self.acme.id):
            stored = store.get(install_id)
        self.assertEqual((stored.status, stored.auth, stored.external_account, stored.site), ("connecting", "oauth", f"jira:{CLOUD_ID}", "https://quillfern.atlassian.net"))
        self.assertEqual(stored.created_by, "person-maya")
        self.assertEqual(stored.config, {"cloud_id": CLOUD_ID, "site_url": "https://quillfern.atlassian.net"})
        raw = self._raw_secret(self.acme.id, install_id)
        self.assertTrue(raw.startswith("fernet:"))
        self.assertNotIn("jira-access", raw)
        self.assertNotIn("jira-refresh", raw)
        self.assertEqual(orgs.external_account_org(f"jira:{CLOUD_ID}"), self.acme.id)

        # The admin picks projects (package B's route) from every project the token can browse.
        offered = registry.get("jira").available_containers(store.get_install(self.acme.id, install_id))
        self.assertEqual([(c.key, c.external_id) for c in offered], [("HR", "10002"), ("PAY", "10000"), ("PLAT", "10001")])
        # Then the sync reads through bearer_for.
        with tenancy.org_context(self.acme.id):
            store.update_config(install_id, {"projects": "PAY,PLAT"})
            store.set_status(install_id, "active")
            install = store.to_install(store.get(install_id))
        self.assertGreater(self._sync(self.acme.id, install), 0)
        self.assertGreater(self._count(self.acme.id, "MATCH (t:Ticket {source: 'jira'}) RETURN count(t)"), 0)
        self.assertEqual(self._count(self.acme.id, "MATCH (t:Ticket {source: 'jira'}) WHERE NOT t.url STARTS WITH 'https://quillfern.atlassian.net/browse/' RETURN count(t)"), 0)
        jira_paths = {path for method, path in self.vendors.calls if "/rest/api/3/" in path}
        self.assertTrue(jira_paths and all(path.startswith(f"/ex/jira/{CLOUD_ID}/") for path in jira_paths))
        # Nothing reached the legacy graph.
        self.assertEqual(self._count(tenancy.LEGACY_ORG_ID, "MATCH (t:Ticket {source: 'jira', install: '" + install.key + "'}) RETURN count(t)"), 0)

        # The access token expires: one refresh, the rotated refresh token saved, the spent one dead.
        spent = self._expire(self.acme.id, install)
        before = self.vendors.refreshes["jira"]
        token = oauth.bearer_for(install)
        self.assertEqual(self.vendors.refreshes["jira"], before + 1)
        saved = store.read_secret(self.acme.id, install.key)
        self.assertEqual(saved["access_token"], token)
        self.assertNotEqual(saved["refresh_token"], spent)
        self.assertIn(spent, self.vendors.spent)
        self.assertIn(saved["refresh_token"], self.vendors.refresh)
        self.assertEqual(oauth.bearer_for(install), token)  # cached; no second refresh
        self.assertEqual(self.vendors.refreshes["jira"], before + 1)
        self.assertGreater(self._sync(self.acme.id, install), 0)
        self.assertNoSecretsLogged()

    def test_concurrent_refreshes_spend_the_refresh_token_once(self):
        install_id = self._connected("jira")
        install = store.get_install(self.acme.id, install_id)
        self._expire(self.acme.id, install)
        before = self.vendors.refreshes["jira"]
        tokens, errors = [], []

        def worker():
            try:
                tokens.append(oauth.bearer_for(install))
            except Exception as error:  # pragma: no cover - reported below
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.vendors.refreshes["jira"], before + 1)
        self.assertEqual(len(set(tokens)), 1)
        self.assertEqual(falkordb_client.kv().get(oauth.lock_key(install)), None)

    def test_revoked_refresh_token_asks_for_a_reconnect(self):
        install_id = self._connected("jira")
        install = store.get_install(self.acme.id, install_id)
        spent = self._expire(self.acme.id, install)
        self.vendors.refresh.pop(spent)
        with self.assertRaises(CredentialsRejected) as raised:
            oauth.bearer_for(install)
        self.assertIn("invalid_grant", str(raised.exception))
        self.assertNotIn(spent, str(raised.exception))
        self.assertNotIn("Unknown or invalid", str(raised.exception))
        self.assertNoSecretsLogged()

    def test_reconnecting_a_site_replaces_the_tokens_of_its_install(self):
        first = self._connected("jira")
        old = store.read_secret(self.acme.id, first)
        second = self._connected("jira")
        self.assertEqual(second, first)
        self.assertNotEqual(store.read_secret(self.acme.id, first)["refresh_token"], old["refresh_token"])
        with tenancy.org_context(self.acme.id):
            self.assertEqual(len(store.list_installs("jira")), 1)

    def test_several_sites_wait_for_a_choice(self):
        other = {"id": "99999999-2222-4333-8444-555555555555", "name": "Other", "url": "https://other.atlassian.net", "scopes": ["read:jira-work"]}
        self.vendors.resources.append(other)
        self.addCleanup(self.vendors.resources.remove, other)
        state = self._authorize("jira")["query"]["state"]
        answer = self._callback("jira", code="jira-code", state=state)
        self.assertRegex(answer.headers["location"], r"^/console/connect/jira/site\?id=jira%3A[0-9a-f]{12}$")
        install_id = urllib.parse.unquote(answer.headers["location"].split("id=")[1])
        install = store.get_install(self.acme.id, install_id)
        self.assertEqual([site.cloud_id for site in jira_oauth.sites_for(install)], [CLOUD_ID, other["id"]])
        self.assertIsNone(orgs.external_account_org(f"jira:{CLOUD_ID}"))
        with self.assertRaises(LookupError):
            jira_oauth.choose_site(self.acme.id, install_id, "not-a-granted-site")
        self.assertEqual(jira_oauth.choose_site(self.acme.id, install_id, CLOUD_ID), install_id)
        with tenancy.org_context(self.acme.id):
            stored = store.get(install_id)
        self.assertEqual((stored.external_account, stored.config["site_url"]), (f"jira:{CLOUD_ID}", "https://quillfern.atlassian.net"))
        self.assertEqual(orgs.external_account_org(f"jira:{CLOUD_ID}"), self.acme.id)

    def test_a_resource_outside_atlassian_net_is_not_a_site(self):
        self.vendors.resources[0]["url"] = "https://quillfern.evil.example"
        self.addCleanup(self.vendors.resources[0].__setitem__, "url", "https://quillfern.atlassian.net")
        state = self._authorize("jira")["query"]["state"]
        self.assertEqual(self._callback("jira", code="jira-code", state=state).headers["location"], "/console?error=oauth_failed")

    # --- HubSpot --------------------------------------------------------------------------------

    def test_hubspot_connect_sync_and_refresh(self):
        authorize = self._authorize("hubspot")
        self.assertEqual(authorize["url"].netloc, "app.hubspot.com")
        self.assertEqual(authorize["query"]["scope"], "crm.objects.companies.read crm.objects.deals.read crm.objects.owners.read oauth")
        answer = self._callback("hubspot", code="hubspot-code", state=authorize["query"]["state"])
        self.assertEqual(answer.headers["location"], "/console?connected=hubspot")
        install_id = self._install_id(self.acme.id, "hubspot")
        with tenancy.org_context(self.acme.id):
            stored = store.get(install_id)
            store.set_status(install_id, "active")
            install = store.to_install(store.get(install_id))
        self.assertEqual((stored.external_account, stored.config, stored.site), (f"hubspot:{HUB_ID}", {"hub_id": str(HUB_ID)}, "https://app.hubspot.com"))
        self.assertNotIn("hubspot-access", self._raw_secret(self.acme.id, install_id))

        self.assertGreater(self._sync(self.acme.id, install), 0)
        self.assertGreater(self._count(self.acme.id, "MATCH (s:Service {source: 'hubspot', kind: 'account'}) RETURN count(s)"), 0)

        refresh_token = self._expire(self.acme.id, install)
        token = oauth.bearer_for(install)
        saved = store.read_secret(self.acme.id, install.key)
        self.assertEqual((saved["access_token"], saved["refresh_token"]), (token, refresh_token))  # HubSpot does not rotate
        self.assertEqual(self.vendors.refreshes["hubspot"], 1)
        self.assertNoSecretsLogged()

    def test_hubspot_token_for_another_portal_reads_nothing(self):
        install_id = self._connected("hubspot")
        with tenancy.org_context(self.acme.id):
            store.update_config(install_id, {"hub_id": "13579"})
            install = store.to_install(store.get(install_id))
            with self.assertRaises(ConnectorError):
                registry.get("hubspot").check_credentials(install)

    # --- concurrency, removal, the app's own credentials ------------------------------------------

    def test_overlapping_callbacks_for_one_site_make_one_install(self):
        first = self._authorize("jira")["query"]["state"]
        second = self._authorize("jira")["query"]["state"]
        self.vendors.codes["jira-code-2"] = "jira"
        real_existing = console_oauth._existing
        arrived: list[str] = []

        def slow_existing(source, account):
            found = real_existing(source, account)
            # Wait for the other callback to reach its own lookup too. Without the lock both get here and
            # both find nothing; with it the other is held at the lock, so this gives up after 2 s.
            arrived.append(account)
            deadline = time.monotonic() + 2.0
            while len(arrived) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            return found

        locations = []

        def callback(code, state):
            client = TestClient(self.client.app, base_url=PUBLIC)
            answer = client.get("/console/oauth/jira/callback", params={"code": code, "state": state}, headers={"Cookie": f"{COOKIE}={self.cookie}"}, follow_redirects=False)
            locations.append(answer.headers["location"])

        with mock.patch.object(console_oauth, "_existing", slow_existing):
            threads = [threading.Thread(target=callback, args=args) for args in (("jira-code", first), ("jira-code-2", second))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(locations, ["/console?connected=jira"] * 2)
        with tenancy.org_context(self.acme.id):
            self.assertEqual(len(store.list_installs("jira")), 1)
        self.assertIsNone(falkordb_client.kv().get(f"console:claim:jira:{CLOUD_ID}"))

    def test_reconnecting_an_install_being_removed_is_refused(self):
        install_id = self._connected("jira")
        before = store.read_secret(self.acme.id, install_id)
        with tenancy.org_context(self.acme.id):
            store.set_status(install_id, "removing")
        state = self._authorize("jira")["query"]["state"]
        self.assertEqual(self._callback("jira", code="jira-code", state=state).headers["location"], "/console?error=oauth_failed")
        with tenancy.org_context(self.acme.id):
            rows = store.list_installs("jira")
        self.assertEqual([(row.id, row.status) for row in rows], [(install_id, "removing")])
        self.assertEqual(store.read_secret(self.acme.id, install_id), before)

    def test_refused_app_credentials_are_the_operators_to_fix_not_a_reconnect(self):
        install = store.get_install(self.acme.id, self._connected("jira"))
        self._expire(self.acme.id, install)
        with mock.patch.dict(os.environ, {"JIRA_OAUTH_CLIENT_SECRET": "rotated-by-mistake"}):
            with self.assertRaises(ConnectorError) as raised:
                oauth.bearer_for(install)
        self.assertNotIsInstance(raised.exception, CredentialsRejected)
        self.assertIn("JIRA_OAUTH_CLIENT_SECRET", str(raised.exception))
        self.assertNotIn("rotated-by-mistake", str(raised.exception))
        self.assertNoSecretsLogged()

    # --- package A's real session ---------------------------------------------------------------

    def test_connect_and_callback_with_package_a_real_session(self):
        try:
            session_module = importlib.import_module("src.domain.console.session")
            csrf_module = importlib.import_module("src.domain.console.csrf")
        except ImportError:
            self.skipTest("package A's console session (#151) is not on this branch yet")
        live = console_oauth.settings
        with mock.patch.object(session_module, "settings", live), mock.patch.object(csrf_module, "settings", live), \
                mock.patch.object(console_oauth, "_admin_session", REAL_ADMIN_SESSION):
            sid, session = session_module.create(org=self.acme.id, person="person-maya", person_name="Maya", team="T1", slack_user="U1", role="owner")
            self.addCleanup(session_module.destroy, sid)
            headers = {"X-CSRF-Token": session.csrf, "Origin": PUBLIC, "Cookie": f"{COOKIE}={sid}"}
            answer = self.client.post("/console/api/connectors/jira/connect", headers=headers)
            self.assertEqual(answer.status_code, 200, answer.text)
            self.vendors.codes["jira-code"] = "jira"
            state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(answer.json()["authorize_url"]).query))["state"]
            # Another browser's session cannot finish it; the state is spent by the attempt.
            other_sid, _ = session_module.create(org=self.acme.id, person="person-maya", person_name="Maya", team="T1", slack_user="U1", role="owner")
            self.addCleanup(session_module.destroy, other_sid)
            self.assertEqual(self._callback("jira", cookie=other_sid, code="jira-code", state=state).headers["location"], "/console?error=oauth_state")
            answer = self.client.post("/console/api/connectors/jira/connect", headers=headers)
            state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(answer.json()["authorize_url"]).query))["state"]
            self.assertEqual(self._callback("jira", cookie=sid, code="jira-code", state=state).headers["location"], "/console?connected=jira")
            # A's own checks: a member may not connect, and a wrong CSRF token is refused before the route runs.
            self.assertEqual(self.client.post("/console/api/connectors/jira/connect", headers={**headers, "X-CSRF-Token": "wrong"}).status_code, 403)
            member_sid, member = session_module.create(org=self.acme.id, person="person-lena", person_name="Lena", team="T1", slack_user="U2", role="member")
            self.addCleanup(session_module.destroy, member_sid)
            answer = self.client.post("/console/api/connectors/jira/connect", headers={**headers, "X-CSRF-Token": member.csrf, "Cookie": f"{COOKIE}={member_sid}"})
            self.assertEqual(answer.status_code, 403)
        with tenancy.org_context(self.acme.id):
            self.assertEqual([row.created_by for row in store.list_installs("jira")], ["person-maya"])

    # --- refusals -------------------------------------------------------------------------------

    def test_a_state_works_once_for_its_own_session_and_org(self):
        state = self._authorize("jira")["query"]["state"]
        self.assertEqual(self._callback("jira", cookie="someone-else", code="jira-code", state=state).headers["location"], "/console?error=oauth_state")
        # The state was spent by that attempt.
        self.assertEqual(self._callback("jira", code="jira-code", state=state).headers["location"], "/console?error=oauth_state")

        state = self._authorize("jira")["query"]["state"]
        type(self).current = FakeSession(org=self.rival.id, id_hash=_hash(self.cookie))
        self.assertEqual(self._callback("jira", code="jira-code", state=state).headers["location"], "/console?error=oauth_state")

        type(self).current = FakeSession(org=self.acme.id, id_hash=_hash(self.cookie))
        state = self._authorize("jira")["query"]["state"]
        self.assertEqual(self._callback("hubspot", code="hubspot-code", state=state).headers["location"], "/console?error=oauth_state")
        self.assertEqual(self._callback("jira", code="jira-code", state="made-up").headers["location"], "/console?error=oauth_state")

    def test_vendor_errors_are_not_echoed(self):
        state = self._authorize("jira")["query"]["state"]
        answer = self._callback("jira", error="access_denied", error_description="<script>alert(1)</script>", state=state)
        self.assertEqual(answer.headers["location"], "/console?error=oauth_denied")
        state = self._authorize("jira")["query"]["state"]
        answer = self._callback("jira", code="wrong-code", state=state)
        self.assertEqual(answer.headers["location"], "/console?error=oauth_failed")
        self.assertEqual(answer.headers["cache-control"], "no-store")
        self.assertNoSecretsLogged()

    def test_an_account_feeds_one_org(self):
        self._connected("jira")
        type(self).current = FakeSession(org=self.rival.id, id_hash=_hash(self.cookie))
        state = self._authorize("jira")["query"]["state"]
        self.assertEqual(self._callback("jira", code="jira-code", state=state).headers["location"], "/console?error=account_claimed")
        with tenancy.org_context(self.rival.id):
            self.assertEqual(store.list_installs(), [])

    def test_connect_needs_csrf_a_browser_session_and_app_credentials(self):
        self.assertEqual(self._connect("jira", **{"X-CSRF-Token": "wrong"}).status_code, 403)
        self.assertEqual(self._connect("jira", Origin="https://evil.example").status_code, 403)
        type(self).current = FakeSession(org=self.acme.id, id_hash="", operator=True)
        self.assertEqual(self._connect("jira").json(), {"error": "operator_session"})
        type(self).current = FakeSession(org=self.acme.id, id_hash=_hash(self.cookie))
        self.assertEqual(self._connect("github").status_code, 404)
        with mock.patch.dict(os.environ, {"JIRA_OAUTH_CLIENT_SECRET": ""}):
            self.assertEqual(self._connect("jira").status_code, 409)
        with mock.patch.object(console_oauth, "settings", dataclasses.replace(console_oauth.settings, console_enabled=False)):
            self.assertEqual(self._connect("jira").status_code, 404)
            self.assertEqual(self._callback("jira", code="jira-code", state="x").status_code, 404)


if __name__ == "__main__":
    unittest.main()
