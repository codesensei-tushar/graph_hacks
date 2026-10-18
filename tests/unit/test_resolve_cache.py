"""``resolve.load_rows``'s cache and the word indexes that replace scanning every key. No database:
the graph and Redis are fakes."""

import random
import unittest
from unittest import mock

from src.db import falkordb_client
from src.db.falkordb_client import DarkGraph
from src.domain.knowledge import resolve
from src.domain.knowledge.resolve import EntityRow, Rows

ROWS = [
    EntityRow("billing-service", "Billing Service", "Service", aliases="billing,payments", kind="service"),
    EntityRow("api-gateway", "API Gateway", "Service", aliases="gateway,api", kind="service"),
    EntityRow("invoice-worker", "Invoice Worker", "Service", aliases="invoices", kind="service"),
    EntityRow("search-service", "Search Service", "Service", aliases="search", kind="service"),
    EntityRow("auth-service", "Auth Service", "Service", aliases="auth,login", kind="service"),
    EntityRow("memory-layer", "Memory Layer", "Service", kind="service"),
    EntityRow("maya", "Maya Ortiz", "Person"),
    EntityRow("arjun", "Arjun Mehta", "Person"),
    EntityRow("arjun-k", "Arjun Kapoor", "Person"),
    EntityRow("payments", "Payments", "Team"),
    EntityRow("doc-1", "Billing runbook", "Document", title="Retries and refunds"),
    EntityRow("t-102", "TKT-102", "Ticket", title="Annual plan renewals charge twice on retry"),
    EntityRow("pay-12", "PAY-12", "Ticket"),
    EntityRow("d-1", "Move memory off postgres", "Decision", aliases="memory decision"),
]


class FormsTest(unittest.TestCase):
    """Two words ``same_word`` equates always share a form, and a ``near_word`` typo is always in a
    bucket the lookup reads, so the indexes never hide a key the full scan would have found."""

    def vocabulary(self) -> list[str]:
        rng = random.Random(5)
        words = {token for row in ROWS for text in (row.name, row.aliases, row.title) for token in resolve.tokens(text)}
        words |= {"match", "matches", "box", "boxes", "service", "services", "auth", "author", "invoce", "biling", "paymnts"}
        for word in list(words):
            words |= {word + "s", word + "es", word[:-1], word + "x"}
            if len(word) > 3:
                i = rng.randrange(1, len(word))
                words |= {word[:i] + word[i + 1 :], word[:i] + "z" + word[i:]}
        return sorted(w for w in words if w)

    def test_same_words_share_a_form(self):
        words = self.vocabulary()
        for a in words:
            for b in words:
                if resolve.same_word(a, b):
                    self.assertTrue(resolve._forms(a) & resolve._forms(b), (a, b))

    def test_near_words_are_in_the_buckets_read(self):
        words = self.vocabulary()
        for a in words:
            for b in words:
                if resolve.near_word(a, b):
                    self.assertEqual(a[0], b[0])
                    self.assertLessEqual(abs(len(a) - len(b)), 2)


class IndexedMatchingTest(unittest.TestCase):
    QUESTIONS = [
        "who owns billing", "who knows the payments gateway", "what breaks if auth goes down", "the claude api",
        "the search cache", "tell me about arjun", "tell me about maya", "why did we move memory off postgres",
        "biling", "invoce worker", "paymnts", "retries refunds", "what is TKT-102", "PAY-12", "renewals", "nothing at all",
        "services", "who owns the invoices", "arjun mehta and the memory layer", "xyzzy plugh", "author", "memry layr",
    ]

    def test_the_indexes_give_what_scanning_every_key_gives(self):
        def every_key(rows, _words):
            return list(range(len(rows.keys())))

        labels = resolve.DEFAULT_LABELS
        for question in self.QUESTIONS:
            indexed = resolve.resolve_rows(question, Rows(ROWS), labels, known=resolve._graph_words(None, [], Rows(ROWS)))
            with mock.patch.object(resolve, "_match_positions", every_key), mock.patch.object(resolve, "_fuzzy_positions", every_key):
                scanned = resolve.resolve_rows(question, Rows(ROWS), labels, known=resolve._graph_words(None, [], Rows(ROWS)))
                plain = resolve.resolve_rows(question, list(ROWS), labels, known=lambda word: any(resolve.same_word(word, token) for row in ROWS for text in (row.name, row.aliases, row.title) for token in resolve.tokens(text)))
            with self.subTest(question=question):
                self.assertEqual(indexed, scanned)
                self.assertEqual(indexed, plain)


