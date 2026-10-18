"""``search`` and the refusal fallback without a database: which questions they take, the words searched, the order of the
fallback steps, and index creation that never crashes."""

import unittest
from unittest import mock

from src.domain.knowledge import ask_service, fallback, search
from src.domain.knowledge.dto import ToolResult


class _Result:
    def __init__(self, rows):
        self.result_set = rows


class _Graph:
    """Answers ``db.indexes()`` with the given rows; ``query`` runs ``on_query``."""

    def __init__(self, index_rows=(), on_query=None, list_fails=False):
        self.index_rows = list(index_rows)
        self.on_query = on_query
        self.list_fails = list_fails
        self.created: list[tuple[str, str]] = []

    def ro_query(self, statement, params=None):
        if self.list_fails:
            raise RuntimeError("connection reset")
        return _Result(self.index_rows)

    def query(self, statement, params=None):
        if self.on_query is not None:
            self.on_query(statement, params)
        self.created.append((params["label"], params["field"]))
        return _Result([])


class TopicQuestionTest(unittest.TestCase):
    def test_topic_questions_and_their_words(self):
        cases = {
            "who knows about refunds": ["refunds"],
            "who can help with webhooks": ["webhooks"],
            "who is the expert on pdf layout": ["pdf", "layout"],
            "who knows about join tables": ["join", "tables"],
            "who knows about the refund policy": ["refund", "policy"],
        }
        for question, terms in cases.items():
            with self.subTest(question=question):
                self.assertTrue(search.TOPIC_QUESTION.search(question))
                self.assertEqual(search.topic_terms(question), terms)
        self.assertEqual(search.full_text_query(["pdf", "layout"]), "pdf layout")

    def test_a_named_thing_is_not_a_topic_question(self):
        for question in ("who knows the payments gateway", "who owns billing", "who knows billing", "what breaks if auth goes down"):
            with self.subTest(question=question):
                self.assertIsNone(search.TOPIC_QUESTION.search(question))

    def test_routes(self):
        # "who knows about refunds" still goes to who_knows, which tries the fallback before refusing.
        for question in ("who knows about refunds", "who can help with webhooks", "who knows the payments gateway", "search-service", "search brain skips user scopes"):
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question)[0], "who_knows")
        # Words to look for go straight to search.
        for question in ("search for webhooks", "look up refunds", "anything about pdf layout", "mentions of TKT-102", "search the graph for retries"):
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question), ("search", {"text": question}))
        # The pins search must not take.
        for question, tool in (
            ("what have we said about billing", "related_memories"),
            ("who said the retries were flaky", "recall"),
            ("why did we move the memory layer off postgres", "decision_trace"),
            ("what should I read before touching the invoice worker", "reading_list"),
        ):
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question)[0], tool)

    def test_no_topic_no_search(self):
        with mock.patch.object(search, "search") as searched:
            self.assertIsNone(search.if_topic("who knows the payments gateway", ["org_shared"]))
            searched.assert_not_called()

    def test_a_topic_that_comes_up_nowhere_is_no_answer(self):
        with mock.patch.object(search, "search", return_value=ToolResult(tool="search", answer="Nothing", found=False)):
            self.assertIsNone(search.if_topic("who knows about refunds", ["org_shared"]))


class FallbackTest(unittest.TestCase):
    MISS = ToolResult(tool="who_knows", answer="Nothing in the graph matches “x”.", found=False, candidates=["X"])

    def test_the_first_step_that_answers_wins(self):
        calls = []

        def nothing(question, scopes):
            calls.append("nothing")
            return None

        def not_found(question, scopes):
            calls.append("not_found")
            return ToolResult(tool="explore", answer="", found=False)

        def found(question, scopes):
            calls.append("found")
            return ToolResult(tool="search", answer="yes")

        def never(question, scopes):
            calls.append("never")
            return ToolResult(tool="late", answer="no")

        with mock.patch.object(fallback, "STEPS", (nothing, not_found, found, never)):
            self.assertEqual(fallback.before_refusing("q", ["org_shared"], self.MISS).answer, "yes")
        self.assertEqual(calls, ["nothing", "not_found", "found"])

    def test_a_failing_step_is_logged_and_skipped(self):
        def boom(question, scopes):
            raise RuntimeError("index gone")

        with mock.patch.object(fallback, "STEPS", (boom,)), self.assertLogs("src.domain.knowledge.fallback", level="ERROR"):
            self.assertIs(fallback.before_refusing("q", ["org_shared"], self.MISS), self.MISS)

    def test_no_answer_returns_the_refusal_itself(self):
        with mock.patch.object(fallback, "STEPS", ()):
            self.assertIs(fallback.before_refusing("q", ["org_shared"], self.MISS), self.MISS)

    def test_search_is_the_last_step(self):
        # Search, then what was said in chat, last.
        self.assertEqual(fallback.STEPS[-2:], (fallback._search, fallback._recall))


class EnsureIndexesTest(unittest.TestCase):
    def test_creates_only_the_missing_fields(self):
        present = [("Document", {"name": ["FULLTEXT"], "summary": ["FULLTEXT"]}), ("Ticket", {"title": ["FULLTEXT"], "id": ["RANGE"]})]
        graph = _Graph(present)
        created = search.ensure_search_indexes(graph)
        self.assertEqual(created, ["Ticket.summary", "PullRequest.title", "PullRequest.summary", "Decision.name", "Decision.summary"])
        self.assertNotIn(("Document", "name"), graph.created)

    def test_everything_present_is_a_no_op(self):
        present = [(label, {name: ["FULLTEXT"] for name in fields}) for label, fields in search.INDEXED.items()]
        graph = _Graph(present)
        self.assertEqual(search.ensure_search_indexes(graph), [])
        self.assertEqual(graph.created, [])

    def test_a_failure_is_logged_and_the_rest_carry_on(self):
        def fail_on_tickets(statement, params):
            if params["label"] == "Ticket":
                raise RuntimeError("out of memory")

        graph = _Graph(on_query=fail_on_tickets)
        with self.assertLogs("src.domain.knowledge.search", level="WARNING"):
            created = search.ensure_search_indexes(graph)
        self.assertNotIn("Ticket.title", created)
        self.assertIn("Decision.summary", created)

    def test_already_indexed_is_not_an_error(self):
        def already(statement, params):
            raise RuntimeError(f"Attribute '{params['field']}' is already indexed")

        with self.assertNoLogs("src.domain.knowledge.search", level="WARNING"):
            self.assertEqual(search.ensure_search_indexes(_Graph(on_query=already)), [])

    def test_an_unreachable_graph_never_raises(self):
        with self.assertLogs("src.domain.knowledge.search", level="ERROR"):
            self.assertEqual(search.ensure_search_indexes(_Graph(list_fails=True)), [])

    def test_startup_builds_every_index_through_the_schema(self):
        from dataclasses import replace

        from src import main

        calls = []
        with (
            mock.patch.object(main, "settings", replace(main.settings, seed_on_startup=False)),
            mock.patch.object(main.schema, "ensure", side_effect=lambda *args, **kwargs: calls.append("schema")),
            mock.patch.object(main, "_other_orgs", return_value=[]),
        ):
            main._seed_if_empty()
        self.assertEqual(calls, ["schema"])
