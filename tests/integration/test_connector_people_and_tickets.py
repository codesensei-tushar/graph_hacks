"""People only a connected tool knows, single tickets, and BLOCKS read in its true direction.

Quillfern with a Jira project and a HubSpot CRM written through ``src/connectors/graph.py``:
Om Sherikar is an unlinked Jira user assigned PAY-11; Yash Patil is an unlinked HubSpot user who
owns three accounts; PAY-13 blocks PAY-11; PAY-12 is done. A Slack asker (who reads
``org_internal``) gets answers about them; the company-wide reader never resolves either.
"""

import json
import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.connectors import graph
from src.connectors.base import Container, Edge, ExternalUser, Install, Item, Page
from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import algorithms, ask_api, ask_service, bus_factor, catalogue, explore, find, person_profile, search  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_connector_people_test{tests.TEST_GRAPH_SUFFIX}"
PUBLIC = ["org_shared"]
SLACK = ["org_shared", "org_internal"]
JIRA = Install(source="jira", config={})
HUBSPOT = Install(source="hubspot", config={})


def _ask(question: str, scopes=None):
    tool, args = ask_service.route(question)
    return catalogue.get_tool(tool).run(args, scopes or SLACK)


def _cites(result) -> list[str]:
    return [step.cite() for step in result.path]


def _text(result) -> str:
    return json.dumps(result.to_dict(), default=str) + result.answer


