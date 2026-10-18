"""Compound questions, a preamble before "who should I talk to", open-ticket phrasing, typo
suggestions, and small talk: pure, no graph."""

import types
import unittest
from unittest import mock

import tests  # noqa: F401  pins the test settings before src loads

from src.domain.knowledge import ask_service, resolve, smalltalk  # noqa: E402
from src.domain.knowledge.resolve import EntityRow  # noqa: E402
from src.domain.memory.writeback import SlackMemoryScope  # noqa: E402
from src.domain.slack import compute, format  # noqa: E402


class CompoundRouteTest(unittest.TestCase):
    def test_the_first_question_is_answered_and_the_second_offered(self):
        self.assertEqual(ask_service.route("who owns billing and what breaks if auth goes down"), ("who_owns", {"name": "who owns billing"}))
        self.assertEqual(
            ask_service.split_compound("who owns billing and what breaks if auth goes down"), ("who owns billing", "what breaks if auth goes down")
        )
        self.assertEqual(ask_service.split_compound("who owns billing; who knows auth?")[1], "who knows auth?")

    def test_one_question_about_more_people_or_with_context_first_is_not_split(self):
        for question in (
            "who owns billing, and who else has worked on it",
            "billing is failing, what changed",
            "what breaks if auth goes down, two hops out",
        ):
            with self.subTest(question=question):
                self.assertIsNone(ask_service.split_compound(question)[1])
        self.assertEqual(ask_service.route("billing is failing, what changed")[0], "what_changed")

    def test_a_rest_about_the_first_parts_thing_is_split(self):
        # It used to stay one question; the agent now asks the rest about what the first part named.
        self.assertEqual(
            ask_service.split_compound("what does the invoice worker depend on and what depends on it"),
            ("what does the invoice worker depend on", "what depends on it"),
        )
        self.assertEqual(ask_service.split_compound("who owns billing and what breaks if it goes down")[1], "what breaks if it goes down")
        self.assertEqual(ask_service.split_compound("what if auth breaks? also who to contact"), ("what if auth breaks", "who to contact"))
        self.assertEqual(ask_service.split_compound("who owns billing also who to contact"), ("who owns billing", "who to contact"))

    def test_who_should_i_talk_to_beats_i_am_new(self):
        question = "I am new on payments and need to fix a bug, who should I talk to about the invoice worker"
        self.assertEqual(ask_service.route(question), ("who_knows", {"name": "who should I talk to about the invoice worker", "context": question}))
        # Nothing after the phrase: the whole question is the subject.
        self.assertEqual(ask_service.route("billing is failing, who should I ping"), ("who_knows", {"name": "billing is failing, who should I ping"}))
        self.assertEqual(ask_service.route("I just joined payments")[0], "onboarding_brief")

    def test_open_ticket_phrasings_route_alike(self):
        expected = ("find", {"label": "Ticket", "state": "open", "about": "billing"})
        for question in ("open tickets about billing", "what are the open tickets about billing", "list open tickets about billing", "show me open tickets about billing"):
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question), expected)


class TypoTest(unittest.TestCase):
    ROWS = [
        EntityRow("billing-service", "Billing Service", "Service", aliases="billing,payments"),
        EntityRow("invoice-worker", "Invoice Worker", "Service", aliases="invoices"),
        EntityRow("auth-service", "Auth Service", "Service", aliases="auth"),
    ]

    def test_near_words(self):
        self.assertTrue(resolve.near_word("biling", "billing"))
        self.assertTrue(resolve.near_word("invoce", "invoice"))
        self.assertTrue(resolve.near_word("paymnets", "payments"))
        self.assertFalse(resolve.near_word("author", "auth"))
        self.assertFalse(resolve.near_word("killing", "billing"))
        self.assertFalse(resolve.near_word("bill", "ball"))

    def test_a_misspelt_single_word_is_suggested_never_resolved(self):
        resolution = resolve.resolve_rows("who owns biling", self.ROWS)
        self.assertFalse(resolution.found)
        self.assertEqual(resolution.candidate_names(), ["Billing Service"])
        self.assertIn("Nearest: Billing Service", resolve.describe_miss(resolution))
        self.assertEqual(resolve.resolve_rows("who owns the invoce worker", self.ROWS).candidate_names()[0], "Invoice Worker")


class SmallTalkTest(unittest.TestCase):
    def test_greetings_and_what_can_you_do_are_help(self):
        for message in ("hi", "Hello!", "hey there", "good morning", "what can you do?", "hi, what can you do", "help me"):
            with self.subTest(message=message):
                self.assertEqual(smalltalk.kind(message), smalltalk.HELP)
        self.assertTrue(smalltalk.reply("hi").startswith("Hi! Ask me about the company"))

    def test_thanks_is_an_acknowledgement(self):
        for message in ("thanks", "Thanks!", "thank you so much", "cheers", "ok", "got it."):
            with self.subTest(message=message):
                self.assertEqual(smalltalk.reply(message), smalltalk.THANKS_TEXT)

    def test_a_question_is_never_small_talk(self):
        for message in ("hi, who owns billing", "thanks, and what breaks if auth goes down", "who owns billing", "help with billing"):
            with self.subTest(message=message):
                self.assertIsNone(smalltalk.kind(message))

    def test_slack_answers_small_talk_without_the_graph(self):
        def request(text):
            return types.SimpleNamespace(
                question=text, asker_slack_user_id="U1", memory_scope=SlackMemoryScope(kind="shared", channel_id="C1"), read_tags=["org_shared"]
            )

        with mock.patch.object(compute, "channel_names", return_value={}):
            self.assertEqual(compute._command(request("hello")).reply, format.HELP_TEXT)
            self.assertEqual(compute._command(request("what can you do?")).reply, format.HELP_TEXT)
            self.assertEqual(compute._command(request("thanks!")).reply, smalltalk.THANKS_TEXT)
            self.assertIsNone(compute._command(request("who owns billing")))


if __name__ == "__main__":
    unittest.main()
