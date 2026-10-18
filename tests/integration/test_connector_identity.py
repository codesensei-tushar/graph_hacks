"""One person in two tools, first names, and the asker named as "I" or "me".

Quillfern with a Jira project and a HubSpot portal written through ``src/connectors/graph.py``.
Yash Patil is a Jira user assigned PAY-21 (about Billing) and a HubSpot user owning two accounts,
with the same email in both, and no Person. Om Sherikar is a Jira user with no email.
"""

import types
import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.connectors import graph
from src.connectors.base import Container, Edge, ExternalUser, Install, Item, Page
from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import agent, algorithms, ask_api, ask_service, catalogue, find, resolve  # noqa: E402
from src.domain.slack import agent_turn  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_connector_identity_test{tests.TEST_GRAPH_SUFFIX}"
PUBLIC = ["org_shared"]
SLACK = ["org_shared", "org_internal"]
JIRA = Install(source="jira", config={})
HUBSPOT = Install(source="hubspot", config={})


def _ask(question: str, scopes=None):
    tool, args = ask_service.route(question)
    return catalogue.get_tool(tool).run(args, scopes or SLACK)


class ConnectorIdentityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        ask_api.ANSWERS.clear()
        project = Container(id=JIRA.container_id("PAY"), name="Payments project", external_id="PAY", kind="project")
        graph.write_containers(JIRA, [project])
        jira_yash = ExternalUser(id=JIRA.user_id("y1"), source="jira", external_id="y1", name="Yash Patil", email="yash@partner.example")
        om = ExternalUser(id=JIRA.user_id("o1"), source="jira", external_id="o1", name="Om Sherikar", email="")
        pay21 = Item(label="Ticket", id=JIRA.item_id("10021"), external_id="10021", container=project.id, name="PAY-21", title="Refund rounding", about=("billing",))
        pay22 = Item(label="Ticket", id=JIRA.item_id("10022"), external_id="10022", container=project.id, name="PAY-22", title="Invoice footer", about=("billing",))
        graph.write_page(JIRA, Page(items=[pay21, pay22], users=[jira_yash, om], edges=[Edge("ASSIGNED", jira_yash.id, pay21.id), Edge("ASSIGNED", om.id, pay22.id)]))

        crm = Container(id=HUBSPOT.container_id("crm"), name="HubSpot CRM", external_id="crm", kind="crm")
        graph.write_containers(HUBSPOT, [crm])
        hubspot_yash = ExternalUser(id=HUBSPOT.user_id("h1"), source="hubspot", external_id="h1", name="Yash Patil", email="Yash@Partner.example")
        accounts = [
            Item(label="Service", kind="account", id=HUBSPOT.item_id(f"company:{n}"), external_id=f"company:{n}", container=crm.id, name=name)
            for n, name in ((1, "Larkspur Freightways"), (2, "Glimmerwick Studios"))
        ]
        graph.write_page(HUBSPOT, Page(items=accounts, users=[hubspot_yash], edges=[Edge("OWNS", hubspot_yash.id, account.id) for account in accounts]))

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in reversed(cls._patches):
                patch.stop()

    # --- one identity across tools ---------------------------------------------------------------

    def test_who_is_shows_both_tools(self):
        result = _ask("who is Yash Patil")
        self.assertEqual(result.tool, "person_profile")
        self.assertTrue(result.answer.startswith("Yash Patil (Jira, HubSpot) is a Jira and HubSpot user."), result.answer)
        self.assertIn("Owns 2 accounts: Glimmerwick Studios and Larkspur Freightways.", result.answer)
        self.assertIn("Assigned 1 open ticket: PAY-21 (“Refund rounding”).", result.answer)
        self.assertIn("CONNECTOR_JIRA_PEOPLE (accountId=email) or CONNECTOR_HUBSPOT_PEOPLE (ownerId=email)", result.answer)

    def test_leaving_find_and_path_read_both_tools(self):
        leaves = _ask("what if Yash Patil leaves")
        self.assertIn("If Yash Patil (Jira, HubSpot) leaves, 3 things rest on them alone", leaves.answer)
        self.assertIn("PAY-21", leaves.answer)
        self.assertIn("1 ticket worked on by or assigned to Yash Patil (Jira, HubSpot): PAY-21", find.find("Ticket", {"person": "Yash Patil"}, SLACK).answer)
        path = algorithms.path_between("how is Yash Patil connected to Billing Service", "", SLACK)
        self.assertTrue(path.answer.startswith("Yash Patil (Jira, HubSpot) reaches Billing Service in 2 hops"), path.answer)

    # --- first names ---------------------------------------------------------------------------

    def test_a_unique_first_name_resolves_a_connector_user(self):
        self.assertTrue(_ask("who is yash").answer.startswith("Yash Patil (Jira, HubSpot) is"))
        self.assertTrue(_ask("what if om leaves").answer.startswith("If Om Sherikar (Jira) leaves"), _ask("what if om leaves").answer)
        # A person the company knows still wins, by first name too.
        self.assertTrue(_ask("who is arjun").answer.startswith("Arjun Mehta is"))

    def test_the_company_wide_reader_resolves_none_of_it(self):
        for question in ("who is yash", "what if om leaves", "who is Yash Patil"):
            with self.subTest(question=question):
                self.assertNotIn("Yash Patil (", _ask(question, PUBLIC).answer)
                self.assertNotIn("Om Sherikar", _ask(question, PUBLIC).answer)

    # --- the asker -----------------------------------------------------------------------------

    def test_off_slack_a_first_person_question_says_where_to_ask(self):
        for question in ("what team am I on", "what if I leave", "my open tickets", "which of my accounts are at risk"):
            with self.subTest(question=question):
                answer = ask_api.answer_question(question)
                self.assertFalse(answer.found)
                self.assertEqual(answer.answer, resolve.SELF_MISS)

    def test_in_slack_i_and_me_are_the_asker(self):
        request = types.SimpleNamespace(asker_slack_user_id="UARJUN", read_tags=SLACK)
        with mock.patch.object(agent_turn, "asker_person", return_value="Arjun Mehta"):
            for question, tool in (("what team am I on", "person_profile"), ("what if I leave", "if_person_leaves"), ("my open tickets", "find")):
                with self.subTest(question=question):
                    choice = agent_turn.for_the_asker(agent.choose_with_keywords(question), question, request)
                    result, _choice = agent.run_choice(choice, question, SLACK)
                    self.assertEqual(result.tool, tool)
                    self.assertTrue(result.found, result.answer)
                    self.assertIn("Arjun Mehta", result.answer)
            # Someone else named in the question stays the subject.
            choice = agent_turn.for_the_asker(agent.choose_with_keywords("what if Lena leaves, should I worry"), "what if Lena leaves, should I worry", request)
            self.assertIn("Lena", choice.args["name"])


if __name__ == "__main__":
    unittest.main()
