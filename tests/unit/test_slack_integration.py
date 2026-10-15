import hashlib
import hmac
import time
import unittest

from src.integrations.slack.bot_address import collect_bot_address_aliases, is_bot_addressed_by_name
from src.integrations.slack.events import (
    SlackEventCallback,
    SlackEventInner,
    SlackUrlVerification,
    is_addressed_to_other_slack_user,
    is_answered_event,
    is_context_retention_event,
    is_name_wake_candidate,
    is_private_slack_channel,
    parse_envelope,
    parse_workspace_user_event,
    should_ignore_message,
)
from src.integrations.slack.verify import is_slack_retry, verify_slack_signature

SECRET = "shhh"
BOT = "UBOT"


def signed(body: bytes, secret: str = SECRET, ts: int | None = None) -> dict:
    ts = int(time.time()) if ts is None else ts
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:{body.decode()}".encode(), hashlib.sha256).hexdigest()
    return {"x-slack-signature": sig, "x-slack-request-timestamp": str(ts)}


def ev(**kwargs) -> SlackEventInner:
    kwargs.setdefault("type", "message")
    return SlackEventInner(**kwargs)


class VerifyTests(unittest.TestCase):
    body = b'{"type":"event_callback"}'

    def test_valid_signature(self):
        self.assertTrue(verify_slack_signature(self.body, signed(self.body), SECRET))

    def test_wrong_secret(self):
        self.assertFalse(verify_slack_signature(self.body, signed(self.body, secret="other"), SECRET))

    def test_tampered_body(self):
        self.assertFalse(verify_slack_signature(b'{"x":1}', signed(self.body), SECRET))

    def test_stale_timestamp(self):
        old = int(time.time()) - 301
        self.assertFalse(verify_slack_signature(self.body, signed(self.body, ts=old), SECRET))

    def test_within_skew(self):
        now = 1_700_000_000
        headers = signed(self.body, ts=now - 299)
        self.assertTrue(verify_slack_signature(self.body, headers, SECRET, now_seconds=now))

    def test_missing_headers_or_secret(self):
        self.assertFalse(verify_slack_signature(self.body, {}, SECRET))
        self.assertFalse(verify_slack_signature(self.body, {"x-slack-signature": "v0=abc"}, SECRET))
        self.assertFalse(verify_slack_signature(self.body, signed(self.body), ""))

    def test_non_numeric_timestamp(self):
        headers = signed(self.body)
        headers["x-slack-request-timestamp"] = "nope"
        self.assertFalse(verify_slack_signature(self.body, headers, SECRET))

    def test_retry_header(self):
        self.assertTrue(is_slack_retry({"x-slack-retry-num": "1"}))
        self.assertFalse(is_slack_retry({}))


class EnvelopeTests(unittest.TestCase):
    def test_url_verification(self):
        parsed = parse_envelope({"type": "url_verification", "challenge": "abc"})
        self.assertIsInstance(parsed, SlackUrlVerification)
        self.assertEqual(parsed.challenge, "abc")

    def test_event_callback(self):
        parsed = parse_envelope(
            {"type": "event_callback", "team_id": "T1", "event_id": "Ev1", "event": {"type": "message", "user": "U1", "text": "hi"}}
        )
        self.assertIsInstance(parsed, SlackEventCallback)
        self.assertEqual(parsed.event.user, "U1")

    def test_unknown_type(self):
        self.assertIsNone(parse_envelope({"type": "app_rate_limited"}))

    def test_team_join_nested_user(self):
        body = {"type": "event_callback", "team_id": "T1", "event_id": "Ev2", "event": {"type": "team_join", "user": {"id": "U9", "is_bot": False}}}
        self.assertIsNone(parse_envelope(body))
        team_id, event_id, kind, user = parse_workspace_user_event(body)
        self.assertEqual((team_id, event_id, kind, user.id), ("T1", "Ev2", "team_join", "U9"))

    def test_workspace_user_event_ignores_others(self):
        body = {"type": "event_callback", "team_id": "T1", "event_id": "E", "event": {"type": "message", "user": "U1"}}
        self.assertIsNone(parse_workspace_user_event(body))