class FakeKv:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def incr(self, key):
        self.values[key] = self.values.get(key, 0) + 1


class FakeResult:
    def __init__(self, rows):
        self.result_set = rows


class FakeGraph:
    """Answers the UNION ALL of label scans from rows tagged with a scope; counts queries."""

    name = "g_rows"

    def __init__(self, nodes):
        self.nodes = nodes  # (node id, scope, EntityRow)
        self.queries = []

    def ro_query(self, q, params=None, timeout=None):
        self.queries.append(params)
        out = []
        for index, part in enumerate(q.split(" UNION ALL ")):
            label = part.split("MATCH (n:", 1)[1].split(")", 1)[0]
            scopes = params[f"scopes{index}"]
            for node, scope, row in self.nodes:
                if row.label == label and scope in scopes:
                    out.append([node, scope, row.id, row.name, row.label, row.aliases, row.title, row.kind])
        return FakeResult(out)


class LoadRowsCacheTest(unittest.TestCase):
    def setUp(self):
        resolve.clear_rows_cache()
        self.kv = FakeKv()
        patch = mock.patch.object(falkordb_client, "kv", return_value=self.kv)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(resolve.clear_rows_cache)
        nodes = [(index, "org_shared", row) for index, row in enumerate(ROWS)]
        nodes.append((100, "slack_channel_C1", EntityRow("secret", "Secret Project", "Service", kind="service")))
        nodes.append((50, "org_internal", EntityRow("hubspot", "HubSpot", "Service", kind="system")))
        self.graph = FakeGraph(nodes)

    def test_rows_are_read_once_per_generation(self):
        first = resolve.load_rows(self.graph, resolve.DEFAULT_LABELS, ["org_shared"])
        self.assertEqual(len(self.graph.queries), 1)
        self.assertIs(resolve.load_rows(self.graph, resolve.DEFAULT_LABELS, ["org_shared"]), first)
        self.assertEqual(len(self.graph.queries), 1)
        falkordb_client.bump_generation("g_rows")
        self.assertIsNot(resolve.load_rows(self.graph, resolve.DEFAULT_LABELS, ["org_shared"]), first)
        self.assertEqual(len(self.graph.queries), 2)

    def test_rows_come_in_node_order_and_only_from_the_scopes_asked(self):
        rows = resolve.load_rows(self.graph, ("Service",), ["org_shared", "org_internal"])
        self.assertEqual([row.id for row in rows][-2:], ["memory-layer", "hubspot"])  # node 5, then node 50
        self.assertNotIn("secret", [row.id for row in rows])
        self.assertIn("secret", [row.id for row in resolve.load_rows(self.graph, ("Service",), ["org_shared", "slack_channel_C1"])])

    def test_a_users_own_scopes_add_to_the_shared_rows_without_reading_them_again(self):
        resolve.load_rows(self.graph, resolve.DEFAULT_LABELS, ["org_shared", "org_internal"])
        resolve.load_rows(self.graph, resolve.DEFAULT_LABELS, ["org_shared", "org_internal", "slack_channel_C1"])
        self.assertEqual(len(self.graph.queries), 2)
        self.assertEqual({tuple(scopes) for scopes in self.graph.queries[1].values()}, {("slack_channel_C1",)})

    def test_a_dark_graph_and_an_unreadable_generation_are_never_cached(self):
        self.assertEqual(resolve.load_rows(DarkGraph("g_rows"), resolve.DEFAULT_LABELS, ["org_shared"]), [])
        with mock.patch.object(falkordb_client, "generation", return_value=None):
            resolve.load_rows(self.graph, ("Service",), ["org_shared"])
            resolve.load_rows(self.graph, ("Service",), ["org_shared"])
        self.assertEqual(len(self.graph.queries), 2)

    def test_the_cache_is_capped(self):
        with mock.patch.object(resolve, "ROWS_CACHE_MAX", 3):
            for n in range(6):
                resolve.load_rows(self.graph, ("Service",), ["org_shared", f"slack_channel_X{n}"])
            self.assertLessEqual(len(resolve._rows_cache), 3)
            self.assertLessEqual(len(resolve._part_cache), 3)

    def test_resolve_name_loads_every_label_once(self):
        resolve.resolve_name("who owns the claude api", scopes=["org_shared"], graph=self.graph)
        self.assertEqual(len(self.graph.queries), 1)


if __name__ == "__main__":
    unittest.main()
