"""Channel features ported from company-brain: directory, lookup, history documents,
introductions, public-channel rollout, and channel observe.

The rollout and observe cases run against the local FalkorDB/Redis with a fake
Slack client and a hand-driven scheduler. They use their own team ids and clean
up their keys and Memory nodes.
"""

import json
import os
import time
import unittest
import uuid
from unittest import mock

os.environ.setdefault("FALKORDB_GRAPH", "company_channels_test")

from src.core import scheduler  # noqa: E402
from src.db.falkordb_client import connect, graph_name, kv  # noqa: E402
from src.domain.slack.channels import directory, introduction, lookup, observe, policy, rollout  # noqa: E402
from src.domain.slack.channels.history_document import (  # noqa: E402
    SlackHistoryDocumentInput,
    build_slack_history_documents,
    recent_slack_channel_evidence,
)
from src.integrations.slack.client import (  # noqa: E402
    SlackChannelHistoryResult,
    SlackConversation,
    SlackConversationInfo,
    SlackCursorPage,
    SlackJoinResult,
    SlackPostResult,
    SlackPublicChannel,
    SlackUserInfo,
    SlackUserInfoLookup,
)

DAY = 24 * 60 * 60


def _ts(seconds_ago: float) -> str:
    return f"{time.time() - seconds_ago:.6f}"


class PolicyAndDirectoryTest(unittest.TestCase):
    def test_eligibility_skips_general_home_archived_external(self):
        base = dict(is_member=False, is_archived=False, is_general=False, is_external=False)
        ok = SlackPublicChannel(id="C1", name="eng", **base)
        self.assertTrue(policy.is_eligible_public_channel(ok, home_channel_id="CHOME"))
        for channel in (
            SlackPublicChannel(id="C2", name="general", **base),
            SlackPublicChannel(id="C3", name="company-brain", **base),
            SlackPublicChannel(id="CHOME", name="home", **base),
            SlackPublicChannel(id="C4", name="old", **{**base, "is_archived": True}),
            SlackPublicChannel(id="C5", name="shared", **{**base, "is_external": True}),
            SlackPublicChannel(id="C6", name="all-hands", **{**base, "is_general": True}),
        ):
            self.assertFalse(policy.is_eligible_public_channel(channel, home_channel_id="CHOME"), channel.name)

    def test_thread_root(self):
        self.assertTrue(policy.is_slack_history_thread_root({"ts": "1.0", "reply_count": 2}))
        self.assertTrue(policy.is_slack_history_thread_root({"ts": "1.0", "thread_ts": "1.0", "reply_count": 2}))
        self.assertFalse(policy.is_slack_history_thread_root({"ts": "2.0", "thread_ts": "1.0", "reply_count": 2}))
        self.assertFalse(policy.is_slack_history_thread_root({"ts": "1.0", "reply_count": 0}))

    def test_channel_refs(self):
        self.assertEqual(directory.parse_channel_ref("<#C123ABC|eng>"), {"id": "C123ABC", "name": "eng"})
        self.assertEqual(directory.parse_channel_ref("C123ABC")["id"], "C123ABC")
        self.assertEqual(directory.parse_channel_ref("#eng-platform"), {"id": None, "name": "eng-platform"})
        dir_ = [SlackConversation("C1", "eng-platform", False), SlackConversation("G1", "billing-war-room", True)]
        self.assertEqual(directory.resolve_channel_ref(dir_, "#ENG-platform").id, "C1")
        self.assertEqual(directory.find_channels_named_in_query(dir_, "what happened in billing", 5)[0].id, "G1")
        # Backfill prefers public channels after named matches.
        self.assertEqual([c.id for c in directory.rank_channels_for_query(dir_, "nothing matches", 2)], ["C1", "G1"])

    def test_private_channel_access_gate(self):
        client = mock.Mock()
        client.get_conversation_members.return_value = ["UALICE"]
        private = directory.SlackConversation("G1", "secret", True)
        public = directory.SlackConversation("C1", "eng", False)
        check = directory.check_asker_can_search_channel
        self.assertEqual(check(client, "T", public, "UBOB", is_org_member=False).reason, "not_org_member")
        self.assertTrue(check(client, "T", public, "UBOB", is_org_member=True).ok)
        self.assertEqual(
            check(client, "T", private, "UBOB", is_org_member=True, response_surface="dm").reason,
            "private_channel_requires_membership",
        )
        self.assertEqual(
            check(client, "T", private, "UALICE", is_org_member=True, response_surface="public_channel").reason,
            "private_channel_non_dm_response",
        )
        self.assertTrue(check(client, "T", private, "UALICE", is_org_member=True, response_surface="dm").ok)
        # Guests need membership even for public channels.
        self.assertFalse(check(client, "T", public, "UBOB", is_org_member=True, asker_is_restricted=True).ok)


