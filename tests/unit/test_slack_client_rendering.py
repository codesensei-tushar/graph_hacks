"""Slack client, formatting, references, streams, attachments, traces (port of company-brain)."""

import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

from slack_sdk.errors import SlackApiError

from src.integrations.slack import attachments, debug_trace_delivery, message_trace
from src.integrations.slack.client import SlackClient, SlackConversation
from src.integrations.slack.format import (
    SLACK_CONTINUATION_SENTENCE,
    humanize_tool_action,
    normalize_text_preserving_code_fences,
    plain_text_fallback,
    split_slack_markdown,
    to_slack_mrkdwn,
)
from src.integrations.slack.references import create_slack_reply_reference_resolver, format_slack_channel_references
from src.integrations.slack.stream import SlackStreamSession


def ok(data: dict, status: int = 200, headers: dict | None = None):
    return SimpleNamespace(data={"ok": True, **data}, status_code=status, headers=headers or {})


def fail(error: str, status: int = 200, headers: dict | None = None):
    response = SimpleNamespace(data={"ok": False, "error": error}, status_code=status, headers=headers or {})
    return SlackApiError(error, response)


class FakeWeb:
    """Routes api_call by method name to queued responses; records calls."""

    def __init__(self, routes: dict):
        self.routes = {method: list(responses) for method, responses in routes.items()}
        self.calls: list[tuple[str, dict]] = []

    def api_call(self, method, http_verb="POST", json=None, params=None, data=None):
        self.calls.append((method, json or params or data or {}))
        queue = self.routes.get(method)
        if not queue:
            raise AssertionError(f"unexpected Slack call {method}")
        result = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(result, Exception):
            raise result
        return result

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


def client(routes: dict) -> tuple[SlackClient, FakeWeb]:
    web = FakeWeb(routes)
    return SlackClient("xoxb-test", web=web), web


class FormatTest(unittest.TestCase):
    def test_dashes_normalized_and_code_spacing_kept(self):
        # Like the original, dashes change everywhere; only space collapsing skips fenced code.
        text = "alpha  —  beta 3–5 ```a  b```"
        self.assertEqual(normalize_text_preserving_code_fences(text), "alpha - beta 3-5 ```a  b```")

    def test_mrkdwn_subset(self):
        text = "## Title\n**bold** and [docs](https://example.com)\n| a | b |\n|---|---|\n| 1 | 2 |\n---"
        self.assertEqual(to_slack_mrkdwn(text), "*Title*\n*bold* and <https://example.com|docs>\na  ·  b\n1  ·  2")

    def test_plain_text_fallback(self):
        self.assertEqual(plain_text_fallback("# Hi\n**x** [y](https://e.com) `z`"), "Hi\nx y (https://e.com) z")

    def test_split_prefers_sentences(self):
        text = ("One sentence here. " * 10).strip()
        chunks = split_slack_markdown(text, 80)
        self.assertTrue(all(len(chunk) <= 80 for chunk in chunks))
        self.assertTrue(all(chunk.endswith(".") for chunk in chunks))
        self.assertEqual(" ".join(chunks), text)

    def test_split_reopens_code_fences(self):
        code = "```py\n" + "\n".join(f"line_{i} = {i}" for i in range(40)) + "\n```"
        chunks = split_slack_markdown(code, 200)
        self.assertGreater(len(chunks), 1)
        self.assertIn(SLACK_CONTINUATION_SENTENCE, chunks[0])
        self.assertTrue(chunks[1].startswith("```py\n"))
        for chunk in chunks:
            self.assertEqual(chunk.count("```") % 2, 0)

    def test_split_empty(self):
        self.assertEqual(split_slack_markdown("   "), [])

    def test_humanize_tool_action(self):
        self.assertEqual(humanize_tool_action("tickets.tickets-create_issue"), "Tickets · Create issue")
        self.assertEqual(humanize_tool_action("Create ticket"), "Create ticket")


