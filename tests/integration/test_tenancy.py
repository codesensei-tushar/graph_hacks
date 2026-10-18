"""Orgs: each org's graph is its own, and every entry point runs in the right one.

Two orgs are created for real (``org_<ulid>`` graphs and Org nodes in the test control
graph), and removed afterwards. The legacy org stays on the test's FALKORDB_GRAPH, and the
production graph name ``company`` is never touched.
"""

import hashlib
import hmac
import json
import time
import unittest
import uuid
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from fastapi.testclient import TestClient  # noqa: E402

from src.api.v1 import slack as slack_routes  # noqa: E402
from src.api.v1 import slack_install  # noqa: E402
from src.core import scheduler, tenancy  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.db import falkordb_client  # noqa: E402
from src.db.falkordb_client import connect, kv  # noqa: E402
from src.domain import orgs  # noqa: E402
from src.domain.slack import workspace as workspace_module  # noqa: E402
from src.domain.slack.lifecycle_api import SlackOAuthResult  # noqa: E402

ADMIN = "tenancy-admin-token"


def _strict(on: bool) -> None:
    object.__setattr__(settings, "tenancy_strict", on)


class TenancyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        orgs.ensure_control_indexes()
        cls.x = orgs.create_org("Tenancy X", created_by="TX:UX")
        cls.y = orgs.create_org("Tenancy Y", created_by="TY:UY")

    @classmethod
    def tearDownClass(cls):
        for org in (cls.x, cls.y):
            scheduler.cancel_org(org.id)
            try:
                falkordb_client.client().select_graph(org.graph).delete()
            except Exception:
                pass
        tenancy.control_graph().query("MATCH (o:Org) WHERE o.id IN $ids DETACH DELETE o", {"ids": [cls.x.id, cls.y.id]})
        tenancy.control_graph().query("MATCH (r:WorkspaceRoute) WHERE NOT (r)-[:OF]->() DELETE r")
        orgs.clear_route_cache()

    def tearDown(self):
        _strict(False)
        orgs.clear_route_cache()

    # --- the graph follows the org ---------------------------------------------------------

    def test_orgs_have_their_own_graphs_and_indexes(self):
        self.assertRegex(self.x.graph, r"^org_[0-9a-z]{26}$")
        self.assertNotEqual(self.x.graph, self.y.graph)
        self.assertEqual(orgs.get_org(self.x.id).graph, self.x.graph)
        self.assertEqual(orgs.get_org(self.x.id).slug, "tenancy-x")
        with tenancy.org_context(self.x.id):
            self.assertEqual(falkordb_client.graph_name(), self.x.graph)
            _, graph = connect()
            labels = {row[0] for row in graph.query("CALL db.indexes()").result_set}
        self.assertLessEqual({"Memory", "Ticket", "ConnectorState", "SlackUser"}, labels)

    def test_a_write_in_one_org_is_invisible_in_another_and_in_legacy(self):
        marker = f"tenancy-{uuid.uuid4().hex[:8]}"
        _strict(True)
        with tenancy.org_context(self.x.id):
            _, graph = connect()
            graph.query("CREATE (:Person {id: $id, name: 'Only In X'})", {"id": marker})
            self.assertEqual(graph.query("MATCH (p:Person {id: $id}) RETURN count(p)", {"id": marker}).result_set[0][0], 1)
        with tenancy.org_context(self.y.id):
            _, graph = connect()
            self.assertEqual(graph.query("MATCH (p:Person {id: $id}) RETURN count(p)", {"id": marker}).result_set[0][0], 0)
        with tenancy.org_context(tenancy.LEGACY_ORG_ID):
            self.assertEqual(falkordb_client.graph_name(), settings.falkordb_graph)
            _, graph = connect()
            self.assertEqual(graph.query("MATCH (p:Person {id: $id}) RETURN count(p)", {"id": marker}).result_set[0][0], 0)
        _strict(False)
        self.assertNotIn("company", {self.x.graph, self.y.graph, settings.falkordb_graph})
        if "company" in falkordb_client.client().list_graphs():
            production = falkordb_client.client().select_graph("company")
            self.assertEqual(production.ro_query("MATCH (p:Person {id: $id}) RETURN count(p)", {"id": marker}).result_set[0][0], 0)

    def test_no_org_in_context_raises_when_strict_and_falls_back_otherwise(self):
        self.assertIsNone(tenancy.current_org())
        self.assertEqual(falkordb_client.graph_name(), settings.falkordb_graph)
        _strict(True)
        with self.assertRaises(tenancy.NoOrgContext):
            falkordb_client.graph_name()
        with self.assertRaises(tenancy.NoOrgContext):
            connect()

    def test_contexts_nest_and_unknown_or_forged_orgs_are_refused(self):
        with tenancy.org_context(self.x.id):
            with tenancy.org_context(self.y.id):
                self.assertEqual(tenancy.current_org(), self.y.id)
            self.assertEqual(tenancy.current_org(), self.x.id)
        self.assertIsNone(tenancy.current_org())
        with self.assertRaises(LookupError):
            with tenancy.org_context("01nosuchorg"):
                pass
        with self.assertRaises(ValueError):
            with tenancy.org_context(tenancy.OrgRef(id="evil", graph="company")):
                pass

    def test_threads_carry_the_org_only_when_bound(self):
        import threading

        seen = {}
        with tenancy.org_context(self.x.id):
            plain = threading.Thread(target=lambda: seen.__setitem__("plain", tenancy.current_org()))
            bound = threading.Thread(target=tenancy.bind(lambda: seen.__setitem__("bound", tenancy.current_org())))
        plain.start(), bound.start(), plain.join(), bound.join()
        self.assertEqual(seen, {"plain": None, "bound": self.x.id})

    # --- the scheduler -----------------------------------------------------------------------

    def test_jobs_run_in_the_org_that_scheduled_them(self):
        ran = []
        name = f"tenancyProbe{uuid.uuid4().hex[:6]}"
        scheduler.register(name, lambda payload: ran.append((payload["n"], tenancy.current_org(), falkordb_client.graph_name())))
        job = f"probe:{uuid.uuid4().hex[:6]}"
        with tenancy.org_context(self.x.id):
            scheduler.schedule(0, name, {"n": "x"}, job_id=job)
            self.assertTrue(scheduler.exists(job))
        with tenancy.org_context(self.y.id):
            self.assertFalse(scheduler.exists(job))
            scheduler.schedule(0, name, {"n": "y"}, job_id=job)
        # The legacy org's ids are not prefixed, and its jobs carry no org: queued jobs keep working.
        scheduler.schedule(0, name, {"n": "legacy"}, job_id=job)
        self.assertIsNotNone(kv().zscore("brain:schedule", job))
        self.assertIsNotNone(kv().zscore("brain:schedule", f"{self.x.id}:{job}"))
        self.assertNotIn("org", json.loads(kv().hget("brain:schedule:payload", job)))
        scheduler.run_due(now=time.time() + 1)
        self.assertEqual(
            sorted(ran),
            sorted([("x", self.x.id, self.x.graph), ("y", self.y.id, self.y.graph), ("legacy", tenancy.LEGACY_ORG_ID, settings.falkordb_graph)]),
        )

    def test_cancel_matching_only_touches_its_own_org(self):
        name = f"tenancyCancel{uuid.uuid4().hex[:6]}"
        scheduler.register(name, lambda payload: None)
        with tenancy.org_context(self.x.id):
            scheduler.schedule(600, name, {}, job_id="c")
        with tenancy.org_context(self.y.id):
            scheduler.schedule(600, name, {}, job_id="c")
            self.assertEqual(scheduler.cancel_matching(name), 1)
        with tenancy.org_context(self.x.id):
            self.assertTrue(scheduler.exists("c"))
            scheduler.cancel("c")

    def test_a_job_of_a_deleted_org_is_dropped(self):
        ran = []
        name = f"tenancyGone{uuid.uuid4().hex[:6]}"
        scheduler.register(name, ran.append)
        kv().hset("brain:schedule:payload", "gone:j", json.dumps({"name": name, "payload": {}, "org": "01gone"}))
        kv().zadd("brain:schedule", {"gone:j": time.time() - 1})
        scheduler.run_due()
        self.assertEqual(ran, [])
        self.assertIsNone(kv().zscore("brain:schedule", "gone:j"))

    # --- Slack routing -----------------------------------------------------------------------

    def test_teams_route_to_their_org_and_unknown_teams_to_legacy(self):
        team = f"TROUTE{uuid.uuid4().hex[:6].upper()}"
        self.assertTrue(orgs.org_ref_for_team(team).legacy)
        orgs.route_team(team, self.x.id)
        self.assertEqual(orgs.org_ref_for_team(team).id, self.x.id)
        self.assertEqual(orgs.org_for_team(team).id, self.x.id)
        self.assertIn(team, orgs.teams_of(self.x.id))
        orgs.route_team(team, self.y.id)
        self.assertEqual(orgs.org_ref_for_team(team).id, self.y.id)
        orgs.unroute_team(team)
        self.assertTrue(orgs.org_ref_for_team(team).legacy)

    def _signed(self, body: dict) -> tuple[bytes, dict]:
        raw = json.dumps(body).encode()
        stamp = str(int(time.time()))
        signature = "v0=" + hmac.new(settings.slack_signing_secret.encode(), f"v0:{stamp}:".encode() + raw, hashlib.sha256).hexdigest()
        return raw, {"x-slack-request-timestamp": stamp, "x-slack-signature": signature, "content-type": "application/json"}

    def test_slack_events_resolve_the_team_before_any_graph_call(self):
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(slack_routes.router)
        team = f"TEVT{uuid.uuid4().hex[:6].upper()}"
        orgs.route_team(team, self.x.id)
        seen = []

        def lookup(team_id):
            seen.append((team_id, tenancy.current_org(), falkordb_client.graph_name()))
            return None

        for routed_team, expected in ((team, (self.x.id, self.x.graph)), ("TUNKNOWN", (tenancy.LEGACY_ORG_ID, settings.falkordb_graph))):
            seen.clear()
            body = {
                "type": "event_callback", "team_id": routed_team, "event_id": f"Ev{uuid.uuid4().hex}",
                "event": {"type": "message", "user": "U1", "channel": "C1", "channel_type": "channel", "text": "hi", "ts": "1.1"},
            }
            raw, headers = self._signed(body)
            with mock.patch.object(slack_routes, "get_workspace_by_team_id", side_effect=lookup):
                response = TestClient(app).post("/slack/events", content=raw, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(seen, [(routed_team, *expected)])

    def test_background_tasks_run_in_the_team_org(self):
        from fastapi import BackgroundTasks

        background = BackgroundTasks()
        tasks = slack_routes._OrgTasks(background, orgs.get_org_ref(self.x.id))
        seen = []
        tasks.add_task(lambda: seen.append(tenancy.current_org()))
        import asyncio

        asyncio.run(background())
        self.assertEqual(seen, [self.x.id])

    # --- the install callback writes the route -----------------------------------------------

    def _install(self, org_id: str, team: str):
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(slack_install.router)
        state = uuid.uuid4().hex
        kv().set(f"slack:oauth:{state}", json.dumps({"return_to": "http://test", "org": org_id}), ex=60)
        oauth = SlackOAuthResult(team_id=team, team_name="Routed Co", bot_token="xoxb-routed", bot_user_id="UBOT", authed_user_id="UADMIN", scopes="chat:write", app_id="A1")
        with mock.patch.object(slack_install, "exchange_slack_oauth", return_value=oauth), mock.patch.object(slack_install, "bootstrap_slack_workspace"), mock.patch.object(slack_install, "greet_slack_installer"):
            return TestClient(app).get(f"/slack/oauth/callback?code=c&state={state}")

    def test_install_callback_stores_the_workspace_in_the_org_and_routes_the_team(self):
        team = f"TINST{uuid.uuid4().hex[:6].upper()}"
        response = self._install(self.x.id, team)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(orgs.routed_org_id(team), self.x.id)
        with tenancy.org_context(self.x.id):
            self.assertIsNotNone(workspace_module.get_workspace_row(team))
        with tenancy.org_context(self.y.id):
            self.assertIsNone(workspace_module.get_workspace_row(team))
        self.assertIsNone(workspace_module.get_workspace_row(team))  # legacy
        # The same team cannot be installed into another org.
        orgs.clear_route_cache()
        self.assertEqual(self._install(self.y.id, team).status_code, 409)
        with tenancy.org_context(self.y.id):
            self.assertIsNone(workspace_module.get_workspace_row(team))
        with tenancy.org_context(self.x.id):
            workspace_module.teardown_slack_workspace(team, uninstall=lambda web: "revoked")
            self.assertIsNone(workspace_module.get_workspace_row(team))
        self.assertIsNone(orgs.routed_org_id(team))

    def test_install_is_refused_when_the_installer_does_not_belong_to_the_org(self):
        team = f"TREF{uuid.uuid4().hex[:6].upper()}"
        seen = []

        def refusal(team_id, user):
            # Called after the token is stored, as A's check reads the workspace to ask Slack.
            seen.append((team_id, user, tenancy.current_org(), workspace_module.get_workspace_row(team_id) is not None))
            return "not_a_member"

        with mock.patch.object(slack_install, "_membership_refusal", side_effect=refusal):
            self.assertEqual(self._install(self.x.id, team).status_code, 403)
        self.assertEqual(seen, [(team, "UADMIN", self.x.id, True)])
        self.assertIsNone(orgs.routed_org_id(team))
        with tenancy.org_context(self.x.id):
            self.assertIsNone(workspace_module.get_workspace_row(team))

    def test_deactivated_users_are_not_members(self):
        from src.integrations.slack.client import _slack_user_info_from_member

        user = _slack_user_info_from_member({"id": "UGONE", "name": "gone", "deleted": True, "team_id": "T1"})
        self.assertTrue(user.deleted)
        self.assertFalse(workspace_module.is_workspace_member(user))
        self.assertTrue(workspace_module.is_workspace_member(_slack_user_info_from_member({"id": "UHERE", "name": "here", "real_name": "Here"})))

    # --- the migration -------------------------------------------------------------------------

    def test_migration_routes_every_existing_workspace_to_the_legacy_org(self):
        from fastapi import FastAPI

        from src.api.v1 import org_ops

        app = FastAPI()
        app.include_router(org_ops.router)
        team, taken = f"TMIG{uuid.uuid4().hex[:6].upper()}", f"TTAKEN{uuid.uuid4().hex[:6].upper()}"
        workspace_module.upsert_workspace(team_id=team, bot_token_enc="plain:xoxb-1", team_name="Legacy Co")
        workspace_module.upsert_workspace(team_id=taken, bot_token_enc="plain:xoxb-2", team_name="Moved Co")
        orgs.route_team(taken, self.y.id)
        object.__setattr__(settings, "brain_admin_token", ADMIN)
        try:
            client = TestClient(app)
            self.assertEqual(client.post("/ops/orgs/migrate").status_code, 403)
            response = client.post("/ops/orgs/migrate", headers={"authorization": f"Bearer {ADMIN}"})
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertIn(team, body["routed"])
            self.assertIn(taken, body["skipped"])
            self.assertTrue(body["org"]["legacy"])
            self.assertEqual(orgs.routed_org_id(team), tenancy.LEGACY_ORG_ID)
            self.assertEqual(orgs.routed_org_id(taken), self.y.id)
            # Idempotent.
            self.assertEqual(client.post("/ops/orgs/migrate", headers={"authorization": f"Bearer {ADMIN}"}).status_code, 200)
            listed = client.get("/ops/orgs", headers={"authorization": f"Bearer {ADMIN}"}).json()["orgs"]
            self.assertIn(self.x.id, [org["id"] for org in listed])
        finally:
            object.__setattr__(settings, "brain_admin_token", "")
            _, graph = connect()
            graph.query("MATCH (w:SlackWorkspace) WHERE w.team_id IN $teams DETACH DELETE w", {"teams": [team, taken]})
            orgs.unroute_team(team)
            orgs.unroute_team(taken)

    def test_external_accounts_belong_to_one_org(self):
        key = f"jira:{uuid.uuid4().hex}"
        self.assertTrue(orgs.claim_external_account(key, self.x.id))
        self.assertTrue(orgs.claim_external_account(key, self.x.id))
        self.assertFalse(orgs.claim_external_account(key, self.y.id))
        self.assertEqual(orgs.external_account_org(key), self.x.id)
        orgs.release_external_account(key, self.x.id)
        self.assertTrue(orgs.claim_external_account(key, self.y.id))
        orgs.release_external_account(key, self.y.id)


if __name__ == "__main__":
    unittest.main()
