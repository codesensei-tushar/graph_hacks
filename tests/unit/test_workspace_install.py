"""Pure tests for the install / identity lifecycle ports (no FalkorDB, no network)."""

import re
import unittest
from pathlib import Path
from unittest import mock

from slack_sdk.errors import SlackApiError

from src.domain.slack import lifecycle_api
from src.domain.slack.company_domain import company_domain, normalize_company_domain
from src.domain.slack.farewell import (
    SlackFarewellDeps,
    post_slack_farewell,
    should_hold_for_farewell,
    slack_farewell_client_msg_id,
)
from src.domain.slack.greet import first_name_of, install_greeting, member_greeting
from src.domain.slack.home_welcome import company_brain_disconnect_message, company_brain_home_welcome_messages
from src.domain.slack.lifecycle_api import SlackConversation, SlackMember, SlackPostResult, map_slack_member
from src.domain.slack.manifest import SLACK_BOT_EVENTS, SLACK_BOT_SCOPES, SLACK_USER_SCOPES, slack_app_manifest
from src.domain.slack.membership_provenance import SlackMembershipMapping, provisioned_membership_to_revoke
from src.domain.slack.team_invite import parse_team_invite_interaction_payload
from src.domain.slack.team_invite_card import (
    TEAM_INVITE_NOTIFY_ALL_ACTION_ID,
    TEAM_INVITE_PICK_BLOCK_ID,
    TEAM_INVITE_SELECT_ACTION_ID,
    TEAM_INVITE_SEND_ACTION_ID,
    MemberConnectAction,
    MemberIntroParts,
    automatic_team_invite_progress_blocks,
    is_eligible_full_member,
    member_intro_blocks,
    member_intro_text,
    team_invite_card_blocks,
)
from src.domain.slack.token_crypto import decrypt_token, encrypt_token
from src.domain.slack.workspace import escape_slack_text, format_slack_org_member_denial, should_post_slack_org_member_denial


class _Response:
    def __init__(self, data, status_code=200, headers=None):
        self.data = data
        self.status_code = status_code
        self.headers = headers or {}