class ReferencesTest(unittest.TestCase):
    channels = [SlackConversation(id="C0123ABCDE", name="eng", is_private=False)]

    def test_names_and_ids_become_links(self):
        out = format_slack_channel_references("see #eng and C0123ABCDE.", self.channels, "T1")
        link = "[#eng](https://app.slack.com/client/T1/C0123ABCDE)"
        self.assertEqual(out, f"see {link} and {link}.")

    def test_unknown_name_stays_and_unknown_id_is_generic(self):
        out = format_slack_channel_references("#random and C9999999999", self.channels, "T1")
        self.assertEqual(out, "#random and that channel")

    def test_code_links_and_slack_tokens_untouched(self):
        text = "`#eng` [#eng](https://x.com) <#C0123ABCDE|eng>"
        self.assertEqual(format_slack_channel_references(text, self.channels, "T1"), text)

    def test_resolver_loads_directory_once(self):
        loader = mock.Mock(return_value=self.channels)
        resolve = create_slack_reply_reference_resolver("T1", loader)
        resolve("#eng")
        resolve("#eng again")
        self.assertEqual(resolve("no channels"), "no channels")
        loader.assert_called_once()


class ClientTest(unittest.TestCase):
    def test_post_message_formats_and_returns_ts(self):
        slack, web = client({"chat.postMessage": [ok({"ts": "1.1"})]})
        self.assertEqual(slack.post_message("C1", "**hi**", "0.1"), "1.1")
        self.assertEqual(web.calls[0][1]["text"], "*hi*")
        self.assertEqual(web.calls[0][1]["thread_ts"], "0.1")

    def test_post_message_failure_returns_none(self):
        slack, _ = client({"chat.postMessage": [fail("channel_not_found")]})
        self.assertIsNone(slack.post_message("C1", "hi"))
        self.assertIsNone(slack.post_message("", "hi"))

    def test_idempotent_post_detects_dedupe(self):
        slack, _ = client({"chat.postMessage": [ok({"ts": "1000.0"})]})
        result = slack.post_slack_message_idempotent("C1", "hi", "abc")
        self.assertTrue(result.ok and result.deduped)

    def test_rate_limit_surfaces_retry_after(self):
        slack, _ = client({"conversations.join": [fail("ratelimited", 429, {"retry-after": "7"})]})
        result = slack.join_slack_public_channel("C1")
        self.assertFalse(result.ok)
        self.assertEqual(result.retry_after_seconds, 7)

    def test_already_in_channel_counts_as_joined(self):
        slack, _ = client({"conversations.join": [fail("already_in_channel")]})
        self.assertEqual(slack.join_slack_public_channel("C1").already_member, True)

    def test_reply_delivery_falls_back_to_mrkdwn(self):
        slack, web = client({"chat.postMessage": [fail("invalid_blocks"), ok({"ts": "2.2"})]})
        delivery = slack.post_slack_reply_delivery("C1", "**answer**", "0.1")
        self.assertEqual((delivery.ts, delivery.complete), ("2.2", True))
        self.assertEqual(web.calls[0][1]["blocks"][0]["type"], "markdown")
        self.assertEqual(web.calls[1][1]["blocks"][0]["type"], "section")

    def test_thread_history_pages_and_dedupes(self):
        slack, _ = client(
            {
                "conversations.replies": [
                    ok({"messages": [{"ts": "1"}, {"ts": "2"}], "response_metadata": {"next_cursor": "c2"}}),
                    ok({"messages": [{"ts": "2"}, {"ts": "3"}]}),
                ]
            }
        )
        history = slack.get_slack_thread_history("C1", "1", max_pages=3, max_messages=10)
        self.assertTrue(history.ok and history.complete)
        self.assertEqual([m["ts"] for m in history.messages], ["1", "2", "3"])

    def test_thread_history_keeps_failed_cursor(self):
        slack, _ = client(
            {
                "conversations.replies": [
                    ok({"messages": [{"ts": "1"}], "response_metadata": {"next_cursor": "c2"}}),
                    fail("internal_error"),
                ]
            }
        )
        history = slack.get_slack_thread_history("C1", "1", max_pages=3, max_messages=10)
        self.assertFalse(history.ok)
        self.assertEqual(history.next_cursor, "c2")
        self.assertEqual(len(history.messages), 1)

    def test_channel_history_returned_oldest_first(self):
        slack, _ = client({"conversations.history": [ok({"messages": [{"ts": "3"}, {"ts": "2"}], "has_more": False})]})
        result = slack.get_slack_channel_history_result("C1")
        self.assertEqual([m["ts"] for m in result.messages], ["2", "3"])

    def test_conversation_info_reasons(self):
        slack, _ = client({"conversations.info": [fail("channel_not_found")]})
        self.assertEqual(slack.lookup_slack_conversation_info("C1").reason, "conversation_unavailable")
        slack, _ = client({"conversations.info": [fail("ratelimited", 429)]})
        self.assertEqual(slack.lookup_slack_conversation_info("C1").reason, "slack_api_error")
        slack, _ = client({"conversations.info": [ok({"channel": {"id": "G1", "name": "x", "is_private": True, "topic": {"value": " t "}}})]})
        info = slack.conversation_info("G1")
        self.assertTrue(info.is_private)
        self.assertEqual(info.topic, "t")

    def test_user_info_maps_profile(self):
        member = {"id": "U1", "name": "maya", "real_name": "Maya O", "is_restricted": True, "tz": "UTC", "profile": {"email": "m@x.test"}}
        slack, _ = client({"users.info": [ok({"user": member})]})
        user = slack.user_info("U1")
        self.assertEqual((user.real_name, user.handle, user.email, user.is_restricted), ("Maya O", "maya", "m@x.test", True))
        slack, _ = client({"users.info": [fail("user_not_found")]})
        self.assertIsNone(slack.user_info("U1"))
        self.assertEqual(slack.lookup_slack_user_info("U1").reason, "user_not_found")

    def test_reactions(self):
        slack, _ = client({"reactions.add": [fail("already_reacted")]})
        self.assertEqual(slack.add_slack_reaction_detailed("C1", "1", "eyes").outcome, "already_present")
        self.assertFalse(slack.add_slack_reaction_detailed("", "1", "eyes").ok)

    def test_public_channel_page_flags_external(self):
        channels = [
            {"id": "C1", "name": "eng", "is_member": True, "num_members": 5},
            {"id": "C2", "name": "partner", "is_ext_shared": True},
            {"id": "C3", "name": "org", "is_shared": True, "is_org_shared": True},
        ]
        slack, _ = client({"conversations.list": [ok({"channels": channels})]})
        page = slack.list_slack_public_channels_page()
        self.assertEqual([c.is_external for c in page.items], [False, True, False])
        self.assertEqual(page.items[0].member_count, 5)
        self.assertTrue(page.complete)

    def test_uninstall_outcomes(self):
        slack, _ = client({"apps.uninstall": [fail("token_revoked")]})
        self.assertEqual(slack.uninstall_slack_app("id", "secret"), "terminal")
        slack, _ = client({"apps.uninstall": [fail("ratelimited")]})
        self.assertEqual(slack.uninstall_slack_app("id", "secret"), "transient")
        slack, _ = client({"apps.uninstall": [fail("not_allowed")], "auth.revoke": [ok({})]})
        self.assertEqual(slack.uninstall_slack_app("id", "secret"), "revoked")