class LookupTest(unittest.TestCase):
    def test_text_formatting(self):
        text = "<@U1> ping <!subteam^S1|oncall> and <!here> in <#C9|eng>"
        self.assertEqual(lookup.format_channel_message_text(text, {"U1": "Maya"}), "@Maya ping @oncall and @here in <#C9|eng>")

    def test_windows(self):
        now = 1_700_000_000_000
        self.assertEqual(lookup.parse_slack_time_window(None, now).label, "last 7 days")
        y = lookup.parse_slack_time_window("yesterday", now)
        self.assertEqual(y.latest_ms - y.oldest_ms, DAY * 1000)

    def test_action_items_and_links(self):
        msgs = lookup.format_channel_messages(
            [
                {"ts": "100.0", "user": "U1", "text": "we need to rotate the billing keys"},
                {"ts": "200.0", "user": "U2", "text": "done, rotated"},
                {"ts": "300.0", "user": "U1", "text": "todo: write the runbook"},
            ]
        )
        items = lookup.extract_action_items_from_messages(msgs)
        self.assertEqual([i.status for i in items], ["likely_done", "likely_open"])
        links = lookup.extract_links_from_messages(
            [{"ts": "1.0", "text": "see <https://example.com/doc|the doc> and https://example.com/raw"}]
        )
        self.assertEqual([link.url for link in links], ["https://example.com/doc", "https://example.com/raw"])

    def test_channel_lookup_and_search(self):
        client = mock.Mock()
        history = [
            {"ts": _ts(3600), "user": "U1", "text": "billing retries are flaky again"},
            {"ts": _ts(1800), "user": "U2", "text": "lunch?"},
        ]
        client.get_slack_channel_history.return_value = SlackChannelHistoryResult(ok=True, messages=history, complete=True)
        ctx = lookup.SlackLookupContext(client=client, channel="C1", team_id=f"T{uuid.uuid4().hex[:6]}")
        out = lookup.run_slack_channel_lookup(ctx, lookup.ChannelLookupArgs(intent="find_related", query="billing retries"))
        self.assertIn("billing retries are flaky", out)
        self.assertNotIn("lunch", out)

        client.get_bot_conversations.return_value = [SlackConversation("C1", "billing", False), SlackConversation("G1", "secret", True)]
        client.get_conversation_members.return_value = ["UOTHER"]
        found = lookup.run_slack_channels_search(
            ctx,
            lookup.SlackChannelsSearchAuth(asker_slack_user_id="U9", is_org_member=True, response_surface="dm"),
            lookup.SlackChannelsSearchArgs(query="billing retries"),
        )
        self.assertIn("#billing", found)
        self.assertIn("Skipped 1 channel.", found)
        kv().delete(directory._cache_key(ctx.team_id))


