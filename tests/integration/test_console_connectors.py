"""Connector installs made in the console, end to end, in a real org with its own graph.

A fake source ("consolefake") is installed in org X through the store, its projects are
selected through the console API, and its sync runs through the real scheduler inside
org X. Nothing reaches the legacy graph or org Y. Disconnecting removes everything the
install wrote, in batches, then the install and its external account. Package A's
session dependencies are replaced by fakes (``dependency_overrides``).
"""

import time
import unittest
import uuid
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from cryptography.fernet import Fernet  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api.v1 import console_connectors  # noqa: E402
from src.connectors import registry, store, sync  # noqa: E402
from src.connectors.base import AvailableContainer, Connector, Container, InstallInfo, Item, Page  # noqa: E402
from src.core import scheduler, tenancy  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.db import falkordb_client  # noqa: E402
from src.db.falkordb_client import connect  # noqa: E402
from src.domain import orgs  # noqa: E402
from src.domain.knowledge import ask_api  # noqa: E402

ORIGIN = "https://console.test"
CSRF = "csrf-token-of-the-session"
SECRET = "consolefake-token-never-shown"


class ConsoleFake(Connector):
    source = "consolefake"
    required = ("token",)
    secret_fields = ("token",)
    container_field = "projects"
    poll_seconds = 600

    PROJECTS = {"10": "ALPHA", "20": "BETA", "30": "GAMMA"}

    def __init__(self):
        self.items_per_project = 3

    def check_credentials(self, install):
        assert install.get("token") == SECRET
        return InstallInfo(account="bot@consolefake.example", site="consolefake.example")

    def available_containers(self, install):
        assert install.get("token") == SECRET
        return [AvailableContainer(external_id=pid, key=key, name=f"{key} project") for pid, key in self.PROJECTS.items()]

    def containers(self, install):
        wanted = set(install.get_list("projects"))
        return [Container(id=install.container_id(pid), name=key, external_id=pid) for pid, key in self.PROJECTS.items() if key in wanted]

    def fetch(self, install, container, cursor):
        items = [
            Item(label="Ticket", id=install.item_id(f"{container.name}-{n}"), external_id=f"{container.name}-{n}", container=container.id, name=f"{container.name}-{n}", title=f"Issue {n}")
            for n in range(self.items_per_project)
        ]
        return Page(items=items, next_cursor="done")


FAKE = ConsoleFake()


class ConsoleConnectorsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        registry.register(FAKE)
        orgs.ensure_control_indexes()
        cls.x = orgs.create_org("Console X", created_by="TX:UX")
        cls.y = orgs.create_org("Console Y", created_by="TY:UY")
        cls.saved = {name: getattr(settings, name) for name in ("console_enabled", "public_url", "token_encryption_keys", "token_encryption_key")}
        for name, value in (("console_enabled", True), ("public_url", ORIGIN), ("token_encryption_keys", (Fernet.generate_key().decode(),)), ("token_encryption_key", "")):
            object.__setattr__(settings, name, value)
        cls.app = FastAPI()
        cls.app.include_router(console_connectors.router)

    @classmethod
    def tearDownClass(cls):
        for name, value in cls.saved.items():
            object.__setattr__(settings, name, value)
        for org in (cls.x, cls.y):
            scheduler.cancel_org(org.id)
            for key in [key for key in registry._installs if key[0] == org.id]:
                registry._installs.pop(key)
            try:
                falkordb_client.client().select_graph(org.graph).delete()
            except Exception:
                pass
        tenancy.control_graph().query("MATCH (o:Org) WHERE o.id IN $ids DETACH DELETE o", {"ids": [cls.x.id, cls.y.id]})
        registry._connectors.pop("consolefake", None)

    def setUp(self):
        self.session = {"org": self.x.id, "person": {"id": "p-ada", "name": "Ada"}, "role": "admin", "csrf": CSRF, "slack_user": "UADA", "team": "TX"}
        self.app.dependency_overrides[console_connectors.console_user] = lambda: self.session
        self.app.dependency_overrides[console_connectors.console_admin] = self._admin
        self.client = TestClient(self.app)
        self.headers = {"x-csrf-token": CSRF, "origin": ORIGIN}
        self.account = f"consolefake:{uuid.uuid4().hex[:8]}"
        self.install = store.create_install(
            self.x.id, "consolefake", None, "token", status="connecting", external_account=self.account,
            config={}, secret={"token": SECRET}, created_by="p-ada",
        )
        orgs.claim_external_account(self.account, self.x.id)

    def _admin(self):
        if self.session["role"] not in ("owner", "admin"):
            raise HTTPException(status_code=403, detail="admins only")
        return self.session

    def _run_scheduler(self, rounds: int = 6) -> None:
        for _ in range(rounds):
            scheduler.run_due(now=time.time() + 1)

    def _count_in(self, org, query: str, params: dict | None = None) -> int:
        with tenancy.org_context(org.id):
            _, graph = connect()
            return graph.query(query, params or {}).result_set[0][0]

    def test_connect_select_sync_and_disconnect_stay_inside_the_org(self):
        key = self.install.key
        rows = self.client.get("/console/api/connections").json()
        row = next(row for row in rows if row["id"] == key)
        self.assertEqual((row["status"], row["auth"], row["items"]), ("connecting", "token", 0))
        self.assertNotIn(SECRET, str(rows))

        available = self.client.get(f"/console/api/connections/{key}/available").json()
        self.assertEqual([item["key"] for item in available], ["ALPHA", "BETA", "GAMMA"])

        response = self.client.put(f"/console/api/connections/{key}/containers", json={"selected": ["10", "30"]}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        with tenancy.org_context(self.x.id):
            stored = store.get(key)
            self.assertEqual((stored.status, stored.config["projects"]), ("active", "ALPHA,GAMMA"))
            self.assertTrue(scheduler.exists(f"connector:sync:{self.x.id}:consolefake:{self.install.id}"))
        self.assertIsNotNone(registry.get_install(self.x.id, "consolefake", self.install.id))

        self._run_scheduler()
        self.assertEqual(self._count_in(self.x, "MATCH (t:Ticket {install: $k}) RETURN count(t)", {"k": key}), 6)
        self.assertEqual(self._count_in(self.y, "MATCH (t:Ticket {install: $k}) RETURN count(t)", {"k": key}), 0)
        legacy = falkordb_client.client().select_graph(settings.falkordb_graph)
        self.assertEqual(legacy.query("MATCH (n {install: $k}) RETURN count(n)", {"k": key}).result_set[0][0], 0)

        row = next(row for row in self.client.get("/console/api/connections").json() if row["id"] == key)
        self.assertEqual((row["status"], row["items"], row["account"]), ("active", 6, "bot@consolefake.example"))
        self.assertEqual(sorted(container["name"] for container in row["containers"]), ["ALPHA", "GAMMA"])

        # Org Y sees none of it.
        self.session = {**self.session, "org": self.y.id}
        self.assertNotIn(key, [row["id"] for row in self.client.get("/console/api/connections").json()])
        self.assertEqual(self.client.get(f"/console/api/connections/{key}/available").status_code, 404)
        self.assertEqual(self.client.delete(f"/console/api/connections/{key}", headers=self.headers).status_code, 404)
        self.session = {**self.session, "org": self.x.id}

        # Disconnect: removing at once, then batched deletion through the scheduler.
        with mock.patch.object(sync, "REMOVE_BATCH", 4), mock.patch.object(sync, "_audit") as audit:
            response = self.client.delete(f"/console/api/connections/{key}", headers=self.headers)
            self.assertEqual(response.status_code, 202)
            with tenancy.org_context(self.x.id):
                self.assertEqual(store.get(key).status, "removing")
                self.assertFalse(scheduler.exists(f"connector:sync:{self.x.id}:consolefake:{self.install.id}"))
            self.assertIsNone(registry.get_install(self.x.id, "consolefake", self.install.id))
            self._run_scheduler(10)
        self.assertEqual(self._count_in(self.x, "MATCH (n {install: $k}) RETURN count(n)", {"k": key}), 0)
        with tenancy.org_context(self.x.id):
            self.assertIsNone(store.get(key))
        self.assertIsNone(orgs.external_account_org(self.account))
        self.assertIn("connector.disconnect", [call.args[2] for call in audit.call_args_list])

    def test_containers_say_who_reads_them_and_what_the_vendor_holds(self):
        """Per container: ``readers`` null (company-wide) or N (restricted to N readers), and ``available``,
        the vendor's own total when its listing gives one (null otherwise)."""
        from src.connectors import graph as connector_graph
        from src.connectors.base import ExternalUser

        key = self.install.key
        readers = [ExternalUser(self.install.user_id(f"acc{n}"), "consolefake", f"acc{n}", f"Reader {n}", None) for n in range(2)]

        def containers(install):
            wanted = set(install.get_list("projects"))
            listed = [
                Container(id=install.container_id("10"), name="ALPHA", external_id="10", available=42),
                Container(id=install.container_id("30"), name="GAMMA", external_id="30", restricted=True),
            ]
            return [c for c in listed if c.name in wanted]

        with mock.patch.object(FAKE, "containers", containers), mock.patch.object(FAKE, "readers", lambda install, container: readers, create=True):
            with tenancy.org_context(self.x.id):
                for user in readers:
                    connector_graph.upsert_user(self.install, user)
            self.client.put(f"/console/api/connections/{key}/containers", json={"selected": ["10", "30"]}, headers=self.headers)
            self._run_scheduler()
        row = next(row for row in self.client.get("/console/api/connections").json() if row["id"] == key)
        got = {c["name"]: (c["readers"], c["available"]) for c in row["containers"]}
        self.assertEqual(got, {"ALPHA": (None, 42), "GAMMA": (2, None)})
        available = self.client.get(f"/console/api/connections/{key}/available").json()
        self.assertEqual(set(available[0]), {"external_id", "key", "name", "available"})

    def test_slack_card_is_the_sessions_own_workspace(self):
        from src.domain.slack import channel_membership
        from src.domain.slack import workspace as workspace_module
        from src.domain.slack.token_crypto import encrypt_token
        from src.integrations.slack.client import SlackConversation

        class Bot:
            calls = 0

            def get_bot_conversations(self, max_channels=1000, require_complete=False):
                Bot.calls += 1
                return [SlackConversation("CGEN", "general", False), SlackConversation("GLEAD", "leads", True), SlackConversation("GHR", "hr", True)]

        team = f"TX{uuid.uuid4().hex[:6].upper()}"
        self.session = {**self.session, "team": team, "role": "owner"}
        with tenancy.org_context(self.x.id):
            workspace_module.upsert_workspace(team_id=team, bot_token_enc=encrypt_token("xoxb-x"), bot_user_id="UBOT", team_name="Console X", installed_by="UADA")
            workspace_module.set_home_channel(team, "CGEN")
            channel_membership.record_membership("GLEAD", "UADA")
        falkordb_client.kv().set(f"slack:last_event:{team}", 1_760_000_000)
        with mock.patch.object(workspace_module, "_client_for", return_value=Bot()), mock.patch.object(type(settings), "slack_oauth_enabled", True):
            card = self.client.get("/console/api/slack").json()
            again = self.client.get("/console/api/slack").json()
        self.assertEqual(Bot.calls, 1, "the channel list is cached")
        self.assertEqual(card, again)
        self.assertEqual(
            card,
            {
                "team_id": team, "team_name": "Console X", "bot_installed": True, "home_channel": {"id": "CGEN", "name": "general"},
                # A private channel shows only to someone in it: UADA is in #leads, not #hr.
                "channels": [{"id": "CGEN", "name": "general", "private": False}, {"id": "GLEAD", "name": "leads", "private": True}],
                "last_event_at": "2025-10-09T08:53:20Z", "install_url": "/slack/oauth/install",
            },
        )
        self.assertNotIn("xoxb", str(card))
        # A member gets no install link; another org's session sees its own (absent) workspace.
        self.session = {**self.session, "role": "member"}
        with mock.patch.object(workspace_module, "_client_for", return_value=Bot()):
            self.assertIsNone(self.client.get("/console/api/slack").json()["install_url"])
        self.session = {**self.session, "org": self.y.id, "team": "TNOBOT"}
        self.assertEqual(
            self.client.get("/console/api/slack").json(),
            {"team_id": "TNOBOT", "team_name": None, "bot_installed": False, "home_channel": None, "channels": [], "last_event_at": None, "install_url": None},
        )

    def test_a_step_finishing_after_disconnect_does_not_cancel_the_removal(self):
        key = self.install.key
        self.client.put(f"/console/api/connections/{key}/containers", json={"selected": ["10"]}, headers=self.headers)
        self._run_scheduler(3)
        # A sync step is already running on the scheduler thread when DELETE arrives...
        self.assertEqual(self.client.delete(f"/console/api/connections/{key}", headers=self.headers).status_code, 202)
        with tenancy.org_context(self.x.id):
            # ...and that step ends by recording its outcome, and by trying to re-arm itself.
            sync._record_status(self.install, {"status": "synced"})
            sync._record_status(self.install, {"status": "credentials_rejected"})
            self.assertEqual(store.get(key).status, "removing")
            sync.run_sync({"source": "consolefake", "install": self.install.id})
            self.assertFalse(scheduler.exists(f"connector:sync:{self.x.id}:consolefake:{self.install.id}"))
        self._run_scheduler(4)
        with tenancy.org_context(self.x.id):
            self.assertIsNone(store.get(key))
        self.assertEqual(self._count_in(self.x, "MATCH (n {install: $k}) RETURN count(n)", {"k": key}), 0)

    def test_selecting_projects_while_a_disconnect_lands_does_not_bring_the_install_back(self):
        key = self.install.key
        real_put = registry.put

        def put_then_disconnect(install):
            real_put(install)
            # DELETE lands between the containers route's checks and its registry.put.
            with tenancy.org_context(self.x.id):
                sync.remove_install(key, actor="p-ada")
            return install

        with mock.patch.object(registry, "put", side_effect=put_then_disconnect):
            response = self.client.put(f"/console/api/connections/{key}/containers", json={"selected": ["10"]}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(registry.get_install(self.x.id, "consolefake", self.install.id))
        with tenancy.org_context(self.x.id):
            self.assertEqual(store.get(key).status, "removing")
            self.assertFalse(scheduler.exists(f"connector:sync:{self.x.id}:consolefake:{self.install.id}"))
            self.assertFalse(store.set_status(key, "active"))

    def test_a_connect_left_without_a_site_is_discarded(self):
        """A second Jira connect whose consent covered several sites, abandoned at the site picker: no site,
        no account. The card can discard it at once, and the listing drops it once the OAuth state is dead."""
        with tenancy.org_context(self.x.id):
            fresh = store.create_install(self.x.id, "jira", None, "oauth", status="connecting", secret={"access_token": "a", "refresh_token": "r"})
            stale = store.create_install(self.x.id, "jira", None, "oauth", status="connecting", secret={"access_token": "a", "refresh_token": "r"})
        ids = [row["id"] for row in self.client.get("/console/api/connections").json()]
        self.assertIn(fresh.key, ids)
        answer = self.client.delete(f"/console/api/connections/{fresh.key}", headers=self.headers)
        self.assertEqual(answer.json(), {"ok": True, "status": "removed"})
        later = time.time() + console_connectors.ABANDONED_SECONDS + 5
        with mock.patch.object(console_connectors.time, "time", return_value=later):
            ids = [row["id"] for row in self.client.get("/console/api/connections").json()]
        self.assertNotIn(stale.key, ids)
        with tenancy.org_context(self.x.id):
            self.assertIsNone(store.get(fresh.key))
            self.assertIsNone(store.get(stale.key))
            self.assertIsNotNone(store.get(self.install.key))  # a connection with an account is never swept

    def test_bodies_must_be_json(self):
        # CSRF (Origin, X-CSRF-Token) is checked by console_user/console_admin themselves.
        key = self.install.key
        response = self.client.put(f"/console/api/connections/{key}/containers", content=b"selected=10", headers={**self.headers, "content-type": "application/x-www-form-urlencoded"})
        self.assertEqual(response.status_code, 415)

    def test_jira_sites_come_from_package_d_in_the_ui_shape(self):
        from types import SimpleNamespace

        with tenancy.org_context(self.x.id):
            jira = store.create("jira", auth="oauth", created_by="p-ada", secrets={"access_token": "a"})
        calls = []
        fake = SimpleNamespace(
            sites_for=lambda install: calls.append(("sites", install.key, tenancy.current_org())) or [SimpleNamespace(cloud_id="c1", name="One", url="https://one.atlassian.net")],
            choose_site=lambda org, key, cloud_id: calls.append(("choose", org, key, cloud_id)) or (key if cloud_id == "c1" else (_ for _ in ()).throw(PermissionError("claimed"))),
        )
        try:
            with mock.patch.object(console_connectors, "_jira_oauth", return_value=fake):
                sites = self.client.get(f"/console/api/connections/{jira.id}/sites").json()
                self.assertEqual(sites, [{"cloud_id": "c1", "name": "One", "url": "https://one.atlassian.net"}])
                chosen = self.client.post(f"/console/api/connections/{jira.id}/site", json={"cloud_id": "c1"}, headers=self.headers)
                self.assertEqual(chosen.json(), {"ok": True, "id": jira.id})
                self.assertEqual(self.client.post(f"/console/api/connections/{jira.id}/site", json={"cloud_id": "c2"}, headers=self.headers).status_code, 409)
                self.assertEqual(self.client.get(f"/console/api/connections/{self.install.key}/sites").status_code, 400)
            self.assertEqual(calls[0], ("sites", jira.id, self.x.id))
            self.assertEqual(calls[1], ("choose", self.x.id, jira.id, "c1"))
            with mock.patch.object(console_connectors, "_jira_oauth", return_value=None):
                self.assertEqual(self.client.get(f"/console/api/connections/{jira.id}/sites").status_code, 503)
        finally:
            with tenancy.org_context(self.x.id):
                store.delete(jira.id)

    def test_rearm_makes_a_backed_off_install_due_now(self):
        install = store.to_install(self._stored())
        registry.put(install)
        with tenancy.org_context(self.x.id):
            from src.connectors import graph as connector_graph

            connector_graph.set_state(install.container_id("10"), install, due_at=int(time.time() * 1000) + 3_600_000, failures=3)
            connector_graph.set_state(install.key, install, failures=3, last_error="rejected")
            sync.schedule("consolefake", 3600, install)
            sync.rearm(install)
            self.assertLess(scheduler.due_at(f"connector:sync:{self.x.id}:consolefake:{self.install.id}"), time.time() + 1)
            self.assertEqual(connector_graph.get_state(install.container_id("10"))["due_at"], 0)
            self.assertNotIn("last_error", connector_graph.get_state(install.key))

    def _stored(self):
        with tenancy.org_context(self.x.id):
            return store.get(self.install.key)

    def test_removal_keeps_an_account_another_install_still_holds(self):
        with tenancy.org_context(self.x.id):
            twin = store.create("consolefake", auth="token", created_by="p-ada", external_account=self.account, secrets={"token": SECRET})
        try:
            self.assertEqual(self.client.delete(f"/console/api/connections/{self.install.key}", headers=self.headers).status_code, 202)
            self._run_scheduler(5)
            with tenancy.org_context(self.x.id):
                self.assertIsNone(store.get(self.install.key))
            self.assertEqual(orgs.external_account_org(self.account), self.x.id)
        finally:
            with tenancy.org_context(self.x.id):
                store.delete(twin.id)

    def test_members_view_but_do_not_change(self):
        self.session = {**self.session, "role": "member"}
        self.assertEqual(self.client.get("/console/api/connections").status_code, 200)
        self.assertEqual(self.client.get(f"/console/api/connections/{self.install.key}/available").status_code, 403)
        self.assertEqual(self.client.delete(f"/console/api/connections/{self.install.key}", headers=self.headers).status_code, 403)

    def test_selection_is_checked_against_what_is_available(self):
        response = self.client.put(f"/console/api/connections/{self.install.key}/containers", json={"selected": ["99"]}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["unknown"], ["99"])

    def test_console_off_is_404(self):
        object.__setattr__(settings, "console_enabled", False)
        try:
            self.assertEqual(self.client.get("/console/api/connections").status_code, 404)
        finally:
            object.__setattr__(settings, "console_enabled", True)

    def test_secrets_are_encrypted_and_bound_to_their_org_and_install(self):
        with tenancy.org_context(self.x.id):
            _, graph = connect()
            stored_value = graph.query("MATCH (c:ConnectorInstall {id: $id}) RETURN c.secret_enc", {"id": self.install.key}).result_set[0][0]
            self.assertTrue(stored_value.startswith("fernet:"))
            self.assertNotIn(SECRET, stored_value)
            self.assertEqual(store.read_secret(self.x.id, self.install.key), {"token": SECRET})
            # The same value copied onto another install is refused.
            other = store.create("consolefake", auth="token", created_by="p-ada")
            graph.query("MATCH (c:ConnectorInstall {id: $id}) SET c.secret_enc = $value", {"id": other.id, "value": stored_value})
            with self.assertRaises(ValueError):
                store.load_secrets(store.get(other.id))
            store.delete(other.id)
        # And into another org's graph.
        with tenancy.org_context(self.y.id):
            copy = store.create("consolefake", auth="token", created_by="p-ada", install=self.install.id)
            _, graph = connect()
            graph.query("MATCH (c:ConnectorInstall {id: $id}) SET c.secret_enc = $value", {"id": copy.id, "value": stored_value})
            with self.assertRaises(ValueError):
                store.load_secrets(store.get(copy.id))
            store.delete(copy.id)

    def test_load_from_graph_puts_stored_installs_and_drops_removed_ones(self):
        registry.drop(self.x.id, "consolefake", self.install.id)
        loaded = registry.load_from_graph(self.x.id)
        self.assertIn(self.install.key, [install.key for install in loaded if install.org == self.x.id])
        install = registry.get_install(self.x.id, "consolefake", self.install.id)
        self.assertEqual((install.org, install.auth, install.get("token")), (self.x.id, "token", SECRET))
        self.assertNotIn(SECRET, repr(install))
        with tenancy.org_context(self.x.id):
            store.set_status(self.install.key, "removing")
        registry.load_from_graph(self.x.id)
        self.assertIsNone(registry.get_install(self.x.id, "consolefake", self.install.id))

    def test_ask_reads_the_askers_own_scopes(self):
        response = self.client.post("/console/api/ask", json={"q": "who owns billing"}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("org_internal", body["scopes"])
        self.assertIn("org_shared", body["scopes"])
        self.assertEqual(body["picked_by"], "keywords")  # no model in tests; members never get it
        # The asker's own DM memory, under the tag a Slack DM turn writes it to (keyed by the Slack user id).
        from src.domain.memory.writeback import slack_memory_container_tag, slack_memory_scope_for_turn

        dm_tag = slack_memory_container_tag(slack_memory_scope_for_turn(is_dm=True, channel="D1", channel_type="im", user_id="UADA", slack_user_id="UADA"))
        self.assertEqual(dm_tag, "user_UADA")
        self.assertIn(dm_tag, body["scopes"])
        self.assertNotIn("user_p-ada", body["scopes"])
        self.assertEqual(self.client.post("/console/api/ask", json={"q": " "}, headers=self.headers).status_code, 400)

    def test_after_a_disconnect_an_answer_shows_none_of_its_data(self):
        key = self.install.key
        self.client.put(f"/console/api/connections/{key}/containers", json={"selected": ["10"]}, headers=self.headers)
        self._run_scheduler()

        def ask() -> str:
            response = self.client.post("/console/api/ask", json={"q": "what is the status of ALPHA-1"}, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            return str(response.json())

        self.assertIn("Issue 1", ask())
        self.assertIn("Issue 1", ask())  # asked twice: the second is not a stale copy either way
        with mock.patch.object(sync, "_audit"):
            self.assertEqual(self.client.delete(f"/console/api/connections/{key}", headers=self.headers).status_code, 202)
            self._run_scheduler(10)
        self.assertEqual(self._count_in(self.x, "MATCH (n {install: $k}) RETURN count(n)", {"k": key}), 0)
        self.assertNotIn("Issue 1", ask())

    def test_a_write_to_the_graph_changes_the_public_cache_key(self):
        with tenancy.org_context(self.x.id):
            before = ask_api.cache_key("q")
            self.assertEqual(ask_api.cache_key("q"), before)
            _, graph = connect()
            graph.query("CREATE (:Note {id: 'cache-key-probe'})")
            after = ask_api.cache_key("q")
            graph.query("MATCH (n:Note {id: 'cache-key-probe'}) DELETE n")
        self.assertNotEqual(after, before)
        self.assertEqual(after[0], self.x.graph)

    def tearDown(self):
        with tenancy.org_context(self.x.id):
            if store.get(self.install.key) is not None:
                sync.cancel(registry.get_install(self.x.id, "consolefake", self.install.id) or store.to_install(store.get(self.install.key)))
                for _ in range(20):
                    if not sync.delete_batch(self.install.key):
                        break
                store.delete(self.install.key)
        registry.drop(self.x.id, "consolefake", self.install.id)
        orgs.release_external_account(self.account, self.x.id)


class OrphanedInstallTest(unittest.TestCase):
    """Data an install wrote stays after its configuration is gone; the operator sees and purges it."""

    def test_orphans_are_listed_and_purged_in_batches(self):
        from src.api.v1 import connectors as connectors_route
        from src.connectors import graph as connector_graph
        from src.connectors.base import Install

        org = orgs.create_org("Orphan Org", created_by="TO:UO")
        app = FastAPI()
        app.include_router(connectors_route.router)
        saved = settings.brain_admin_token
        object.__setattr__(settings, "brain_admin_token", "orphan-admin")
        headers = {"authorization": "Bearer orphan-admin"}
        try:
            gone = Install(source="jira", id="old", org=org.id)
            with tenancy.org_context(org.id):
                connector_graph.write_containers(gone, [Container(id=gone.container_id("1"), name="OLD", external_id="1")])
                connector_graph.set_state(gone.key, gone, account="old@example.com")
                _, graph = connect()
                for n in range(7):
                    graph.query("CREATE (:Ticket {id: $id, install: $key, scope: 'org_internal'})", {"id": gone.item_id(n), "key": gone.key})
            client = TestClient(app)
            body = client.get(f"/ops/connectors?org={org.id}", headers=headers).json()
            self.assertEqual([row["install"] for row in body["orphaned"]], ["jira:old"])
            self.assertEqual(body["orphaned"][0]["nodes"], 9)
            self.assertEqual(client.delete(f"/ops/connectors/orphans/jira:nothing?org={org.id}", headers=headers).status_code, 409)
            with mock.patch.object(sync, "REMOVE_BATCH", 4), mock.patch.object(sync, "_audit"):
                self.assertEqual(client.delete(f"/ops/connectors/orphans/jira:old?org={org.id}", headers=headers).status_code, 202)
                for _ in range(5):
                    scheduler.run_due(now=time.time() + 1)
            with tenancy.org_context(org.id):
                _, graph = connect()
                self.assertEqual(graph.query("MATCH (n {install: 'jira:old'}) RETURN count(n)").result_set[0][0], 0)
            self.assertEqual(client.get(f"/ops/connectors?org={org.id}", headers=headers).json()["orphaned"], [])
        finally:
            object.__setattr__(settings, "brain_admin_token", saved)
            scheduler.cancel_org(org.id)
            falkordb_client.client().select_graph(org.graph).delete()
            tenancy.control_graph().query("MATCH (o:Org {id: $id}) DETACH DELETE o", {"id": org.id})


if __name__ == "__main__":
    unittest.main()