class StreamTest(unittest.TestCase):
    def test_public_progress_card_replaced_by_fresh_answer(self):
        slack, web = client(
            {
                "chat.postMessage": [ok({"ts": "9.1"}), ok({"ts": "9.2"})],
                "chat.update": [ok({})],
                "chat.delete": [ok({})],
            }
        )
        checkpoints = []
        session = SlackStreamSession(
            slack, channel="C1", thread_ts="1.0", team_id="T1", public_progress=True, on_delivery_checkpoint=checkpoints.append
        )
        session.progress.card("call-1", "Searching the graph", "in_progress")
        session.progress.card("call-1", "Searching the graph", "complete")
        result = session.finalize("Here is the answer.", failed=False)
        self.assertEqual((result.streamed, result.message_ts), (True, "9.2"))
        self.assertEqual(web.methods(), ["chat.postMessage", "chat.update", "chat.postMessage", "chat.delete"])
        self.assertEqual(web.calls[0][1]["blocks"][0]["type"], "plan")
        self.assertIn({"progress_message_ts": None}, checkpoints)
        self.assertFalse(session.finalize("again", failed=False).streamed)

    def test_dm_stream_appends_and_stops(self):
        slack, web = client(
            {"chat.startStream": [ok({"ts": "5.5"})], "chat.appendStream": [ok({})], "chat.stopStream": [ok({})]}
        )
        session = SlackStreamSession(slack, channel="D1", thread_ts="1.0", team_id="T1", recipient_user_id="U1")
        session.progress.card("call-1", "Reading the thread", "in_progress")
        result = session.finalize("Done.", failed=False)
        self.assertEqual((result.streamed, result.message_ts), (True, "5.5"))
        self.assertEqual(web.methods(), ["chat.startStream", "chat.appendStream", "chat.appendStream", "chat.appendStream", "chat.stopStream"])
        first_chunks = web.calls[1][1]["chunks"]
        self.assertEqual(first_chunks[0], {"type": "plan_update", "title": "Working on it"})
        self.assertEqual(first_chunks[1]["status"], "in_progress")

    def test_stream_unavailable_falls_back_quietly(self):
        slack, _ = client({"chat.startStream": [fail("not_allowed")]})
        session = SlackStreamSession(slack, channel="D1", thread_ts="1.0", team_id="T1")
        session.progress.card("call-1", "Working", "in_progress")
        session.progress.card("call-2", "Working more", "in_progress")
        self.assertTrue(session._stream_dead)

    def test_narration_dedupes_and_caps(self):
        slack, web = client({"chat.postMessage": [ok({"ts": "3.3"})]})
        session = SlackStreamSession(slack, channel="C1", thread_ts="1.0", team_id="T1", public_progress=True)
        self.assertTrue(session.progress.narrate("Looking now."))
        self.assertFalse(session.progress.narrate("Looking now."))
        for i in range(20):
            session.progress.narrate(f"step {i}")
        self.assertEqual(len(web.calls), 10)


