"""Install / identity / member lifecycle against real FalkorDB, with a fake Slack.

Uses its own throwaway graph by patching the graph name (settings are frozen at
import, and other test modules set environment variables), and deletes it after.
Needs FalkorDB on localhost:6379 (``docker compose up -d``).
"""

import json
import unittest
import uuid
from unittest import mock

from slack_sdk.errors import SlackApiError

from src.core import scheduler
from src.db import falkordb_client
from src.db.falkordb_client import kv
from src.domain.slack import account_link, team_invite
from src.domain.slack import workspace as ws_module
from src.domain.slack.member_provisioning import provision_slack_workspace_member, revoke_slack_workspace_member
from src.domain.slack.token_crypto import encrypt_token
from src.domain.slack.workspace import (
    get_org_actor_by_slack_identity,
    get_workspace_by_team_id,
    get_workspace_member,
    set_home_channel,
    teardown_slack_workspace,
    upsert_slack_workspace_member,
    upsert_workspace,
)
from src.integrations.slack.client import SlackUserInfo

GRAPH = f"company_test_lifecycle_{uuid.uuid4().hex[:6]}"


class _Response:
    def __init__(self, data, status_code=200, headers=None):
        self.data = data
        self.status_code = status_code
        self.headers = headers or {}


class FakeWeb:
    def __init__(self, users: dict[str, dict]):
        self.users = users
        self.calls: list[tuple[str, dict]] = []
        self.ts = 1_900_000_000

    def posts(self, method="chat.postMessage"):
        return [body for name, body in self.calls if name == method]

    def api_call(self, method, json=None, **_):
        body = json or {}
        self.calls.append((method, body))
        if method == "users.info":
            user = self.users.get(body["user"])
            if not user:
                raise SlackApiError("x", _Response({"ok": False, "error": "user_not_found"}))
            return _Response({"ok": True, "user": user})
        if method == "users.list":
            return _Response({"ok": True, "members": list(self.users.values())})
        if method == "conversations.open":
            return _Response({"ok": True, "channel": {"id": f"D{body['users']}"}})
        if method in ("chat.postMessage", "chat.update"):
            self.ts += 1
            return _Response({"ok": True, "ts": f"{self.ts}.000100"})
        return _Response({"ok": True})


class FakeClient:
    def __init__(self, web: FakeWeb):
        self.web = web

    def user_info(self, user_id):
        raw = self.web.users.get(user_id)
        if not raw:
            return None
        return SlackUserInfo(
            id=user_id,
            real_name=raw.get("real_name"),
            email=(raw.get("profile") or {}).get("email"),
            is_bot=bool(raw.get("is_bot")),
            is_restricted=bool(raw.get("is_restricted")),
        )

    def bot_user_id(self):
        return "UBOT"


def _user(user_id: str, email: str | None, **flags) -> dict:
    return {"id": user_id, "name": user_id.lower(), "real_name": f"Person {user_id}", "profile": {"email": email}, **flags}


class WorkspaceLifecycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._graph_patch = mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH)
        cls._graph_patch.start()

    @classmethod
    def tearDownClass(cls):
        _, graph = falkordb_client.connect()
        try:
            graph.delete()
        finally:
            cls._graph_patch.stop()

    def setUp(self):
        _, graph = falkordb_client.connect()
        graph.query("MATCH (n) DETACH DELETE n")
        graph.query("CREATE (:Person {id: 'p-ada', name: 'Ada Park', email: 'ada@quillfern.example.com'})")
        self.team = f"T{uuid.uuid4().hex[:8].upper()}"
        self.web = FakeWeb(
            {
                "UADA": _user("UADA", "ADA@quillfern.example.com"),
                "UNEW": _user("UNEW", "new@quillfern.example.com"),
                "UGUEST": _user("UGUEST", "guest@elsewhere.example.com", is_restricted=True),
                "UBOT": _user("UBOT", None, is_bot=True),
            }
        )
        self._client_patch = mock.patch.object(ws_module, "_client_for", return_value=FakeClient(self.web))
        self._client_patch.start()
        upsert_workspace(team_id=self.team, bot_token_enc=encrypt_token("xoxb-test"), bot_user_id="UBOT", team_name="Quillfern", installed_by="UADA")

    def tearDown(self):
        team_invite._reset_team_invite_state(self.team)
        self._client_patch.stop()

    def test_stored_install_resolves_as_oauth_workspace(self):
        workspace = get_workspace_by_team_id(self.team)
        self.assertEqual((workspace.source, workspace.team_name, workspace.installed_by), ("oauth", "Quillfern", "UADA"))
        self.assertIsNone(get_workspace_by_team_id("TNOPE"))

    def test_email_bootstrap_becomes_a_durable_mapping(self):
        first = get_org_actor_by_slack_identity(self.team, "UADA", "ADA@quillfern.example.com")
        self.assertEqual((first.lookup, first.actor.person_id), ("email_matched", "p-ada"))
        again = get_org_actor_by_slack_identity(self.team, "UADA", None)
        self.assertEqual((again.lookup, again.actor.person_id), ("mapped", "p-ada"))
        _, graph = falkordb_client.connect()
        self.assertEqual(graph.query("MATCH (:SlackUser {id: 'UADA'})-[:IS]->(p:Person) RETURN p.id").result_set, [["p-ada"]])
        self.assertEqual(get_org_actor_by_slack_identity(self.team, "UX", "nobody@x.example").lookup, "not_in_org")
        self.assertEqual(get_org_actor_by_slack_identity(self.team, "UX", None).lookup, "no_slack_email")

    def test_confirmed_link_is_not_overwritten_by_email_match(self):
        _, graph = falkordb_client.connect()
        graph.query("CREATE (:Person {id: 'p-other', name: 'Other', email: 'other@quillfern.example.com'})")
        upsert_slack_workspace_member(team_id=self.team, slack_user_id="UADA", person_id="p-ada", email=None, link_source="web_confirmed")
        upsert_slack_workspace_member(team_id=self.team, slack_user_id="UADA", person_id="p-other", email=None, link_source="email_match")
        member = get_workspace_member(self.team, "UADA")
        self.assertEqual((member["person_id"], member["link_source"]), ("p-ada", "web_confirmed"))

    def test_provision_then_revoke_only_undoes_what_slack_granted(self):
        created = provision_slack_workspace_member(team_id=self.team, slack_user_id="UNEW", email="new@quillfern.example.com", name="New Person")
        self.assertTrue(created.created_person and created.created_membership)
        self.assertEqual(get_org_actor_by_slack_identity(self.team, "UNEW").lookup, "mapped")
        self.assertTrue(revoke_slack_workspace_member(team_id=self.team, slack_user_id="UNEW"))
        self.assertEqual(get_org_actor_by_slack_identity(self.team, "UNEW").lookup, "mapped_not_in_org")
        _, graph = falkordb_client.connect()
        self.assertEqual(graph.query("MATCH (p:Person {id: $id}) RETURN p.org_member", {"id": created.person_id}).result_set, [[False]])

        existing = provision_slack_workspace_member(team_id=self.team, slack_user_id="UADA", email="ada@quillfern.example.com")
        self.assertEqual((existing.person_id, existing.created_person, existing.created_membership), ("p-ada", False, False))
        revoke_slack_workspace_member(team_id=self.team, slack_user_id="UADA")
        self.assertEqual(graph.query("MATCH (p:Person {id: 'p-ada'}) RETURN p.org_member").result_set, [[True]])

    def test_resolve_actor_provisions_full_members_and_denies_guests(self):
        workspace = get_workspace_by_team_id(self.team)
        actor = ws_module.resolve_actor(workspace, "UNEW")
        self.assertEqual((actor.lookup, actor.is_member), ("provisioned", True))
        self.assertIsNotNone(actor.person_id)
        self.assertFalse(ws_module.resolve_actor(workspace, "UGUEST").is_member)

    def test_account_link_is_single_use_and_rejects_unknown_people(self):
        provision_slack_workspace_member(team_id=self.team, slack_user_id="UNEW", email="new@quillfern.example.com")
        url = account_link.create_slack_account_link_url(team_id=self.team, slack_user_id="UNEW", slack_email="new@quillfern.example.com")
        token = url.rsplit("/", 1)[1]
        self.assertEqual(account_link.get_slack_account_link_preview(token).status, "ready")
        self.assertEqual(account_link.complete_slack_account_link(token, "nobody@x.example").reason, "not_in_org")
        done = account_link.complete_slack_account_link(token, "ada@quillfern.example.com")
        self.assertTrue(done.ok)
        member = get_workspace_member(self.team, "UNEW")
        self.assertEqual((member["person_id"], member["link_source"]), ("p-ada", "web_confirmed"))
        self.assertEqual(account_link.complete_slack_account_link(token, "ada@quillfern.example.com").reason, "used")
        self.assertEqual(account_link.get_slack_account_link_preview("bogus").status, "invalid")
        # The Slack-provisioned Person for this identity lost its membership.
        _, graph = falkordb_client.connect()
        rows = graph.query("MATCH (p:Person {email: 'new@quillfern.example.com'}) RETURN p.org_member").result_set
        self.assertEqual(rows, [[False]])

    def test_team_join_is_quiet_until_opted_in_then_welcomes_once(self):
        self.assertTrue(team_invite.process_slack_team_join(self.team, {"id": "UNEW"})["terminal"])
        self.assertEqual(self.web.posts(), [])

        team_invite.enable_automatic_lifecycle(self.team)
        set_home_channel(self.team, "CHOME")
        self.assertTrue(team_invite.process_slack_team_join(self.team, {"id": "UNEW"})["terminal"])
        self.assertEqual(len(self.web.posts()), 1)
        self.assertEqual(self.web.posts("conversations.invite")[-1], {"channel": "CHOME", "users": "UNEW"})
        self.assertTrue(team_invite.process_slack_team_join(self.team, {"id": "UNEW"})["terminal"])
        self.assertEqual(len(self.web.posts()), 1)
        self.assertTrue(team_invite.process_slack_team_join(self.team, {"id": "UGUEST", "is_restricted": True})["terminal"])

    def test_user_change_to_guest_revokes(self):
        provision_slack_workspace_member(team_id=self.team, slack_user_id="UNEW", email="new@quillfern.example.com")
        self.web.users["UNEW"]["is_restricted"] = True
        self.assertTrue(team_invite.process_slack_user_change(self.team, {"id": "UNEW"})["terminal"])
        self.assertEqual(get_workspace_member(self.team, "UNEW")["status"], "revoked")

    def _drive(self, max_steps: int = 50) -> None:
        handlers = {
            team_invite.RUN_DIRECTORY_PAGE: team_invite.run_team_invite_directory_page,
            team_invite.RUN_PROVISION_STEP: team_invite.run_team_invite_provision_step,
            team_invite.RUN_INVITE_STEP: team_invite.run_team_invite_step,
        }
        for _ in range(max_steps):
            run = team_invite.current_run(self.team)
            if not run or run["status"] in ("done", "failed", "aborted"):
                return
            raw = kv().hget(scheduler.PAYLOADS, run["schedule_id"] or "")
            if raw is None:
                return
            job = json.loads(raw)
            scheduler.cancel(run["schedule_id"])
            handlers[job["name"]](job["payload"])

    def test_automatic_rollout_provisions_invites_and_welcomes(self):
        set_home_channel(self.team, "CHOME")
        team_invite.start_automatic_team_invite_rollout(team_id=self.team, installer_slack_user_id="UADA", admin_channel="DUADA", admin_message_ts="1.0")
        self._drive()
        run = team_invite.current_run(self.team)
        # UADA is the installer, UGUEST and UBOT are ineligible: only UNEW is welcomed.
        self.assertEqual((run["status"], run["total"], run["sent"]), ("done", 1, 1))
        self.assertEqual([body["channel"] for body in self.web.posts()], ["DUNEW"])
        final_card = self.web.posts("chat.update")[-1]
        self.assertIn("Rollout complete: 1 welcomed", json.dumps(final_card["blocks"]))
        # Re-running dedupes against the welcome ledger.
        team_invite.start_automatic_team_invite_rollout(team_id=self.team, installer_slack_user_id="UADA", admin_channel="DUADA", admin_message_ts="1.0")
        self._drive()
        self.assertEqual(team_invite.current_run(self.team)["deduped"], 1)
        self.assertEqual(len(self.web.posts()), 1)

    def test_admin_wave_requires_admin(self):
        team_invite.start_team_invite_wave(team_id=self.team, mode="all", requested_by_slack_user_id="UNEW", card_channel="DUNEW")
        self.assertEqual(team_invite.current_run(self.team)["status"], "aborted")
        self.assertIn("Only a Company Brain admin", self.web.posts("chat.postEphemeral")[-1]["text"])

    def test_teardown_removes_install_and_members_and_clears_state(self):
        provision_slack_workspace_member(team_id=self.team, slack_user_id="UNEW", email="new@quillfern.example.com")
        team_invite.enable_automatic_lifecycle(self.team)
        result = teardown_slack_workspace(self.team, uninstall=lambda _web: "revoked")
        self.assertTrue(result.disconnected and result.revoked)
        self.assertIsNone(ws_module.get_workspace_row(self.team))
        self.assertIsNone(get_workspace_member(self.team, "UNEW"))
        self.assertFalse(team_invite.automatic_lifecycle_enabled(self.team))

    def test_transient_uninstall_keeps_the_install_for_retry(self):
        result = teardown_slack_workspace(self.team, uninstall=lambda _web: "transient")
        self.assertEqual((result.disconnected, result.reason), (False, "cleanup_failed"))
        self.assertIsNotNone(ws_module.get_workspace_row(self.team))


if __name__ == "__main__":
    unittest.main()