class HistoryDocumentTest(unittest.TestCase):
    def setUp(self):
        self.start = int((time.time() - 7 * DAY) * 1000)
        self.end = int(time.time() * 1000)

    def _input(self, messages, **kw):
        return SlackHistoryDocumentInput(
            team_id="T1", channel_id="C1", channel_name="eng", window_start_ms=self.start, window_end_ms=self.end,
            messages=messages, bot_user_id="UBOT", **kw,
        )

    def test_documents_are_deterministic_and_skip_bot_and_out_of_window(self):
        root = _ts(2 * DAY)
        messages = [
            {"ts": root, "user": "U1", "text": "Shipping the invoice worker today", "reply_count": 1},
            {"ts": _ts(2 * DAY - 60), "thread_ts": root, "user": "U2", "text": "ack, watching dashboards"},
            {"ts": _ts(DAY), "user": "UBOT", "text": "bot reply"},
            {"ts": _ts(30 * DAY), "user": "U1", "text": "ancient"},
            {"ts": _ts(DAY), "user": "U3", "text": "", "subtype": "channel_join"},
        ]
        docs = build_slack_history_documents(self._input(messages, topic="Engineering"))
        self.assertEqual(len(docs), 1)
        doc = docs[0]
        self.assertTrue(doc.custom_id.startswith("slackhist_v1_"))
        self.assertEqual(doc.container_tag, "org_shared")
        self.assertIn("# Slack history: #eng", doc.content)
        self.assertIn("TOPIC: Engineering", doc.content)
        self.assertIn("  ↳ ", doc.content)
        self.assertNotIn("bot reply", doc.content)
        self.assertNotIn("ancient", doc.content)
        again = build_slack_history_documents(self._input(messages, topic="Engineering"))
        self.assertEqual(again[0].custom_id, doc.custom_id)

    def test_large_day_splits_into_parts(self):
        messages = [{"ts": _ts(DAY - i), "user": "U1", "text": "x" * 900} for i in range(60)]
        docs = build_slack_history_documents(self._input(messages), max_chars=5_000)
        self.assertGreater(len(docs), 1)
        self.assertTrue(all(len(d.content) <= 5_000 for d in docs))
        self.assertEqual(len({d.custom_id for d in docs}), len(docs))

    def test_evidence(self):
        messages = [{"ts": _ts(DAY), "user": "U1", "text": "decided to move off the old queue"}]
        self.assertIn("decided to move", recent_slack_channel_evidence(messages, "UBOT"))


class IntroductionTest(unittest.TestCase):
    def test_no_model_means_no_themes_and_static_intro(self):
        themes = introduction.extract_channel_themes(
            channel_id="C1", channel_name="eng", evidence="- line", valid_message_ts=["1.0"]
        )
        self.assertEqual(themes, [])
        intro = introduction.compose_channel_introduction(channel_name="eng", installer_name="Maya", themes=[], checks=[])
        self.assertIn("Maya brought me in", intro)

    def test_fallback_mentions_first_theme(self):
        theme = introduction.ChannelTheme("C1:1", "C1", "eng", "Invoice Retries", "s", ["1.0"], "high")
        intro = introduction.fallback_introduction(installer_name="", themes=[theme], checks=[])
        self.assertIn("your admin brought me in", intro)
        self.assertIn("invoice retries stood out", intro)


