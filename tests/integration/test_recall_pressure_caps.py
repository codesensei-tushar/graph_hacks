"""A saved memory found by a natural question, decision pressure ranked and dated, clamps that say so,
bare ticket keys, projects as subjects, and the console's linking hint. Quillfern on a real FalkorDB."""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.connectors import graph as connector_graph
from src.connectors.base import Container, Install, Item, Page
from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import algorithms, ask_api, ask_service, catalogue, people, search, what_changed  # noqa: E402
from src.domain.knowledge.resolve import Entity  # noqa: E402
from src.domain.memory import memory_service  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_recall_pressure_test{tests.TEST_GRAPH_SUFFIX}"
ORG = ["org_shared"]
SLACK = ["org_shared", "org_internal"]
JIRA = Install(source="jira", config={})
BEFORE = 1_789_000_000_000  # before the billing retries decision (20 Sep 2026)
AFTER = 1_790_000_000_000


def _ask(question: str, scopes=None):
    tool, args = ask_service.route(question)
    return catalogue.get_tool(tool).run(args, scopes or ORG)


class RecallPressureCapsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        search.ensure_search_indexes()
        ask_api.ANSWERS.clear()
        memory_service.write("the refund batch runs at 02:00 UTC every night", "org_shared", "UBO", "CPUB", "1700000100.000100")
        _, graph = falkordb_client.connect()
        graph.query(
            """
            MATCH (billing:Service {id: 'billing-service'})
            CREATE (:Ticket {id: 'TKT-301', name: 'TKT-301', title: 'Old export bug', created_at: $before, scope: 'org_shared'})-[:ABOUT]->(billing),
                   (:Ticket {id: 'TKT-304', name: 'TKT-304', title: 'Renewal webhooks retried twice', created_at: $after, scope: 'org_shared'})-[:ABOUT]->(billing),
                   (:Ticket {id: 'TKT-303', name: 'TKT-303', title: 'Invoice footer typo', created_at: $after, scope: 'org_shared'})-[:ABOUT]->(billing)
            """,
            {"before": BEFORE, "after": AFTER},
        )
        project = Container(id=JIRA.container_id("QPAY"), name="Quillfern Payments", external_id="QPAY", kind="project")
        connector_graph.write_containers(JIRA, [project])
        item = Item(label="Ticket", id=JIRA.item_id("1"), external_id="1", container=project.id, name="QPAY-1", title="Card retries", state="open")
        connector_graph.write_page(JIRA, Page(items=[item], users=[], edges=[]))

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in reversed(cls._patches):
                patch.stop()

    # --- R2-2: a saved memory, asked about naturally --------------------------------------------

    def test_a_saved_memory_answers_a_natural_question(self):
        result = _ask("when does the refund batch run")
        self.assertTrue(result.found, result.answer)
        self.assertTrue(result.answer.startswith("From what was said in chat: “the refund batch runs at 02:00 UTC every night” (someone, "), result.answer)
        said = _ask("what have we said about the refund batch")
        self.assertTrue(said.found, said.answer)
        self.assertIn("02:00 UTC", said.answer)

    def test_a_loose_match_never_answers_another_question(self):
        self.assertFalse(_ask("who owns the refund rocket").found)
        self.assertFalse(_ask("who owns the payments gateway").found)

    # --- R2-6: decision pressure -----------------------------------------------------------------

    def test_pressure_counts_only_later_work_most_on_topic_first(self):
        result = ask_service.decision_pressure("is the billing retries decision still the last word", ORG)
        self.assertNotIn("TKT-301", result.answer)
        # On topic first (it shares "renewal", "webhooks" with the decision), though TKT-303 sorts before it.
        self.assertIn("It does not cite TKT-304 (about Billing Service) and TKT-303 (about Billing Service).", result.answer)

    # --- R2-8: clamps say so -----------------------------------------------------------------------

    def test_clamps_are_said(self):
        self.assertTrue(algorithms.blast_radius("what breaks if auth goes down", ORG, 999).answer.endswith("(Capped at 6 hops.)"))
        self.assertTrue(_ask("what breaks if auth goes down, 40 hops out").answer.endswith("(Capped at 6 hops.)"))
        self.assertNotIn("Capped", _ask("what breaks if auth goes down, two hops out").answer)
        self.assertTrue(what_changed.what_changed("what changed", ORG, days=999).answer.endswith("(Capped at a year.)"))
        self.assertTrue(_ask("what changed in the last 999 days").answer.endswith("(Capped at a year.)"))

    # --- bare keys, projects as subjects, the console hint ----------------------------------------

    def test_a_bare_key_and_projects_as_subjects(self):
        self.assertEqual(ask_service.route("QPAY-9"), ("ticket_status", {"name": "QPAY-9"}))
        self.assertTrue(_ask("QPAY-9", SLACK).answer.startswith("I can't find QPAY-9 in the graph; Jira QPAY is synced (QPAY-1 to QPAY-1)."))
        self.assertTrue(_ask("what's open in QPAY", SLACK).answer.startswith("1 open ticket in Jira QPAY: QPAY-1"))
        named = _ask("what is Quillfern Payments", SLACK)
        self.assertTrue(named.answer.startswith("Jira QPAY has 1 direct connection"), named.answer)
        self.assertTrue(_ask("who owns QPAY", SLACK).answer.startswith("Jira QPAY has"), _ask("who owns QPAY", SLACK).answer)
        # The company-wide reader cannot see the project.
        self.assertNotIn("QPAY-1", _ask("what is Quillfern Payments").answer)

    def test_a_console_org_links_people_in_the_console(self):
        user = Entity("jira:x", "Om Sherikar (Jira)", people.ACCOUNT, "Jira")
        self.assertIn("CONNECTOR_JIRA_PEOPLE", people.unlinked_hint(user))
        with mock.patch("src.core.tenancy.is_legacy", return_value=False):
            self.assertEqual(people.unlinked_hint(user), "Om Sherikar (Jira) is not linked to a person yet; an admin can link them in Console → People.")


if __name__ == "__main__":
    unittest.main()
