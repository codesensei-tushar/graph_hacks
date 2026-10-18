"""``topic_experts`` without a database: which questions it takes, the words it searches, and index creation that never crashes."""

import unittest
from unittest import mock

from src.domain.knowledge import ask_service, topics


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
                self.assertTrue(topics.TOPIC_QUESTION.search(question))
                self.assertEqual(topics.topic_terms(question), terms)
        self.assertEqual(topics.full_text_query(["pdf", "layout"]), "pdf layout")

    def test_a_named_thing_is_not_a_topic_question(self):
        for question in ("who knows the payments gateway", "who owns billing", "who knows billing", "what breaks if auth goes down"):
            with self.subTest(question=question):
                self.assertIsNone(topics.TOPIC_QUESTION.search(question))

    def test_no_route_changes(self):
        # Topics never add a route: "who knows about refunds" still goes to who_knows, which asks topics before refusing.
        for question in ("who knows about refunds", "who can help with webhooks", "who knows the payments gateway"):
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question)[0], "who_knows")

    def test_the_refusal_stands_when_the_question_names_no_topic(self):
        miss = object()
        with mock.patch.object(topics, "topic_experts") as search:
            self.assertIs(topics.topic_if_asked("who knows the payments gateway", ["org_shared"], miss), miss)
            search.assert_not_called()

    def test_a_failing_search_keeps_the_refusal(self):
        miss = object()
        with mock.patch.object(topics, "topic_experts", side_effect=RuntimeError("index gone")):
            self.assertIs(topics.topic_if_asked("who knows about refunds", ["org_shared"], miss), miss)


class EnsureIndexesTest(unittest.TestCase):
    def test_creates_only_the_missing_fields(self):
        present = [("Document", {"name": ["FULLTEXT"], "summary": ["FULLTEXT"]}), ("Ticket", {"title": ["FULLTEXT"], "id": ["RANGE"]})]
        graph = _Graph(present)
        created = topics.ensure_topic_indexes(graph)
        self.assertEqual(created, ["Ticket.summary", "PullRequest.title", "PullRequest.summary", "Decision.name", "Decision.summary"])
        self.assertNotIn(("Document", "name"), graph.created)

    def test_everything_present_is_a_no_op(self):
        present = [(label, {name: ["FULLTEXT"] for name in fields}) for label, fields in topics.INDEXED.items()]
        graph = _Graph(present)
        self.assertEqual(topics.ensure_topic_indexes(graph), [])
        self.assertEqual(graph.created, [])

    def test_a_failure_is_logged_and_the_rest_carry_on(self):
        def fail_on_tickets(statement, params):
            if params["label"] == "Ticket":
                raise RuntimeError("out of memory")

        graph = _Graph(on_query=fail_on_tickets)
        with self.assertLogs("src.domain.knowledge.topics", level="WARNING"):
            created = topics.ensure_topic_indexes(graph)
        self.assertNotIn("Ticket.title", created)
        self.assertIn("Decision.summary", created)

    def test_already_indexed_is_not_an_error(self):
        def already(statement, params):
            raise RuntimeError(f"Attribute '{params['field']}' is already indexed")

        with self.assertNoLogs("src.domain.knowledge.topics", level="WARNING"):
            self.assertEqual(topics.ensure_topic_indexes(_Graph(on_query=already)), [])

    def test_an_unreachable_graph_never_raises(self):
        with self.assertLogs("src.domain.knowledge.topics", level="ERROR"):
            self.assertEqual(topics.ensure_topic_indexes(_Graph(list_fails=True)), [])

    def test_startup_creates_them_after_the_memory_indexes(self):
        from dataclasses import replace

        from src import main

        calls = []
        with (
            mock.patch.object(main, "settings", replace(main.settings, seed_on_startup=False)),
            mock.patch.object(main, "ensure_indexes", side_effect=lambda: calls.append("memory")),
            mock.patch.object(main, "ensure_topic_indexes", side_effect=lambda: calls.append("topics")),
        ):
            main._seed_if_empty()
        self.assertEqual(calls, ["memory", "topics"])
