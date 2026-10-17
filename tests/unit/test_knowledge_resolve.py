"""Name resolution without a database: whole-phrase matching, the refuse-to-guess rules, and candidates."""

import unittest

from src.domain.knowledge.resolve import Entity, EntityRow, core, describe_miss, entities_in_rows, join_names, resolve_rows, same_word

ROWS = [
    EntityRow("auth-service", "Auth Service", "Service", "auth, authentication, login, sessions"),
    EntityRow("api-gateway", "API Gateway", "Service", "gateway, api"),
    EntityRow("billing-service", "Billing Service", "Service", "billing, payments, subscriptions"),
    EntityRow("invoice-worker", "Invoice Worker", "Service", "invoices, invoicing"),
    EntityRow("memory-layer", "Memory Layer", "Service", "memory, memory layer, notebook memory"),
    EntityRow("search-service", "Search Service", "Service", "search"),
    EntityRow("doc-rfc", "RFC: memory layer on a graph store", "Document"),
    EntityRow("TKT-102", "TKT-102", "Ticket", "", "Annual plan renewals charge twice on retry"),
    EntityRow("maya", "Maya Ortiz", "Person"),
    EntityRow("arjun", "Arjun Mehta", "Person"),
    EntityRow("payments", "Payments", "Team"),
    EntityRow("decision-1", "Move the memory layer off Postgres", "Decision", "memory layer off postgres, graph store migration"),
]
SERVICES = ("Service", "Document", "Ticket")


def _resolve(text, labels=SERVICES):
    return resolve_rows(text, ROWS, labels)