class FakeWeb:
    """Answers Web API methods from a table: method -> dict, list of dicts, or exception."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def api_call(self, method, json=None, **_):
        self.calls.append((method, json or {}))
        answer = self.answers.get(method, {"ok": True})
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, tuple):
            data, status, headers = answer
            raise SlackApiError("error", _Response(data, status, headers))
        if answer.get("ok") is not True:
            raise SlackApiError("error", _Response(answer))
        return _Response(answer)


class FarewellTest(unittest.TestCase):
    def deps(self, post_result, conversations=None, list_error=None):
        def list_bot_conversations(_web):
            if list_error:
                raise list_error
            return conversations or []

        return SlackFarewellDeps(post_message_idempotent=lambda *_a: post_result, list_bot_conversations=list_bot_conversations)

    def test_posts_to_known_home(self):
        out = post_slack_farewell(web=None, team_id="T", kind="bye", text="x", known_home_channel_id="C1", deps=self.deps(SlackPostResult(ok=True, ts="1")))
        self.assertEqual(out, "posted")

    def test_repeat_post_is_already_posted(self):
        result = SlackPostResult(ok=True, ts="1", deduped=True)
        self.assertEqual(post_slack_farewell(web=None, team_id="T", kind="bye", text="x", known_home_channel_id="C1", deps=self.deps(result)), "already_posted")

    def test_falls_back_to_bot_channels(self):
        deps = self.deps(SlackPostResult(ok=True, ts="1"), [SlackConversation(id="C9", name="company-brain")])
        self.assertEqual(post_slack_farewell(web=None, team_id="T", kind="bye", text="x", known_home_channel_id=None, deps=deps), "posted")

    def test_no_home_is_skipped(self):
        deps = self.deps(SlackPostResult(ok=True, ts="1"), [SlackConversation(id="C9", name="general")])
        self.assertEqual(post_slack_farewell(web=None, team_id="T", kind="bye", text="x", known_home_channel_id=None, deps=deps), "skipped")

    def test_lookup_failure_holds_unless_terminal(self):
        self.assertEqual(post_slack_farewell(web=None, team_id="T", kind="b", text="x", known_home_channel_id=None, deps=self.deps(None, list_error=RuntimeError("ratelimited"))), "hold")
        self.assertEqual(post_slack_farewell(web=None, team_id="T", kind="b", text="x", known_home_channel_id=None, deps=self.deps(None, list_error=RuntimeError("invalid_auth"))), "skipped")

    def test_hold_rules(self):
        self.assertTrue(should_hold_for_farewell(SlackPostResult(ok=False, error="ratelimited")))
        self.assertTrue(should_hold_for_farewell(SlackPostResult(ok=False, error="http_503")))
        self.assertTrue(should_hold_for_farewell(SlackPostResult(ok=False, error="channel_not_found", retry_after_seconds=3)))
        self.assertFalse(should_hold_for_farewell(SlackPostResult(ok=False, error="channel_not_found")))
        self.assertFalse(should_hold_for_farewell(SlackPostResult(ok=False, error="http_404")))
        self.assertTrue(should_hold_for_farewell(SlackPostResult(ok=False, error="weird_unknown")))

    def test_client_msg_id_is_a_stable_v4_uuid(self):
        first = slack_farewell_client_msg_id("bye", "T1", "C1")
        self.assertEqual(first, slack_farewell_client_msg_id("bye", "T1", "C1"))
        self.assertNotEqual(first, slack_farewell_client_msg_id("bye", "T1", "C2"))
        self.assertRegex(first, r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class LifecycleApiTest(unittest.TestCase):
    def test_idempotent_post_detects_dedup_by_old_ts(self):
        web = FakeWeb({"chat.postMessage": {"ok": True, "ts": "100.0"}})
        self.assertTrue(lifecycle_api.post_slack_message_idempotent(web, "C", "hi", "id").deduped)
        self.assertEqual(web.calls[0][1]["client_msg_id"], "id")

    def test_post_failure_carries_retry_after(self):
        web = FakeWeb({"chat.postMessage": ({"ok": False, "error": "ratelimited"}, 429, {"Retry-After": "7"})})
        result = lifecycle_api.post_slack_message_idempotent(web, "C", "hi", "id")
        self.assertFalse(result.ok)
        self.assertEqual((result.error, result.retry_after_seconds), ("ratelimited", 7))

    def test_invite_accepts_already_in_channel(self):
        self.assertTrue(lifecycle_api.invite_slack_user_to_channel(FakeWeb({"conversations.invite": {"ok": False, "error": "already_in_channel"}}), "C", "U").ok)
        self.assertFalse(lifecycle_api.invite_slack_user_to_channel(FakeWeb({"conversations.invite": {"ok": False, "error": "not_in_channel"}}), "C", "U").ok)

    def test_uninstall_outcomes(self):
        self.assertEqual(lifecycle_api.uninstall_slack_app(FakeWeb({"apps.uninstall": {"ok": True}})), "revoked")
        self.assertEqual(lifecycle_api.uninstall_slack_app(FakeWeb({"apps.uninstall": {"ok": False, "error": "token_revoked"}})), "terminal")
        self.assertEqual(lifecycle_api.uninstall_slack_app(FakeWeb({"apps.uninstall": {"ok": False, "error": "ratelimited"}})), "transient")
        self.assertEqual(lifecycle_api.uninstall_slack_app(FakeWeb({"apps.uninstall": ({"ok": False}, 502, {})})), "transient")
        self.assertEqual(lifecycle_api.uninstall_slack_app(FakeWeb({"apps.uninstall": ConnectionError("down")})), "transient")
        # Unknown verdict falls back to auth.revoke.
        self.assertEqual(lifecycle_api.uninstall_slack_app(FakeWeb({"apps.uninstall": {"ok": False, "error": "invalid_auth"}, "auth.revoke": {"ok": True}})), "revoked")
        self.assertEqual(lifecycle_api.uninstall_slack_app(FakeWeb({"apps.uninstall": {"ok": False, "error": "invalid_auth"}, "auth.revoke": {"ok": False, "error": "invalid_auth"}})), "terminal")

    def test_user_lookup_reasons(self):
        self.assertEqual(lifecycle_api.lookup_slack_user_info(FakeWeb({}), "").reason, "missing_user_id")
        self.assertEqual(lifecycle_api.lookup_slack_user_info(FakeWeb({"users.info": {"ok": False, "error": "user_not_found"}}), "U").reason, "user_not_found")
        self.assertEqual(lifecycle_api.lookup_slack_user_info(FakeWeb({"users.info": {"ok": False, "error": "ratelimited"}}), "U").reason, "slack_api_error")
        found = lifecycle_api.lookup_slack_user_info(FakeWeb({"users.info": {"ok": True, "user": {"id": "U", "real_name": "Ada Park", "profile": {"email": "ada@x.example"}}}}), "U")
        self.assertEqual((found.user.name, found.user.email), ("Ada Park", "ada@x.example"))

    def test_map_slack_member(self):
        self.assertIsNone(map_slack_member({"id": "U", "deleted": True, "name": "x"}))
        member = map_slack_member({"id": "U1", "name": "ada", "is_restricted": True, "profile": {"display_name": "Ada"}})
        self.assertEqual((member.name, member.handle, member.display_name, member.is_restricted), ("Ada", "ada", "Ada", True))

    def test_users_page_completion(self):
        web = FakeWeb({"users.list": [{"ok": True, "members": [{"id": "U1", "name": "a"}], "response_metadata": {"next_cursor": "c"}}, {"ok": True, "members": [{"id": "U2", "name": "b"}]}]})
        self.assertEqual([m.id for m in lifecycle_api.get_slack_team_directory(web)], ["U1", "U2"])


class CopyAndCardsTest(unittest.TestCase):
    def test_company_domain(self):
        self.assertIsNone(company_domain("gmail.com"))
        self.assertEqual(company_domain(" Example.COM "), "example.com")
        self.assertEqual(normalize_company_domain("https://www.example.com/about"), "example.com")
        self.assertIsNone(normalize_company_domain("example..com"))
        self.assertIsNone(normalize_company_domain("localhost"))

    def test_copy_never_names_the_hosted_product(self):
        texts = [
            install_greeting("Ada", "Quillfern", "C1"),
            member_greeting("Ada", "Quillfern"),
            company_brain_disconnect_message(),
            *company_brain_home_welcome_messages("Ada <script>"),
            member_intro_text(MemberIntroParts(team_id="T")),
        ]
        for text in texts:
            self.assertNotRegex(text.lower(), r"supermemory|trial")
        self.assertIn("Ada", company_brain_home_welcome_messages("Ada <script>")[0])
        self.assertNotIn("<script>", company_brain_home_welcome_messages("Ada <script>")[0])

    def test_install_greeting_uses_home_link_and_starters(self):
        text = install_greeting("Ada", None, "C1", ["Who owns billing?"])
        self.assertIn("<#C1>", text)
        self.assertIn(">Who owns billing?", text)
        self.assertEqual(first_name_of("  Ada  Park "), "Ada")

    def test_denial_copy(self):
        self.assertIn("full members", format_slack_org_member_denial())
        self.assertIn("Hey Ada &lt;b&gt;,", format_slack_org_member_denial("Ada <b>"))
        self.assertEqual(escape_slack_text("a<&>"), "a&lt;&amp;&gt;")
        self.assertFalse(should_post_slack_org_member_denial())
        self.assertTrue(should_post_slack_org_member_denial(is_dm=True))

    def test_eligibility(self):
        self.assertTrue(is_eligible_full_member(SlackMember(id="U1"), "UBOT", "T1"))
        self.assertFalse(is_eligible_full_member(SlackMember(id="USLACKBOT")))
        self.assertFalse(is_eligible_full_member(SlackMember(id="UBOT"), "UBOT"))
        self.assertFalse(is_eligible_full_member(SlackMember(id="U2", is_ultra_restricted=True)))
        self.assertFalse(is_eligible_full_member(SlackMember(id="U3", team_id="T2"), None, "T1"))
        self.assertFalse(is_eligible_full_member(None))

    def test_cards(self):
        blocks = team_invite_card_blocks()
        ids = [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]
        self.assertEqual(ids, [TEAM_INVITE_SEND_ACTION_ID, TEAM_INVITE_NOTIFY_ALL_ACTION_ID])
        self.assertIn("Notified 1 teammate.", team_invite_card_blocks("done", sent=1)[-1]["elements"][0]["text"].replace(" Re-run", ""))
        done = automatic_team_invite_progress_blocks(phase="done", sent=3, deduped=1, skipped=2)
        self.assertEqual(done[1]["elements"][0]["text"], "✅ Rollout complete: 3 welcomed, 1 already notified, 2 failed.")
        intro = member_intro_blocks(MemberIntroParts(team_id="T", has_connect_actions=True), [MemberConnectAction("linear", "Linear")])
        self.assertEqual(intro[1]["elements"][0]["action_id"], "brain_member_connect_linear")

    def test_membership_provenance(self):
        self.assertIsNone(provisioned_membership_to_revoke([SlackMembershipMapping("active", "p1"), SlackMembershipMapping("revoked", "p2")]))
        self.assertEqual(provisioned_membership_to_revoke([SlackMembershipMapping("revoked", None), SlackMembershipMapping("revoked", "p2")]), "p2")
        self.assertIsNone(provisioned_membership_to_revoke([]))

    def test_team_invite_payload(self):
        payload = {
            "type": "block_actions",
            "team": {"id": "T"},
            "user": {"id": "U"},
            "channel": {"id": "D1"},
            "message": {"ts": "1.0"},
            "actions": [{"action_id": TEAM_INVITE_SEND_ACTION_ID}],
            "state": {"values": {TEAM_INVITE_PICK_BLOCK_ID: {TEAM_INVITE_SELECT_ACTION_ID: {"selected_users": ["U2", 3]}}}},
        }
        parsed = parse_team_invite_interaction_payload(payload)
        self.assertEqual((parsed["mode"], parsed["user_ids"]), ("selected", ["U2"]))
        payload["state"] = {}
        self.assertIsNone(parse_team_invite_interaction_payload(payload))
        payload["actions"] = [{"action_id": TEAM_INVITE_NOTIFY_ALL_ACTION_ID}]
        self.assertEqual(parse_team_invite_interaction_payload(payload)["mode"], "all")
        payload["actions"] = [{"action_id": "other"}]
        self.assertIsNone(parse_team_invite_interaction_payload(payload))


class ManifestAndTokensTest(unittest.TestCase):
    def test_generated_manifest_urls(self):
        manifest = slack_app_manifest("https://brain.example.com/")
        self.assertEqual(
            manifest["oauth_config"]["redirect_urls"],
            ["https://brain.example.com/slack/oauth/callback", "https://brain.example.com/auth/slack/callback"],
        )
        self.assertEqual(manifest["oauth_config"]["scopes"]["user"], ["openid", "email", "profile"])
        self.assertEqual(manifest["settings"]["event_subscriptions"]["request_url"], "https://brain.example.com/slack/events")

    def test_yaml_manifest_matches_generator(self):
        text = (Path(__file__).resolve().parents[2] / "slack_app" / "manifest.yaml").read_text()
        bot = re.search(r"    bot:\n((?:      - .+\n)+)", text).group(1)
        events = re.search(r"    bot_events:\n((?:      - .+\n)+)", text).group(1)
        self.assertEqual(re.findall(r"- (\S+)", bot), SLACK_BOT_SCOPES)
        self.assertEqual(re.findall(r"- (\S+)", events), SLACK_BOT_EVENTS)

    def test_yaml_manifests_carry_sign_in_with_slack(self):
        root = Path(__file__).resolve().parents[2] / "slack_app"
        for name in ("manifest.yaml", "manifest.prod.yaml"):
            with self.subTest(name):
                text = (root / name).read_text()
                user = re.search(r"    user:\n((?:      - .+\n)+)", text).group(1)
                self.assertEqual(re.findall(r"- (\S+)", user), SLACK_USER_SCOPES)
                self.assertRegex(text, r"redirect_urls:\n(?:    - .+\n)*    - https://\S+/auth/slack/callback\n")
        prod = (root / "manifest.prod.yaml").read_text()
        self.assertIn("- https://brain.noobsurd.space/auth/slack/callback\n", prod)

    def test_token_round_trip_without_encryption(self):
        with mock.patch("src.domain.slack.token_crypto._fernet", return_value=None):
            stored = encrypt_token("xoxb-1")
            self.assertTrue(stored.startswith("plain:"))
            self.assertEqual(decrypt_token(stored), "xoxb-1")
            with self.assertRaises(RuntimeError):
                decrypt_token("fernet:abc")
        with self.assertRaises(ValueError):
            decrypt_token("xoxb-raw")


if __name__ == "__main__":
    unittest.main()
