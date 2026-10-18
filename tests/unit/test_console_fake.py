"""The console fixtures match the spec's API shapes, and the fake API (dev only) behaves like the real one.

The console pages are built against these fixtures (console plan, "Console JSON API"), so a key
that drifts from the spec here would hide a mismatch until the real API lands. No graph is read:
``/console/api/ask`` is the only route that would, and it is not called.
"""

import dataclasses
import json
import os
import unittest
from pathlib import Path
from unittest import mock

import tests  # noqa: F401  pins the test settings before src loads

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api import console_fake, console_web  # noqa: E402
from src.core.config import settings  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "console"
ME = {"org", "person", "role", "csrf", "slack_connected"}
CONNECTION = {"id", "source", "status", "account", "site", "auth", "created_by", "created_at", "items", "last_sync_at", "last_error", "containers"}
# readers and available joined the contract in explorer round 4 (approved by main).
CONTAINER = {"id", "name", "items", "backfilled", "capped", "readers", "available", "last_fetch_at", "last_error"}
SLACK = {"team_id", "team_name", "bot_installed", "home_channel", "channels", "last_event_at", "install_url"}
AVAILABLE = {"external_id", "key", "name"}


class FixtureShapeTest(unittest.TestCase):
    def test_me(self):
        me = json.loads((FIXTURES / "me.json").read_text())
        self.assertEqual(set(me), ME)
        self.assertEqual(set(me["org"]), {"id", "name", "slug"})
        self.assertEqual(set(me["person"]), {"id", "name"})

    def test_connections_and_their_containers(self):
        for name in ("connections.json", "connections-attention.json"):
            for connection in json.loads((FIXTURES / name).read_text()):
                self.assertEqual(set(connection), CONNECTION, name)
                self.assertIn(connection["status"], {"connecting", "active", "error", "removing"})
                self.assertIn(connection["auth"], {"oauth", "token", "env"})
                for container in connection["containers"]:
                    self.assertEqual(set(container), CONTAINER, name)
                    self.assertTrue(container["id"].startswith(f"{connection['id']}:container:"))

    def test_slack(self):
        slack = json.loads((FIXTURES / "slack.json").read_text())
        self.assertEqual(set(slack), SLACK)
        self.assertEqual(set(slack["home_channel"]), {"id", "name"})
        for channel in slack["channels"]:
            self.assertEqual(set(channel), {"id", "name", "private"})

    def test_available(self):
        for item in json.loads((FIXTURES / "available.json").read_text()):
            self.assertEqual(set(item), AVAILABLE)


class FakeApiTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(console_web, "settings", dataclasses.replace(settings, console_enabled=True))
        patch.start()
        self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ, {"CONSOLE_FIXTURES": "1"})
        env.start()
        self.addCleanup(env.stop)
        console_fake._state.clear()
        app = FastAPI()
        console_web.install(app)
        self.http = TestClient(app)
        self.csrf = {"X-CSRF-Token": "fixture-csrf-token"}

    def pick(self, scenario: str):
        self.assertEqual(self.http.get(f"/console/_fixture/{scenario}", follow_redirects=False).status_code, 303)

    def test_mounted_only_with_both_flags(self):
        app = FastAPI()
        with mock.patch.dict(os.environ, {"CONSOLE_FIXTURES": ""}):
            console_web.install(app)
        self.assertEqual(TestClient(app).get("/console/api/me").status_code, 404)

    def test_backfill_moves_on_with_each_read(self):
        self.pick("default")
        first = self.http.get("/console/api/connections").json()
        second = self.http.get("/console/api/connections").json()
        done = lambda body: sum(c["backfilled"] for c in body[0]["containers"])  # noqa: E731
        self.assertLess(done(first), done(second))

    def test_mutations_need_the_csrf_token_and_an_admin(self):
        self.pick("default")
        self.assertEqual(self.http.post("/console/api/connectors/jira/connect").status_code, 403)
        url = self.http.post("/console/api/connectors/jira/connect", headers=self.csrf).json()["authorize_url"]
        self.assertTrue(url.startswith("/"))
        self.pick("member")
        self.assertEqual(self.http.post("/console/api/connectors/jira/connect", headers=self.csrf).status_code, 403)

    def test_jira_connect_goes_site_then_projects(self):
        self.pick("empty")
        screen = self.http.get("/console/_fake/oauth/jira", follow_redirects=False)
        self.assertTrue(
            screen.headers["location"].startswith("/console/connect/jira/site?id=jira%3A")
            or "/console/connect/jira/site?id=jira:" in screen.headers["location"]
        )
        connection_id = self.http.get("/console/api/connections").json()[0]["id"]
        cloud = self.http.get(f"/console/api/connections/{connection_id}/sites").json()[0]["cloud_id"]
        self.assertEqual(self.http.post(f"/console/api/connections/{connection_id}/site", json={"cloud_id": cloud}, headers=self.csrf).status_code, 200)
        picks = [a["external_id"] for a in self.http.get(f"/console/api/connections/{connection_id}/available").json()[:3]]
        self.assertEqual(self.http.put(f"/console/api/connections/{connection_id}/containers", json={"selected": picks}, headers=self.csrf).status_code, 200)
        connection = self.http.get("/console/api/connections").json()[0]
        self.assertEqual(connection["status"], "active")
        self.assertEqual([c["id"].rsplit(":", 1)[1] for c in connection["containers"]], picks)

    def test_disconnect_removes_after_a_few_reads(self):
        self.pick("default")
        connection_id = self.http.get("/console/api/connections").json()[1]["id"]
        self.assertEqual(self.http.delete(f"/console/api/connections/{connection_id}", headers=self.csrf).status_code, 200)
        statuses = [next((c["status"] for c in self.http.get("/console/api/connections").json() if c["id"] == connection_id), None) for _ in range(4)]
        self.assertEqual(statuses[0], "removing")
        self.assertIsNone(statuses[-1])

    def test_scenarios(self):
        self.pick("signedout")
        self.assertEqual(self.http.get("/console/api/me").status_code, 401)
        self.pick("new")
        self.assertIsNone(self.http.get("/console/api/me").json()["org"])
        bad = self.http.post("/console/api/orgs", json={"name": "Acme", "invite_code": "nope"}, headers=self.csrf)
        self.assertEqual((bad.status_code, bad.json()), (400, {"error": "invalid_invite"}))
        made = self.http.post("/console/api/orgs", json={"name": "Acme", "invite_code": "BETA-QUILL-2026"}, headers=self.csrf)
        # As the real route: 201 with the whole session, rotated, and the creator pending.
        self.assertEqual(made.status_code, 201)
        self.assertEqual(set(made.json()), ME)
        self.assertEqual((made.json()["org"]["name"], made.json()["role"]), ("Acme", "pending"))
        self.assertNotEqual(made.json()["csrf"], self.csrf["X-CSRF-Token"])
        self.http.get("/slack/oauth/install", follow_redirects=False)
        self.assertEqual(self.http.get("/console/api/me").json()["role"], "owner")
        self.pick("error")
        self.assertEqual(self.http.get("/console/api/connections").status_code, 503)
