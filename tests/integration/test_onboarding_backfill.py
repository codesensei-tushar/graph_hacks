"""Onboarding backfill: token-mode bootstrap and the `backfill public channels` command.

Uses the live Redis under FalkorDB for markers, rollout state and the scheduler;
Slack and the bootstrap's Slack calls are faked.
"""

import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.core import scheduler
from src.db.falkordb_client import kv
from src.domain.slack import compute, token_install
from src.domain.slack.channels import rollout


def _workspace(team_id: str, source: str = "env", scopes=None):
    client = mock.MagicMock()
    client.lookup_slack_user_info.return_value = {"ok": True, "user": {"name": "owner", "real_name": "Owner"}}
    return SimpleNamespace(team_id=team_id, source=source, scopes=scopes, client=client, team_name="Quillfern", installed_by=None)


class _TeamCleanup(unittest.TestCase):
    def setUp(self):
        self.team = f"TB{uuid.uuid4().hex[:8].upper()}"

    def tearDown(self):
        keys = list(kv().scan_iter(match=f"brain:rollout:{self.team}:*"))
        keys.append(token_install._bootstrapped_key(self.team))
        kv().delete(*keys)
        scheduler.cancel_matching(rollout.JOB_RUN_ROLLOUT, lambda payload: payload.get("team_id") == self.team)


class TokenModeBootstrapTest(_TeamCleanup):
    def _patches(self, home=None, existing_home=None):
        return (
            mock.patch.object(token_install, "bootstrap_slack_workspace", return_value=home),
            mock.patch.object(token_install, "greet_slack_installer"),
            mock.patch.object(token_install, "get_home_channel", return_value=existing_home),
            mock.patch.object(token_install, "get_workspace_by_team_id", return_value=None),
        )

    def test_runs_once_with_the_owner_as_installer(self):
        ws = _workspace(self.team)
        boot, greet, home, reload = self._patches(home={"channel_id": "CHOME", "created": True})
        with boot as bootstrap, greet as greeting, home, reload:
            self.assertTrue(token_install.ensure_token_workspace_bootstrapped(ws))
            self.assertFalse(token_install.ensure_token_workspace_bootstrapped(ws))
        bootstrap.assert_called_once_with(workspace=ws, slack_user_id="UOWNER")
        greeting.assert_called_once()
        self.assertEqual(greeting.call_args.kwargs["slack_user_id"], "UOWNER")

    def test_oauth_installs_are_left_to_the_callback(self):
        boot, greet, home, reload = self._patches(home={"channel_id": "CHOME", "created": True})
        with boot as bootstrap, greet, home, reload:
            self.assertFalse(token_install.ensure_token_workspace_bootstrapped(_workspace(self.team, source="oauth")))
        bootstrap.assert_not_called()

    def test_a_failed_bootstrap_retries_next_start(self):
        ws = _workspace(self.team)
        boot, greet, home, reload = self._patches(home=None)
        with boot as bootstrap, greet as greeting, home, reload:
            self.assertFalse(token_install.ensure_token_workspace_bootstrapped(ws))
            self.assertFalse(token_install.ensure_token_workspace_bootstrapped(ws))
        self.assertEqual(bootstrap.call_count, 2)
        greeting.assert_not_called()

    def test_an_existing_home_channel_counts_as_bootstrapped(self):
        boot, greet, home, reload = self._patches(existing_home={"channel_id": "CHOME", "team_id": self.team})
        with boot as bootstrap, greet, home, reload:
            self.assertFalse(token_install.ensure_token_workspace_bootstrapped(_workspace(self.team)))
        bootstrap.assert_not_called()
        self.assertTrue(kv().get(token_install._bootstrapped_key(self.team)))

    def test_disconnect_clears_the_marker(self):
        kv().set(token_install._bootstrapped_key(self.team), "1")
        token_install._forget_bootstrap(self.team)
        self.assertIsNone(kv().get(token_install._bootstrapped_key(self.team)))


class BackfillCommandTest(_TeamCleanup):
    def _card(self):
        rollout._put(
            f"{rollout._p(self.team)}:card",
            {"team_id": self.team, "home_channel_id": "CHOME", "message_ts": "1.0", "admin_channel_id": None, "admin_message_ts": None, "updated_at": 0},
        )

    def test_members_cannot_start_it(self):
        self._card()
        with mock.patch.object(rollout, "_workspace", return_value=_workspace(self.team)):
            reply = rollout.start_public_channel_rollout_from_command(self.team, "UMEMBER")
        self.assertIn("Only a Company Brain organization admin", reply)
        self.assertIsNone(rollout.current_run(self.team))

    def test_owner_starts_the_same_run_as_the_button(self):
        self._card()
        ws = _workspace(self.team)
        with mock.patch.object(rollout, "_workspace", return_value=ws):
            reply = rollout.start_public_channel_rollout_from_command(self.team, "UOWNER")
            self.assertIn("<#CHOME>", reply)
            run = rollout.current_run(self.team)
            self.assertEqual((run["status"], run["phase"], run["home_channel_id"]), ("running", "discover", "CHOME"))
            self.assertIsNone(run["channel_filter_json"])
            self.assertEqual(
                run["window_end_ms"] - run["window_start_ms"], rollout.PUBLIC_CHANNEL_ROLLOUT_DAYS * 24 * 60 * 60 * 1000
            )
            again = rollout.start_public_channel_rollout_from_command(self.team, "UOWNER")
        self.assertIn("already working through the public channels", again)

    def test_missing_scopes_stop_it(self):
        self._card()
        with mock.patch.object(rollout, "_workspace", return_value=_workspace(self.team, scopes="chat:write")):
            reply = rollout.start_public_channel_rollout_from_command(self.team, "UOWNER")
        self.assertIn("Missing Slack scopes", reply)
        self.assertIsNone(rollout.current_run(self.team))

    def test_no_card_bootstraps_the_home_channel_first(self):
        ws = _workspace(self.team)
        with mock.patch.object(rollout, "_workspace", return_value=ws), mock.patch(
            "src.domain.slack.bootstrap.bootstrap_slack_workspace", return_value={"channel_id": "CNEWHOME", "created": True}
        ) as bootstrap:
            reply = rollout.start_public_channel_rollout_from_command(self.team, "UOWNER")
        bootstrap.assert_called_once_with(workspace=ws, slack_user_id="UOWNER")
        self.assertIn("<#CNEWHOME>", reply)
        self.assertEqual(rollout.current_run(self.team)["home_channel_id"], "CNEWHOME")

    def test_no_home_channel_means_no_run(self):
        with mock.patch.object(rollout, "_workspace", return_value=_workspace(self.team)), mock.patch(
            "src.domain.slack.bootstrap.bootstrap_slack_workspace", return_value=None
        ):
            reply = rollout.start_public_channel_rollout_from_command(self.team, "UOWNER")
        self.assertIn("couldn't set up the #company-brain home channel", reply)
        self.assertIsNone(rollout.current_run(self.team))


class BackfillPhrasingTest(unittest.TestCase):
    def test_phrasings(self):
        for text in ("backfill public channels", "Backfill the public channels", "add me to my public channels", "join all public channels"):
            self.assertTrue(compute._BACKFILL.match(text), text)
        for text in ("what happened in public channels", "backfill", "public channels"):
            self.assertFalse(compute._BACKFILL.match(text), text)


if __name__ == "__main__":
    unittest.main()