class ClassifierTests(unittest.TestCase):
    def test_should_ignore_message(self):
        self.assertTrue(should_ignore_message(ev(bot_id="B1", text="x")))
        self.assertTrue(should_ignore_message(ev(app_id="A1")))
        self.assertTrue(should_ignore_message(ev(subtype="message_changed")))
        self.assertTrue(should_ignore_message(ev(subtype="channel_join")))
        self.assertFalse(should_ignore_message(ev()))
        for subtype in ("file_share", "me_message", "thread_broadcast"):
            self.assertFalse(should_ignore_message(ev(subtype=subtype)))

    def test_is_private_slack_channel(self):
        cases = [
            ("C1", "channel", False),
            ("C1", "group", True),
            ("G1", "mpim", True),
            ("D1", "im", False),
            ("G1", None, True),
            ("C1", None, True),  # fail closed
            ("D1", None, False),
            (None, None, False),
        ]
        for channel, kind, expected in cases:
            with self.subTest(channel=channel, kind=kind):
                self.assertEqual(is_private_slack_channel(channel, kind), expected)

    def test_is_answered_event(self):
        self.assertTrue(is_answered_event(ev(channel="D1", channel_type="im", text="hi"), BOT))
        self.assertTrue(is_answered_event(ev(type="app_mention", channel="C1", text=f"<@{BOT}> hi"), BOT))
        # The plain message twin of an @mention is not answered.
        self.assertFalse(is_answered_event(ev(channel="C1", channel_type="channel", text=f"<@{BOT}> hi"), BOT))
        self.assertTrue(is_answered_event(ev(channel="C1", channel_type="channel", text="more", thread_ts="1.0"), BOT))
        self.assertFalse(is_answered_event(ev(channel="C1", channel_type="channel", text="lunch?"), BOT))
        self.assertFalse(is_answered_event(ev(channel="D1", channel_type="im", bot_id="B", text="x"), BOT))

    def test_is_addressed_to_other_slack_user(self):
        self.assertTrue(is_addressed_to_other_slack_user("<@U2> can you look", BOT))
        self.assertFalse(is_addressed_to_other_slack_user(f"<@U2> and <@{BOT}> look", BOT))
        self.assertFalse(is_addressed_to_other_slack_user("no mentions", BOT))
        self.assertFalse(is_addressed_to_other_slack_user("<@U2> hi", None))

    def test_is_context_retention_event(self):
        self.assertTrue(is_context_retention_event(ev(channel="C1", text="hello all"), BOT))
        self.assertFalse(is_context_retention_event(ev(channel="C1", text=f"<@{BOT}> q"), BOT))
        self.assertFalse(is_context_retention_event(ev(type="app_mention", channel="C1", text="x"), BOT))
        self.assertFalse(is_context_retention_event(ev(text="no channel"), BOT))

    def test_is_name_wake_candidate(self):
        self.assertTrue(is_name_wake_candidate(ev(channel="C1", channel_type="channel", text="company brain who owns billing"), BOT))
        self.assertFalse(is_name_wake_candidate(ev(channel="D1", channel_type="im", text="hi"), BOT))
        self.assertFalse(is_name_wake_candidate(ev(channel="C1", text=f"<@{BOT}> hi"), BOT))
        self.assertFalse(is_name_wake_candidate(ev(channel="C1", text="   "), BOT))
        self.assertFalse(is_name_wake_candidate(ev(type="app_mention", channel="C1", text="x"), BOT))


class BotAddressTests(unittest.TestCase):
    def test_aliases(self):
        aliases = collect_bot_address_aliases("Brainy Bot", "brainy", "brainy_bot")
        for expected in ("company brain", "company-brain", "brainy bot", "brainy", "brainy_bot"):
            self.assertIn(expected, aliases)
        self.assertEqual(len(aliases), len(set(aliases)))

    def test_addressed_by_name(self):
        aliases = collect_bot_address_aliases(None, None, None)
        self.assertTrue(is_bot_addressed_by_name("hey company brain, who owns billing?", aliases))
        self.assertTrue(is_bot_addressed_by_name("company-brain: who owns auth", aliases))
        self.assertTrue(is_bot_addressed_by_name("Company Brain who owns auth", aliases))
        self.assertFalse(is_bot_addressed_by_name("I asked company brain yesterday", aliases))
        self.assertFalse(is_bot_addressed_by_name("", aliases))
        self.assertFalse(is_bot_addressed_by_name("company brain", []))


if __name__ == "__main__":
    unittest.main()
