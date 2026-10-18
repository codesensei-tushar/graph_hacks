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
        return [Container(id=install.container_id(key), name=key, external_id=pid) for pid, key in self.PROJECTS.items() if key in wanted]

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

    def test_mutations_need_the_csrf_token_and_origin(self):
        key = self.install.key
        body = {"selected": ["10"]}
        self.assertEqual(self.client.put(f"/console/api/connections/{key}/containers", json=body).status_code, 403)
        self.assertEqual(self.client.put(f"/console/api/connections/{key}/containers", json=body, headers={**self.headers, "x-csrf-token": "wrong"}).status_code, 403)
        self.assertEqual(self.client.put(f"/console/api/connections/{key}/containers", json=body, headers={**self.headers, "origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.post(f"/console/api/connections/{key}/sync", headers={"origin": ORIGIN}).status_code, 403)
        self.assertEqual(self.client.put(f"/console/api/connections/{key}/containers", content=b"selected=10", headers={**self.headers, "content-type": "application/x-www-form-urlencoded"}).status_code, 415)

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
        self.assertIn("user_p-ada", body["scopes"])  # the asker's own DM memory, as in Slack
        self.assertEqual(self.client.post("/console/api/ask", json={"q": " "}, headers=self.headers).status_code, 400)

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


if __name__ == "__main__":
    unittest.main()
