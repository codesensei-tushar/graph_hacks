"""Tickets found by title, unknown keys, blocked tickets, synced projects, and capped projects.

Quillfern with a Jira project PLAT written through ``src/connectors/graph.py``: PLAT-7 "Postgres
upgrade to 16" (about Postgres) is blocked by the open PLAT-8; the closed PLAT-9 is blocked by the
open PLAT-10; PLAT-11 and PLAT-12 are both "Invoice export" tickets. PLAT reached its sync cap.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.connectors import graph
from src.connectors.base import Container, Edge, Install, Item, Page
from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import ask_api, ask_service, catalogue, explore, search  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_ticket_lookup_test{tests.TEST_GRAPH_SUFFIX}"
PUBLIC = ["org_shared"]
SLACK = ["org_shared", "org_internal"]
JIRA = Install(source="jira", config={})


def _ask(question: str, scopes=None):
    tool, args = ask_service.route(question)
    return catalogue.get_tool(tool).run(args, scopes or SLACK)


def _cites(result) -> list[str]:
    return [step.cite() for step in result.path]


class TicketLookupTest(unittest.TestCase):
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
        project = Container(id=JIRA.container_id("PLAT"), name="Platform project", external_id="PLAT", kind="project")
        graph.write_containers(JIRA, [project])

        def ticket(number, title, state="open", about=("postgres",)):
            return Item(label="Ticket", id=JIRA.item_id(f"2{number:04d}"), external_id=f"2{number:04d}", container=project.id, name=f"PLAT-{number}", title=title, state=state, about=about)

        items = [
            ticket(7, "Postgres upgrade to 16"),
            ticket(8, "Free disk on the replicas"),
            ticket(9, "Vacuum settings", state="closed"),
            ticket(10, "Replica lag alert"),
            ticket(11, "Invoice export drops rows", about=("invoice worker",)),
            ticket(12, "Invoice export times out", about=("invoice worker",)),
        ]
        edges = [Edge("BLOCKS", items[1].id, items[0].id), Edge("BLOCKS", items[3].id, items[2].id)]
        graph.write_page(JIRA, Page(items=items, users=[], edges=edges))
        falkordb_client.connect()[1].query("MERGE (s:ConnectorState {id: $id}) SET s.capped = true, s.scope = 'system'", {"id": project.id})

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in reversed(cls._patches):
                patch.stop()

    # --- a ticket by its title -------------------------------------------------------------------

    def test_a_single_title_hit_is_taken_as_the_ticket(self):
        result = _ask("what's blocking the postgres upgrade")
        self.assertEqual(result.tool, "ticket_status")
        self.assertTrue(result.answer.startswith("(Taking “postgres upgrade” as PLAT-7, “Postgres upgrade to 16”.) PLAT-7 is blocked by PLAT-8"), result.answer)
        self.assertIn("PLAT-8 -[BLOCKS]-> PLAT-7", _cites(result))

    def test_several_title_hits_are_listed(self):
        result = _ask("status of the invoice export")
        self.assertEqual(result.tool, "ticket_status")
        self.assertTrue(result.answer.startswith("More than one ticket is titled like that: PLAT-11"), result.answer)
        self.assertIn("PLAT-12", result.answer)

    def test_a_named_service_is_never_taken_for_a_ticket_title(self):
        self.assertEqual(_ask("who is working on the invoice worker").tool, "who_knows")
        self.assertNotEqual(_ask("what's blocking billing").tool, "ticket_status")

    # --- unknown keys ----------------------------------------------------------------------------

    def test_an_unknown_key_in_a_synced_project_says_what_is_synced(self):
        result = _ask("what is PLAT-999")
        self.assertFalse(result.found)
        self.assertTrue(result.answer.startswith("PLAT-999 isn't in the graph; Jira PLAT is synced (PLAT-7 to PLAT-12)."), result.answer)

    def test_an_unknown_key_elsewhere_may_be_in_an_unshared_project(self):
        self.assertEqual(_ask("what is ZED-5").answer, "ZED-5 isn't in the graph. It may be in a project that is not shared with Company Brain.")
        self.assertNotIn("isn't in the graph", _ask("who is working on the billing service for q3-2026").answer)
        # The company-wide reader cannot see PLAT, so learns nothing about it.
        self.assertEqual(_ask("what is PLAT-999", PUBLIC).answer, "PLAT-999 isn't in the graph. It may be in a project that is not shared with Company Brain.")

    # --- blocked, projects, caps -----------------------------------------------------------------

    def test_blocked_lists_only_open_tickets_with_an_open_blocker(self):
        result = _ask("which tickets are blocked")
        self.assertTrue(result.answer.startswith("1 blocked ticket: PLAT-7 (Postgres upgrade to 16)."), result.answer)
        self.assertNotIn("PLAT-9", result.answer)

    def test_a_project_resolves_for_find_and_explore(self):
        result = _ask("open tickets in PLAT")
        self.assertEqual(result.tool, "find")
        self.assertTrue(result.answer.startswith("5 open tickets in Jira PLAT:"), result.answer)
        self.assertIn("PLAT-7 -[IN_CONTAINER]-> Jira PLAT", _cites(result))
        explored = explore.explore("explore PLAT", SLACK)
        self.assertTrue(explored.found, explored.answer)
        self.assertIn("holds", explored.answer)
        self.assertFalse(_ask("open tickets in PLAT", PUBLIC).found)

    def test_a_capped_project_is_said_so(self):
        note = "(Jira PLAT is capped at its newest 1,000 issues.)"
        self.assertIn(note, _ask("open tickets in PLAT").answer)
        self.assertIn(note, _ask("who knows postgres").answer)
        self.assertIn(note, _ask("what is PLAT-7").answer)
        self.assertNotIn("capped", _ask("who knows postgres", PUBLIC).answer)


if __name__ == "__main__":
    unittest.main()