class FakeSlack:
    def __init__(self, channels: list[SlackPublicChannel], history: dict[str, list[dict]]):
        self.channels = channels
        self.history = history
        self.posts: list[tuple] = []
        self.idempotent: dict[str, tuple] = {}
        self.updates: list[tuple] = []
        self.ephemerals: list[tuple] = []
        self.joined: list[str] = []
        self.join_retry_after: dict[str, int] = {}

    def list_slack_public_channels_page(self, cursor=None, limit=200):
        return SlackCursorPage(ok=True, items=list(self.channels), complete=True)

    def join_slack_public_channel(self, channel):
        if self.join_retry_after.pop(channel, None):
            return SlackJoinResult(ok=False, error="ratelimited", retry_after_seconds=7)
        self.joined.append(channel)
        return SlackJoinResult(ok=True, already_member=False)

    def get_slack_channel_history_page(self, channel, *, oldest, latest, cursor=None, limit=100):
        return SlackCursorPage(ok=True, items=[m for m in self.history.get(channel, []) if "thread_ts" not in m or m["thread_ts"] == m["ts"]], complete=True)

    def get_slack_thread_history_page(self, channel, thread_ts, cursor=None, limit=100):
        return SlackCursorPage(
            ok=True, items=[m for m in self.history.get(channel, []) if m.get("thread_ts") == thread_ts], complete=True
        )

    def post_slack_message(self, channel, text, thread_ts=None, blocks=None):
        self.posts.append((channel, text, blocks))
        return f"{len(self.posts)}.000"

    def post_slack_message_idempotent(self, channel, text, client_message_id, blocks=None):
        key = f"{channel}:{client_message_id}"
        if key in self.idempotent:
            return SlackPostResult(ok=True, ts=self.idempotent[key][0], deduped=True)
        ts = f"{len(self.idempotent) + 100}.000"
        self.idempotent[key] = (ts, text)
        self.posts.append((channel, text, blocks))  # a first post is a post like any other
        return SlackPostResult(ok=True, ts=ts)

    def update_slack_message(self, channel, ts, text, blocks=None):
        self.updates.append((channel, ts, blocks))
        return True

    def post_slack_ephemeral(self, channel, user, text, thread_ts=None, blocks=None):
        self.ephemerals.append((channel, user, text))
        return True

    def open_slack_conversation(self, user_id):
        return f"D{user_id}"

    def lookup_slack_user_info(self, user_id):
        return SlackUserInfoLookup(ok=True, user=SlackUserInfo(id=user_id, real_name="Maya Ortiz", display_name="Maya"))

    def get_slack_conversation_info(self, channel):
        return SlackConversationInfo(id=channel, name=channel.lower(), is_private=False)

    def get_slack_channel_history_result(self, channel, **opts):
        return SlackChannelHistoryResult(ok=True, messages=list(self.history.get(channel, [])), complete=True)


class _Scheduler:
    """Records scheduled jobs so a test can run them in order."""

    def __init__(self):
        self.jobs: list[tuple[float, str, dict]] = []
        self.delays: list[float] = []

    def schedule(self, delay, name, payload=None, job_id=None):
        self.jobs.append((delay, name, payload or {}))
        self.delays.append(delay)
        return job_id or uuid.uuid4().hex

    def drain(self, name: str, limit: int = 500) -> int:
        ran = 0
        while ran < limit:
            pending = [j for j in self.jobs if j[1] == name]
            if not pending:
                return ran
            job = pending[0]
            self.jobs.remove(job)
            scheduler._handlers[name](job[2])
            ran += 1
        raise AssertionError("rollout did not settle")