class ConnectorPeopleAndTicketsTest(unittest.TestCase):
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

        project = Container(id=JIRA.container_id("PAY"), name="Payments project", external_id="PAY", kind="project")
        graph.write_containers(JIRA, [project])
        om = ExternalUser(id=JIRA.user_id("o1"), source="jira", external_id="o1", name="Om Sherikar", email="")
        pay11 = Item(
            label="Ticket", id=JIRA.item_id("10011"), external_id="10011", container=project.id, name="PAY-11", title="Refund export drops the currency",
            state="open", status="In Progress", about=("billing",), props={"priority": "High", "issue_type": "Bug"},
        )
        pay12 = Item(
            label="Ticket", id=JIRA.item_id("10012"), external_id="10012", container=project.id, name="PAY-12", title="Invoice PDF layout",
            state="closed", status="Done", about=("billing",), props={"priority": "Low", "issue_type": "Task"},
        )
        pay13 = Item(
            label="Ticket", id=JIRA.item_id("10013"), external_id="10013", container=project.id, name="PAY-13", title="Currency table migration",
            state="open", status="To Do", about=("billing",), props={"priority": "Medium", "issue_type": "Task"},
        )
        graph.write_page(
            JIRA,
            Page(
                items=[pay11, pay12, pay13],
                users=[om],
                edges=[Edge("ASSIGNED", om.id, pay11.id), Edge("ASSIGNED", om.id, pay12.id), Edge("BLOCKS", pay13.id, pay11.id)],
            ),
        )

        crm = Container(id=HUBSPOT.container_id("crm"), name="HubSpot CRM", external_id="crm", kind="crm")
        graph.write_containers(HUBSPOT, [crm])
        yash = ExternalUser(id=HUBSPOT.user_id("y1"), source="hubspot", external_id="y1", name="Yash Patil", email="yash@partner.example")
        accounts = [
            Item(label="Service", kind="account", id=HUBSPOT.item_id(f"company:{number}"), external_id=f"company:{number}", container=crm.id, name=name, state="open", status=stage)
            for number, name, stage in ((1, "Larkspur Freightways", "customer"), (2, "Glimmerwick Studios", "customer"), (3, "Bramblecote Bakery", "opportunity"))
        ]
        graph.write_page(HUBSPOT, Page(items=accounts, users=[yash], edges=[Edge("OWNS", yash.id, account.id) for account in accounts]))

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in reversed(cls._patches):
                patch.stop()

    # --- 3: people only a connected tool knows ---------------------------------------------------

    def test_who_is_an_unlinked_hubspot_owner(self):
        result = _ask("who is Yash Patil")
        self.assertTrue(result.found, result.answer)
        self.assertEqual(result.tool, "person_profile")
        self.assertIn("Yash Patil (HubSpot) is an account in HubSpot.", result.answer)
        self.assertIn("Owns 3 accounts: Bramblecote Bakery, Glimmerwick Studios and Larkspur Freightways.", result.answer)
        self.assertIn("an admin can link", result.answer)
        self.assertIn("Yash Patil (HubSpot) -[OWNS]-> Larkspur Freightways", _cites(result))

    def test_what_if_an_unlinked_owner_leaves(self):
        result = _ask("what if Yash Patil leaves")
        self.assertTrue(result.found, result.answer)
        self.assertIn("If Yash Patil (HubSpot) leaves, 3 things rest on them alone", result.answer)
        self.assertIn("an admin can link", result.answer)

    def test_which_accounts_does_an_unlinked_owner_own(self):
        result = _ask("which accounts does Yash Patil own")
        self.assertEqual(result.tool, "find")
        self.assertIn("3 accounts owned by Yash Patil (HubSpot): Bramblecote Bakery, Glimmerwick Studios and Larkspur Freightways.", result.answer)

    def test_tickets_assigned_to_an_unlinked_jira_user(self):
        result = _ask("what tickets are assigned to Om Sherikar")
        self.assertEqual(result.tool, "find")
        self.assertIn("worked on by or assigned to Om Sherikar (Jira)", result.answer)
        self.assertIn("PAY-11", result.answer)
        self.assertIn("Om Sherikar (Jira) -[ASSIGNED]-> PAY-11", _cites(result))
        self.assertIn("2 tickets", find.find("Ticket", {"person": "Om Sherikar"}, SLACK).answer)

    def test_path_between_an_unlinked_user_and_a_service(self):
        result = algorithms.path_between("how is Om Sherikar connected to Billing Service", "", SLACK)
        self.assertTrue(result.path, result.answer)
        self.assertTrue(result.answer.startswith("Om Sherikar (Jira) reaches Billing Service"), result.answer)
        self.assertIn("an admin can link", result.answer)

    def test_the_company_wide_reader_never_resolves_a_connector_account(self):
        for question in ("who is Yash Patil", "what if Yash Patil leaves", "which accounts does Yash Patil own", "what tickets are assigned to Om Sherikar"):
            with self.subTest(question=question):
                result = _ask(question, PUBLIC)
                self.assertFalse(result.found and "Yash" in result.answer, result.answer)
                self.assertNotIn("Yash Patil (HubSpot)", _text(result))
                self.assertNotIn("Om Sherikar", _text(result))
        self.assertNotIn("Om Sherikar", algorithms.path_between("how is Om Sherikar connected to Billing Service", "", PUBLIC).answer)
        self.assertFalse(person_profile.person_profile("Yash Patil", PUBLIC).found)
        self.assertFalse(bus_factor.if_person_leaves("Yash Patil", PUBLIC).found)

    def test_a_person_still_wins_over_an_account(self):
        self.assertTrue(_ask("who is arjun").answer.startswith("Arjun Mehta is"))

    # --- 4: one ticket -------------------------------------------------------------------------

    def test_what_is_a_ticket(self):
        result = _ask("what is PAY-11")
        self.assertEqual(result.tool, "ticket_status")
        self.assertIn("PAY-11 is “Refund export drops the currency”: open (In Progress), priority High, assigned to Om Sherikar (Jira).", result.answer)
        self.assertIn("Blocked by PAY-13 (“Currency table migration”, open).", result.answer)
        self.assertIn("PAY-13 -[BLOCKS]-> PAY-11", _cites(result))

    def test_status_fixed_who_and_blockers(self):
        self.assertIn("open (In Progress)", _ask("status of PAY-11").answer)
        self.assertTrue(_ask("is PAY-12 fixed").answer.startswith("Yes: PAY-12 is closed (Done)."))
        self.assertTrue(_ask("is PAY-11 fixed").answer.startswith("Not yet: PAY-11 is still open (In Progress)."))
        self.assertTrue(_ask("who is working on PAY-11").answer.startswith("Om Sherikar (Jira) is on PAY-11."))
        self.assertTrue(_ask("what blocks PAY-11").answer.startswith("PAY-11 is blocked by PAY-13"))
        blocker = _ask("what blocks PAY-13")
        self.assertTrue(blocker.answer.startswith("Nothing you can read blocks PAY-13."), blocker.answer)
        self.assertIn("It blocks PAY-11", blocker.answer)

    def test_who_knows_a_ticket_leads_with_title_status_and_assignee(self):
        result = ask_service.who_knows("who knows about PAY-11", SLACK)
        self.assertTrue(result.answer.startswith("PAY-11 is “Refund export drops the currency”: open (In Progress), priority High, assigned to Om Sherikar (Jira)."), result.answer)

    def test_a_ticket_is_hidden_from_the_company_wide_reader(self):
        result = _ask("what is PAY-11", PUBLIC)
        self.assertFalse(result.found)
        self.assertNotIn("Refund export", _text(result))

    # --- 5: BLOCKS in its true direction ---------------------------------------------------------

    def test_explore_words_blocks_by_direction(self):
        blocked = explore.explore("explore PAY-11", SLACK)
        self.assertIn("blocked by PAY-13", blocked.answer)
        self.assertNotIn("blocks PAY-13", blocked.answer)
        blocker = explore.explore("explore PAY-13", SLACK)
        self.assertIn("blocks PAY-11", blocker.answer)
        self.assertNotIn("blocked by", blocker.answer)

    def test_neighbors_follows_blocks(self):
        result = explore.neighbors("PAY-11", "BLOCKS", SLACK, "in")
        self.assertTrue(result.found)
        self.assertIn("PAY-13", result.answer)
        self.assertIn("PAY-13 -[BLOCKS]-> PAY-11", _cites(result))

    def test_which_tickets_are_blocked(self):
        result = _ask("which tickets are blocked")
        self.assertEqual(result.tool, "find")
        self.assertIn("1 blocked ticket: PAY-11 (Refund export drops the currency).", result.answer)
        self.assertIn("PAY-13 -[BLOCKS]-> PAY-11", _cites(result))
        self.assertEqual(find.find("Ticket", {"blocked": "yes"}, PUBLIC).answer, "No blocked tickets.")


if __name__ == "__main__":
    unittest.main()