class ResolveRowsTest(unittest.TestCase):
    def test_an_alias_inside_a_question_resolves(self):
        self.assertEqual(_resolve("who owns billing").entity, Entity("billing-service", "Billing Service", "Service"))
        self.assertEqual(_resolve("A new ticket just landed about a billing bug. Who should look at it?").entity.id, "billing-service")

    def test_the_longest_phrase_wins_over_a_shorter_alias(self):
        self.assertEqual(_resolve("what breaks if the auth service goes down").entity.name, "Auth Service")
        found = _resolve("why did we move the memory layer off postgres", ("Decision", "Service"))
        self.assertEqual(found.entity.label, "Decision")
        self.assertEqual(found.candidates, [])

    def test_two_nodes_named_side_by_side_resolve_to_nothing(self):
        found = _resolve("who knows the payments gateway")
        self.assertIsNone(found.entity)
        self.assertEqual(found.reason, "ambiguous")
        self.assertEqual([(c.entity.name, c.via) for c in found.candidates], [("Billing Service", "payments"), ("API Gateway", "gateway")])
        self.assertEqual(
            describe_miss(found),
            "Nothing in the graph matches “payments gateway” as one thing. Nearest: Billing Service (known as “payments”), API Gateway (known as “gateway”).",
        )

    def test_stems_match_so_singulars_do_not_slip_through(self):
        self.assertTrue(same_word("payments", "payment"))
        self.assertTrue(same_word("matches", "match"))
        self.assertFalse(same_word("api", "apis"))
        self.assertEqual(_resolve("payment gateway").reason, "ambiguous")

    def test_a_longer_word_that_starts_with_an_alias_is_not_that_alias(self):
        self.assertFalse(same_word("author", "auth"))
        self.assertFalse(same_word("authored", "auth"))
        self.assertFalse(same_word("auth", "authentication"))
        self.assertIsNone(_resolve("what did the author decide last week").entity)

    def test_a_match_at_the_end_of_an_unknown_noun_phrase_is_a_candidate_not_an_answer(self):
        nothing = lambda _word: False  # noqa: E731  the graph knows no other word
        found = resolve_rows("what breaks if the claude api goes down, one hop", ROWS, SERVICES, known=nothing)
        self.assertIsNone(found.entity)
        self.assertEqual(found.reason, "partial")
        self.assertEqual([(c.entity.name, c.via) for c in found.candidates], [("API Gateway", "api")])
        self.assertEqual(describe_miss(found, "service"), "Nothing in the graph matches “claude api”. Nearest: API Gateway (known as “api”).")
        # Two words before the match are both the name.
        self.assertEqual(describe_miss(resolve_rows("who owns our old ledger billing", ROWS, SERVICES, known=nothing)), "Nothing in the graph matches “old ledger billing”. Nearest: Billing Service (known as “billing”).")

    def test_a_verb_before_the_match_or_a_word_the_graph_knows_is_not_a_missing_name(self):
        nothing = lambda _word: False  # noqa: E731
        # Only a determiner opens a noun phrase: "maintains" and "cut" are verbs.
        self.assertEqual(resolve_rows("who maintains billing", ROWS, SERVICES, known=nothing).entity.id, "billing-service")
        self.assertEqual(resolve_rows("why did we cut the memory layer", ROWS, SERVICES, known=nothing).entity.id, "memory-layer")
        self.assertEqual(resolve_rows("what breaks if the api goes down", ROWS, SERVICES, known=nothing).entity.id, "api-gateway")
        # A modifier that names something in the graph keeps the match.
        self.assertEqual(resolve_rows("what breaks if the public api goes down", ROWS, SERVICES, known=lambda word: word == "public").entity.id, "api-gateway")
        # Without a vocabulary (classification by best_match), the match stands as before.
        self.assertEqual(resolve_rows("what breaks if the claude api goes down", ROWS, SERVICES).entity.id, "api-gateway")

    def test_names_apart_in_the_sentence_pick_the_strongest_and_keep_the_rest(self):
        found = _resolve("the invoice worker and billing")
        self.assertEqual(found.entity.name, "Invoice Worker")
        self.assertEqual(found.candidate_names(), ["Billing Service"])
        self.assertEqual([e.name for e in entities_in_rows("how is auth connected to billing", ROWS, SERVICES)], ["Auth Service", "Billing Service"])

    def test_label_order_breaks_ties_between_identical_phrases(self):
        self.assertEqual(_resolve("who owns payments", ("Service", "Team")).entity.label, "Service")
        self.assertEqual(_resolve("who owns payments", ("Team", "Service")).entity.label, "Team")

    def test_people_resolve_by_a_unique_first_name_and_tickets_by_id(self):
        found = _resolve("how is maya connected to invoices", ("Person", "Service"))
        self.assertEqual(found.entity.name, "Invoice Worker")
        self.assertEqual(found.candidate_names(), ["Maya Ortiz"])
        self.assertEqual(_resolve("who is on TKT-102").entity.id, "TKT-102")

    def test_nothing_close_means_no_candidates(self):
        found = _resolve("Who owns the espresso machine?")
        self.assertEqual((found.entity, found.reason, found.candidates), (None, "no_match", []))
        self.assertEqual(describe_miss(found), "Nothing in the graph matches “espresso machine”. Name a service, document, or ticket that exists in the graph.")
        self.assertEqual(core("Who owns the espresso machine?"), "espresso machine")

    def test_a_title_word_alone_is_not_a_near_miss(self):
        # "out" appears in a ticket title; a candidate needs a shared word of four letters or more.
        found = _resolve("and two hops out")
        self.assertEqual((found.entity, found.candidates), (None, []))
        titled = _resolve("renewals charge")
        self.assertEqual(titled.candidate_names(), ["TKT-102"])
        self.assertEqual(describe_miss(titled), "Nothing in the graph matches “renewals charge”. Nearest: TKT-102 (titled “Annual plan renewals charge twice on re…”).")

    def test_an_empty_core_is_said_plainly(self):
        found = _resolve("who should look at it")
        self.assertIsNone(found.entity)
        self.assertEqual(describe_miss(found), "I can't tell what that refers to. Name a service, document, or ticket.")
        self.assertEqual(join_names(["A"]), "A")
        self.assertEqual(join_names(["A", "B"]), "A and B")
        self.assertEqual(join_names(["A", "B", "C"]), "A, B and C")

    def test_a_typo_offers_the_nearest_name_without_resolving(self):
        found = _resolve("invoce worker")
        self.assertIsNone(found.entity)
        self.assertEqual(found.candidate_names(), ["Invoice Worker"])
        self.assertEqual(describe_miss(found, "service"), "Nothing in the graph matches “invoce worker”. Nearest: Invoice Worker.")
