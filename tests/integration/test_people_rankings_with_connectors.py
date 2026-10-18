"""People rankings, ownership and who-to-ask once Jira and HubSpot users are in the graph.

Quillfern with a Jira project and a HubSpot portal: Om Sherikar (Jira, unlinked) is assigned
twelve open Billing tickets, three other contractors one each; Yash Patil owns thirteen HubSpot
accounts. None of them is linked to a person.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.connectors import graph
from src.connectors.base import Container, Edge, ExternalUser, Install, Item, Page
from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import ask_api, ask_service, catalogue  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_people_rankings_test{tests.TEST_GRAPH_SUFFIX}"
PUBLIC = ["org_shared"]
SLACK = ["org_shared", "org_internal"]
JIRA = Install(source="jira", config={})
HUBSPOT = Install(source="hubspot", config={})


def _ask(question: str, scopes=None):
    tool, args = ask_service.route(question)
    return catalogue.get_tool(tool).run(args, scopes or SLACK)


class PeopleRankingsWithConnectorsTest(unittest.TestCase):
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
        om = ExternalUser(id=JIRA.user_id("om"), source="jira", external_id="om", name="Om Sherikar", email="")
        others = [ExternalUser(id=JIRA.user_id(f"c{n}"), source="jira", external_id=f"c{n}", name=f"Contractor {n}", email="") for n in range(1, 4)]
        tickets = [
            Item(label="Ticket", id=JIRA.item_id(f"1{n:03d}"), external_id=f"1{n:03d}", container=project.id, name=f"PAY-{n}", title=f"Billing chore {n}", about=("billing",))
            for n in range(1, 16)
        ]
        edges = [Edge("ASSIGNED", om.id, ticket.id) for ticket in tickets[:12]]
        edges += [Edge("ASSIGNED", other.id, ticket.id) for other, ticket in zip(others, tickets[12:])]
        graph.write_page(JIRA, Page(items=tickets, users=[om, *others], edges=edges))

        crm = Container(id=HUBSPOT.container_id("crm"), name="HubSpot CRM", external_id="crm", kind="crm")
        graph.write_containers(HUBSPOT, [crm])
        yash = ExternalUser(id=HUBSPOT.user_id("y1"), source="hubspot", external_id="y1", name="Yash Patil", email="")
        accounts = [
            Item(label="Service", kind="account", id=HUBSPOT.item_id(f"company:{n}"), external_id=f"company:{n}", container=crm.id, name=f"Account {n:02d}")
            for n in range(1, 14)
        ]
        loose = Item(label="Service", kind="account", id=HUBSPOT.item_id("company:99"), external_id="company:99", container=crm.id, name="Nobody's Account")
        graph.write_page(HUBSPOT, Page(items=[*accounts, loose], users=[yash], edges=[Edge("OWNS", yash.id, account.id) for account in accounts]))

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in reversed(cls._patches):
                patch.stop()

    # --- round 10 ----------------------------------------------------------------------------------

    def test_an_unlinked_owner_is_an_owner(self):
        services = _ask("which services have no owner")
        self.assertNotIn("Account 01", services.answer)
        self.assertIn("Nobody's Account", services.answer)
        self.assertIn("owned by someone not linked to a person (13 by Yash Patil (HubSpot))", services.answer)
        accounts = _ask("which accounts have no owner")
        self.assertTrue(accounts.answer.startswith("1 unowned account: Nobody's Account."), accounts.answer)

    def test_unlinked_users_rank_with_people(self):
        load = _ask("who is overloaded")
        self.assertEqual(load.tool, "people_ranking")
        self.assertTrue(load.answer.startswith("Carries the most right now: Om Sherikar (Jira) (12:"), load.answer)
        owns = _ask("who owns the most")
        self.assertIn("Yash Patil (HubSpot) (13", owns.answer)
        names = [row["name"] for row in load.grid["rows"]]
        self.assertIn("Om Sherikar (Jira)", names)
        # The company-wide reader ranks no connector user.
        self.assertNotIn("Om Sherikar", _ask("who is overloaded", PUBLIC).answer)

    def test_key_people_and_the_bus_factor_rank_people(self):
        for question in ("who are the most important people", "who is the bus factor"):
            with self.subTest(question=question):
                self.assertEqual(_ask(question).tool, "people_ranking")

    # --- R3-3: who to ask first --------------------------------------------------------------------

    def test_people_who_know_a_service_come_before_ticket_assignees(self):
        result = _ask("who knows billing")
        ask_first = result.answer.split("Ask first: ", 1)[1]
        self.assertTrue(ask_first.startswith("Arjun Mehta"), ask_first)
        self.assertLess(ask_first.index("Sara Lindqvist"), ask_first.index("Om Sherikar (Jira)"))
        # Two assignees by name, the rest counted.
        self.assertIn("2 more ticket assignees", ask_first)

    def test_ranking_reads_the_users_once_however_many_there_are(self):
        from src.core import query_metrics

        def queries() -> int:
            query_metrics.reset()
            _ask("who is overloaded")
            return query_metrics.snapshot()["total"]["count"]

        query_metrics.install()
        self.addCleanup(query_metrics.uninstall)
        self.addCleanup(query_metrics.reset)
        before = queries()
        self.assertGreater(before, 0)
        crowd = [ExternalUser(id=JIRA.user_id(f"x{n}"), source="jira", external_id=f"x{n}", name=f"Extra {n}", email=f"extra{n}@corp.example") for n in range(60)]
        graph.write_page(JIRA, Page(items=[], users=crowd, edges=[]))
        try:
            self.assertEqual(queries(), before, "the query count must not grow with the number of connector users")
        finally:
            falkordb_client.connect()[1].query("MATCH (u:ExternalUser) WHERE u.name STARTS WITH 'Extra ' DETACH DELETE u")


if __name__ == "__main__":
    unittest.main()
