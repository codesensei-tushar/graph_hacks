"""``connectors`` on a real FalkorDB: Quillfern with its Sales team imported.

The setup adds two teams one person is on, with no work between them, and a private Data
service a Sales person worked on.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import ask_service, bridges, catalogue, resolve  # noqa: E402
from src.seed import quillfern, quillfern_sales, seed  # noqa: E402

GRAPH = f"company_bridges_test{tests.TEST_GRAPH_SUFFIX}"
ORG = ["org_shared"]
PRIVATE = "slack_channel_C5"


def _cites(result) -> list[str]:
    return [step.cite() for step in result.path]


def _connect(question: str, scopes=None):
    tool, args = ask_service.route(question)
    assert tool == "connectors", (question, tool)
    return catalogue.get_tool(tool).run(args, scopes or ORG)


class ConnectorsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        quillfern_sales.seed_sales()
        _, graph = falkordb_client.connect()
        graph.query(
            """
            MATCH (priya:Person {id: 'priya'}), (data:Team {id: 'data'})
            CREATE (ines:Person {id: 'ines', name: 'Ines Duarte'}),
                   (ines)-[:MEMBER_OF]->(:Team {id: 'legal', name: 'Legal'}),
                   (ines)-[:MEMBER_OF]->(:Team {id: 'finance', name: 'Finance'}),
                   (priya)-[:WORKED_ON]->(churn:Service {id: 'churn-model', name: 'Churn model', scope: $private}),
                   (data)-[:RUNS]->(churn)
            """,
            {"private": PRIVATE},
        )

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def test_sales_and_engineering_names_the_one_bridge_and_says_what_it_did_not_find(self):
        result = _connect("who connects Sales and Engineering")
        self.assertTrue(result.found)
        self.assertEqual(
            result.answer,
            "No team in the graph is called “engineering”. "
            "1 person connects Sales to other teams. Tomas Reyes, on Platform, worked on the Pricing playbook (Sales).",
        )
        self.assertEqual(
            _cites(result),
            ["Tomas Reyes -[MEMBER_OF]-> Platform", "Tomas Reyes -[WORKED_ON]-> Pricing playbook", "Sales -[RUNS]-> Pricing playbook"],
        )
        # Asked for the rest of the company, there is nothing to flag.
        self.assertEqual(
            _connect("who connects sales to the rest of the company").answer,
            "1 person connects Sales to other teams. Tomas Reyes, on Platform, worked on the Pricing playbook (Sales).",
        )

    def test_two_teams_by_name_or_by_argument(self):
        expected = "1 person connects Sales and Platform. Tomas Reyes, on Platform, worked on the Pricing playbook (Sales)."
        self.assertEqual(_connect("who connects sales and platform").answer, expected)
        # The model fills the two teams separately.
        self.assertEqual(bridges.connectors("sales", "platform", ORG).answer, expected)
        self.assertEqual(bridges.connectors("sales team", "Sales", ORG).answer, "Sales is one team; name two teams, or one team to see who connects it to the rest.")

    def test_ranked_by_cross_team_work_and_knowing_is_not_a_bridge(self):
        result = _connect("who bridges payments and platform")
        self.assertEqual(
            result.answer,
            "2 people connect Payments and Platform, most cross-team work first. "
            "Lena Fischer, on Payments, owns Notification Service (Platform). "
            "Tomas Reyes, on Platform, worked on Billing Service (Payments).",
        )
        self.assertIn("Platform -[RUNS]-> Notification Service", _cites(result))
        self.assertIn("Payments -[RUNS]-> Billing Service", _cites(result))
        # Arjun knows Auth Service and Sara knows Billing Service: knowing is not shared work.
        self.assertNotIn("Arjun", result.answer)
        self.assertNotIn("Sara", result.answer)

    def test_one_team_to_the_rest_is_capped(self):
        result = _connect("who connects platform")
        self.assertEqual(
            result.answer,
            "4 people connect Platform to other teams, most cross-team work first. "
            "Tomas Reyes, on Platform, worked on Billing Service (Payments) and the Pricing playbook (Sales). "
            "Lena Fischer, on Payments, owns Notification Service (Platform). "
            "Maya Ortiz, on Platform, worked on Memory Layer (Data). "
            "And 1 more: Sara Lindqvist.",
        )
        self.assertNotIn("Sara Lindqvist -[OWNS]-> Graph Store", _cites(result))

    def test_no_bridge_and_a_person_on_both_teams(self):
        self.assertEqual(
            _connect("who connects data and sales").answer,
            "No one connects Data and Sales: no one on one side is on the other or works on what it runs.",
        )
        both = _connect("who connects legal and finance")
        self.assertEqual(both.answer, "1 person connects Legal and Finance. Ines Duarte is on Finance and Legal.")
        self.assertEqual(_cites(both), ["Ines Duarte -[MEMBER_OF]-> Finance", "Ines Duarte -[MEMBER_OF]-> Legal"])

    def test_a_private_thing_bridges_only_for_a_reader_who_can_see_it(self):
        self.assertTrue(_connect("who connects data and sales").answer.startswith("No one connects Data and Sales"))
        private = _connect("who connects data and sales", ORG + [PRIVATE])
        self.assertEqual(private.answer, "1 person connects Data and Sales. Priya Shah, on Sales, worked on Churn model (Data).")
        self.assertIn("Data -[RUNS]-> Churn model", _cites(private))

    def test_no_team_named_is_refused_the_way_the_resolver_words_it(self):
        result = _connect("who connects billing and auth")
        self.assertFalse(result.found)
        self.assertEqual(result.answer, "Nothing in the graph matches “billing auth”. Name a team that exists in the graph.")
        self.assertEqual(result.answer, resolve.describe_miss(resolve.resolve_name("who connects billing and auth", ("Team",), ORG), "team"))


if __name__ == "__main__":
    unittest.main()