class AttachmentsTest(unittest.TestCase):
    def test_normalize_message_content_folds_attachments(self):
        message = {
            "text": "see this",
            "attachments": [{"title": "Report", "title_link": "https://e.com/r", "text": "body", "footer": "ft"}],
        }
        self.assertEqual(attachments.normalize_slack_message_content(message), "see this\nReport\n<https://e.com/r>\nbody\nft")
        exact = {"attachments": [{"title": "<https://e.com/r|r>", "title_link": "https://e.com/other"}]}
        self.assertEqual(attachments.normalize_slack_message_content(exact), "<https://e.com/r|r>")

    def test_collect_files_dedupes_and_caps(self):
        messages = [{"files": [{"id": f"F{i}", "name": f"f{i}.png", "mimetype": "image/png"}]} for i in range(8)]
        messages.append({"files": [{"id": "F7", "name": "f7.png"}]})
        files = attachments.collect_turn_files(messages)
        self.assertEqual([f.id for f in files], ["F3", "F4", "F5", "F6", "F7"])

    def test_attachment_hint(self):
        files = [attachments.SlackFileRef("F1", "a.png", "image/png"), attachments.SlackFileRef("F2", "b.zip", "application/zip")]
        self.assertEqual(
            attachments.format_message_attachment_hint(files),
            "[attachments: a.png (image/png); b.zip (application/zip, not sent to model)]",
        )

    def test_oversized_file_skipped(self):
        slack = mock.Mock()
        big = attachments.SlackFileRef("F1", "a.png", "image/png", size=attachments.MAX_ATTACHMENT_BYTES + 1, url_private="https://x")
        self.assertIsNone(attachments.download_slack_file(slack, big))
        slack.download_private_url.assert_not_called()


class TraceTest(unittest.TestCase):
    def test_debug_trace_falls_back_to_private_dm(self):
        slack = mock.Mock()
        slack.post_slack_ephemeral.return_value = False
        slack.open_slack_conversation.return_value = "D9"
        slack.post_slack_message.return_value = "1.1"
        delivery = debug_trace_delivery.deliver_slack_debug_trace(slack, channel="C1", reactor_user="U1", text="trace")
        self.assertEqual(delivery, "private_dm")
        slack.post_slack_message.assert_called_once_with("D9", "trace")

    def test_debug_trace_in_dm_posts_directly(self):
        slack = mock.Mock()
        slack.post_slack_message.return_value = None
        self.assertEqual(debug_trace_delivery.deliver_slack_debug_trace(slack, channel="D1", reactor_user="U1", text="t"), "failed")

    def test_message_trace_round_trip(self):
        channel = f"CTEST{uuid.uuid4().hex[:8]}"
        try:
            message_trace.record_message_trace(channel, "1.1", "trace-a", "0.1")
            message_trace.record_message_trace(channel, "1.1", "trace-b")
            records = message_trace.lookup_message_trace_records(channel, ["1.1", "2.2", ""])
            self.assertEqual(records["1.1"].trace_id, "trace-b")
            self.assertEqual(records["1.1"].thread_ts, "0.1")
            self.assertNotIn("2.2", records)
        finally:
            from src.db.falkordb_client import kv

            kv().delete(f"brain:slack:message-trace:{channel}")


if __name__ == "__main__":
    unittest.main()