class RolloutTest(unittest.TestCase):
    def setUp(self):
        self.team = f"TCH{uuid.uuid4().hex[:8].upper()}"
        base = dict(is_member=False, is_archived=False, is_general=False, is_external=False)
        self.channels = [
            SlackPublicChannel(id=f"CGEN{self.team}", name="general", **{**base, "is_general": True}),
            SlackPublicChannel(id=f"CENG{self.team}", name="eng-platform", member_count=40, **base),
            SlackPublicChannel(id=f"CRND{self.team}", name="random", member_count=90, **base),
            SlackPublicChannel(id=f"CHR{self.team}", name="hr-team", member_count=80, **base),
            SlackPublicChannel(id=f"CPAY{self.team}", name="payments", member_count=20, **base),
        ]
        root = _ts(2 * DAY)
        self.history = {
            f"CENG{self.team}": [
                {"ts": root, "user": "U1", "text": f"auth service migration starts monday {self.team}", "reply_count": 1, "thread_ts": root},
                {"ts": _ts(2 * DAY - 30), "thread_ts": root, "user": "U2", "text": "I'll own the rollback plan"},
            ],
            f"CPAY{self.team}": [{"ts": _ts(DAY), "user": "U3", "text": f"invoice worker lag spiked {self.team}"}],
        }
        self.slack = FakeSlack(self.channels, self.history)
        self.ws = mock.Mock(client=self.slack, bot_user_id="UBOT", spec=["client", "bot_user_id", "team_id"])
        self.sched = _Scheduler()
        self.patches = [
            mock.patch.object(rollout, "_workspace", return_value=self.ws),
            mock.patch.object(scheduler, "schedule", side_effect=self.sched.schedule),
            mock.patch.object(rollout, "role_of", return_value="admin"),
            mock.patch.object(rollout, "_ensure_home_welcome"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        keys = list(kv().scan_iter(match=f"brain:rollout:{self.team}*"))
        if keys:
            kv().delete(*keys)
        _, graph = connect()
        graph.query("MATCH (m:Memory) WHERE m.channel_id ENDS WITH $team DETACH DELETE m", {"team": self.team})

    def _cards(self):
        rollout.ensure_public_channel_rollout_card({"team_id": self.team, "home_channel_id": "CHOME"})
        rollout.ensure_admin_rollout_card({"team_id": self.team, "installer_slack_user_id": "UADMIN"})

    def test_cards_only_dm_copy_has_button(self):
        self._cards()
        home_blocks, dm_blocks = self.slack.posts[0][2], self.slack.posts[1][2]
        self.assertEqual(self.slack.posts[1][0], "DUADMIN")
        self.assertFalse(any(b["type"] == "actions" for b in home_blocks))
        self.assertTrue(any(b["type"] == "actions" for b in dm_blocks))
        # Idempotent: a second ensure posts nothing new.
        self._cards()
        self.assertEqual(len(self.slack.posts), 2)

    def test_full_rollout_joins_ingests_and_introduces_once(self):
        self._cards()
        self.slack.join_retry_after[f"CENG{self.team}"] = 1
        rollout.start_public_channel_rollout({"team_id": self.team, "home_channel_id": "DUADMIN", "slack_user_id": "UADMIN"})
        self.assertIn("On it.", self.slack.ephemerals[-1][2])
        self.sched.drain(rollout.JOB_RUN_ROLLOUT)
        # Retry-After from Slack became the reschedule delay.
        self.assertIn(7, self.sched.delays)
        self.assertEqual(self.slack.joined.count(f"CENG{self.team}"), 1)

        overview = rollout.get_public_channel_rollout_overview(self.team)
        self.assertEqual(overview["status"], "done")
        self.assertNotIn(f"CGEN{self.team}", self.slack.joined)
        self.assertEqual(overview["introduced"], 4)
        _, graph = connect()
        docs = graph.query(
            "MATCH (m:Memory {scope: 'org_shared'}) WHERE m.source_type = 'slack_channel_history' AND m.channel_id ENDS WITH $team RETURN m.channel_id, m.content",
            {"team": self.team},
        ).result_set
        self.assertEqual({row[0] for row in docs}, {f"CENG{self.team}", f"CPAY{self.team}"})
        self.assertTrue(any("I'll own the rollback plan" in row[1] for row in docs))
        intros = len(self.slack.idempotent)

        # "Check for new public channels": already-introduced channels are not re-introduced.
        rollout.start_public_channel_rollout({"team_id": self.team, "home_channel_id": "DUADMIN", "slack_user_id": "UADMIN"})
        self.sched.drain(rollout.JOB_RUN_ROLLOUT)
        self.assertEqual(len(self.slack.idempotent), intros)
        self.assertEqual(rollout.get_public_channel_rollout_overview(self.team)["status"], "done")

    def test_members_cannot_start_rollout(self):
        self._cards()
        with mock.patch.object(rollout, "role_of", return_value="member"):
            rollout.start_public_channel_rollout({"team_id": self.team, "home_channel_id": "DUADMIN", "slack_user_id": "UBOB"})
        self.assertIn("Only a Company Brain organization admin", self.slack.ephemerals[-1][2])
        self.assertIsNone(rollout.current_run(self.team))

    def test_beachhead_picks_busiest_work_channels(self):
        self._cards()
        rollout.run_public_channel_beachhead({"team_id": self.team, "installer_slack_user_id": "UADMIN"})
        run = rollout.current_run(self.team)
        # hr is blocklisted, random is social (last in line), general is ineligible.
        self.assertEqual(json.loads(run["channel_filter_json"]), [f"CENG{self.team}", f"CPAY{self.team}"])
        self.sched.drain(rollout.JOB_RUN_ROLLOUT)
        self.assertEqual(sorted(self.slack.joined), sorted([f"CENG{self.team}", f"CPAY{self.team}"]))
        # No themes without a model: the card reports a quiet week instead of a read-out.
        last_card = self.slack.updates[-1][2][0]["text"]["text"]
        self.assertIn("Quiet week", last_card)

    def test_interaction_payload(self):
        body = "payload=" + json.dumps(
            {
                "type": "block_actions",
                "team": {"id": "T1"},
                "user": {"id": "U1"},
                "channel": {"id": "D1"},
                "actions": [{"action_id": rollout.PUBLIC_CHANNEL_ROLLOUT_ACTION_ID}],
            }
        )
        from urllib.parse import quote

        raw = ("payload=" + quote(body.removeprefix("payload="))).encode()
        self.assertEqual(
            rollout.parse_public_channel_rollout_interaction_payload(raw),
            {"team_id": "T1", "slack_user_id": "U1", "home_channel_id": "D1"},
        )
        self.assertIsNone(rollout.parse_public_channel_rollout_interaction_payload(b"payload=%7B%7D"))

    def test_deterministic_uuid(self):
        a = rollout.deterministic_uuid("seed")
        self.assertEqual(a, rollout.deterministic_uuid("seed"))
        self.assertEqual(uuid.UUID(a).version, 4)


class ObserveTest(unittest.TestCase):
    def setUp(self):
        self.team = f"TOB{uuid.uuid4().hex[:8].upper()}"
        self.channel = f"CPUB{self.team}"
        self.slack = FakeSlack([], {self.channel: [
            {"ts": _ts(120), "user": "U1", "text": "We decided Maya owns the auth service from now on"},
            {"ts": _ts(60), "bot_id": "B1", "text": "deploy finished"},
        ]})
        self.sched = _Scheduler()
        ws = mock.Mock(client=self.slack, bot_user_id="UBOT", installed_by_slack_user_id="UADMIN")
        self.patches = [
            mock.patch.object(scheduler, "schedule", side_effect=self.sched.schedule),
            mock.patch("src.domain.slack.workspace.get_workspace_by_team_id", return_value=ws),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        key = f"{self.team}:{self.channel}"
        for h in (observe._ROWS, observe._CURSOR, observe._SCAN, observe._RETRY, observe._PROBE):
            kv().hdel(h, key)
        kv().delete(observe._pending_key(key), observe._batch_key(key))

    def test_without_a_model_the_batch_retries_then_exhausts(self):
        observe.arm_channel_observe({"team_id": self.team, "channel": self.channel})
        delay, name, payload = self.sched.jobs.pop()
        self.assertEqual((delay, name), (observe.OBSERVE_DELAY_SECONDS, observe.JOB_RUN_CHANNEL_OBSERVE))

        observe.run_channel_observe(payload)
        key = f"{self.team}:{self.channel}"
        pending = observe._pending_rows(key)
        self.assertEqual([r["user"] for r in pending], ["U1", observe.BOT_AUTHOR_LABEL])
        self.assertEqual(observe.load_channel_retry_state(key)["attempt"], 1)
        self.assertEqual(self.sched.jobs[-1][0], observe.RETRY_BASE_DELAY_SECONDS)

        for _ in range(observe.MAX_RETRY_ATTEMPTS + 1):
            if not self.sched.jobs:
                break
            observe.run_channel_observe(self.sched.jobs.pop()[2])
        self.assertTrue(observe.load_channel_retry_state(key)["exhausted"])
        self.assertIsNone(observe._row(key))
        self.assertEqual(observe.get_observe_cursor({"team_id": self.team, "channel": self.channel}), "")

    def test_private_channel_is_never_observed(self):
        self.slack.get_slack_conversation_info = lambda channel: SlackConversationInfo(id=channel, is_private=True)
        observe.arm_channel_observe({"team_id": self.team, "channel": self.channel})
        observe.run_channel_observe(self.sched.jobs.pop()[2])
        self.assertEqual(observe._pending_rows(f"{self.team}:{self.channel}"), [])
        self.assertIsNone(observe._row(f"{self.team}:{self.channel}"))


def tearDownModule():
    if graph_name() == "company_channels_test":
        _, graph = connect()
        try:
            graph.delete()
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
