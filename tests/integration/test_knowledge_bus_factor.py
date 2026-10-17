"""``if_person_leaves`` on a real FalkorDB: Quillfern engineering, then with its Sales team imported.

The engineering setup adds what Quillfern lacks: a standing decision Sara alone proposed and
approved, an open and a closed ticket only she worked on, a superseded decision of hers, a
person with no edges, and a private service Kofi owns. The Sales team is the real one, from
``examples/teams/quillfern-sales.toml`` through the team importer.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import ask_service, bus_factor, catalogue, resolve  # noqa: E402
from src.domain.knowledge.resolve import Entity  # noqa: E402
from src.seed import quillfern, quillfern_sales, seed  # noqa: E402

GRAPH = f"company_bus_factor_test{tests.TEST_GRAPH_SUFFIX}"
SALES_GRAPH = f"company_bus_factor_sales_test{tests.TEST_GRAPH_SUFFIX}"
ORG = ["org_shared"]
PRIVATE = "slack_channel_C9"


def _cites(result) -> list[str]:
    return [step.cite() for step in result.path]


def _leaves(question: str, scopes=None):
    tool, args = ask_service.route(question)
    assert tool == "if_person_leaves", (question, tool)
    return catalogue.get_tool(tool).run(args, scopes or ORG)


class _Graph(unittest.TestCase):
    graph_name = GRAPH

    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=cls.graph_name),
            mock.patch.object(seed, "graph_name", return_value=cls.graph_name),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        cls.seed_more()

    @classmethod
    def seed_more(cls):
        pass

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(cls.graph_name).delete()
        finally:
            for patch in cls._patches:
                patch.stop()


class IfPersonLeavesTest(_Graph):
    @classmethod
    def seed_more(cls):
        _, graph = falkordb_client.connect()
        graph.query(
            """
            MATCH (sara:Person {id: 'sara'}), (kofi:Person {id: 'kofi'}), (postgres:Service {id: 'postgres'})
            CREATE (backups:Decision {id: 'decision-pg-backups', name: 'Back up Postgres nightly to cold storage', decided_at: 1789000000000}),
                   (sara)-[:PROPOSED]->(backups), (sara)-[:APPROVED]->(backups), (backups)-[:ABOUT]->(postgres),
                   (weekly:Decision {id: 'decision-pg-weekly', name: 'Back up Postgres weekly', decided_at: 1780000000000}),
                   (sara)-[:PROPOSED]->(weekly), (weekly)-[:ABOUT]->(postgres), (backups)-[:SUPERSEDES]->(weekly),
                   (vacuum:Ticket {id: 'TKT-191', name: 'TKT-191', title: 'Vacuum stalls on the notes table'}),
                   (sara)-[:WORKED_ON]->(vacuum), (vacuum)-[:ABOUT]->(postgres),
                   (certs:Ticket {id: 'TKT-190', name: 'TKT-190', title: 'Rotate replica certificates', state: 'closed'}),
                   (sara)-[:WORKED_ON]->(certs), (certs)-[:ABOUT]->(postgres),
                   (:Person {id: 'ines', name: 'Ines Duarte', role: 'New hire'}),
                   (kofi)-[:OWNS]->(:Service {id: 'notebook-export', name: 'Notebook Export', scope: $private})
            """,
            {"private": PRIVATE},
        )

    def test_a_sole_owner_names_who_is_next_in_line_with_the_edges_that_put_them_there(self):
        result = _leaves("what breaks if Arjun leaves")
        self.assertTrue(result.found)
        self.assertEqual(
            result.answer,
            "If Arjun Mehta leaves, 1 thing rests on them alone. "
            "Billing Service: Arjun Mehta is its only owner; next in line: Lena Fischer, then Tomas Reyes; 2 others depend on it.",
        )
        self.assertEqual(
            _cites(result),
            [
                "Arjun Mehta -[OWNS]-> Billing Service",
                "Lena Fischer -[WORKED_ON]-> Billing Service",
                "Tomas Reyes -[WORKED_ON]-> Billing Service",
                "Lena Fischer -[WORKED_ON]-> TKT-102",
                "TKT-102 -[ABOUT]-> Billing Service",
            ],
        )
        own = result.path[0]
        self.assertEqual((own.from_id, own.to_id, own.from_label, own.to_label), ("arjun", "billing-service", "Person", "Service"))

    def test_next_in_line_is_who_knows_without_the_person_leaving(self):
        billing = Entity("billing-service", "Billing Service", "Service", "service")
        _, graph = falkordb_client.connect()
        ranked = [name for name, _score, _reasons in ask_service.rank_people(ask_service.knowers(graph, billing, ORG).path, billing)]
        self.assertEqual(ranked[0], "Arjun Mehta")
        self.assertIn(f"next in line: {ranked[1]}, then {ranked[2]};", _leaves("what breaks if Arjun leaves").answer)
        # who_knows still answers the same way after the split into resolve and knowers.
        self.assertTrue(ask_service.who_knows("who knows billing", ORG).answer.startswith("Billing Service is owned by Arjun Mehta. Ask first: Arjun Mehta"))

    def test_every_phrasing_routes_to_the_same_answer(self):
        expected = _leaves("what breaks if Arjun leaves").answer
        for question in (
            "what is Arjun the only one who knows",
            "what happens if arjun leaves",
            "what if Arjun quits",
            "who covers for arjun",
            "who takes over from Arjun",
            "what if arjun is on leave",
            "what's the bus factor on arjun",
        ):
            with self.subTest(question=question):
                self.assertEqual(_leaves(question).answer, expected)

    def test_only_owner_against_only_one_who_knows(self):
        result = _leaves("what happens if maya leaves")
        self.assertEqual(
            result.answer,
            "If Maya Ortiz leaves, 3 things rest on them alone, ranked by how much depends on each. "
            "Auth Service: Maya Ortiz is its only owner; next in line: Arjun Mehta, then Sara Lindqvist; 5 others depend on it. "
            "Open ticket TKT-103 (“Session refresh fails after token rotation”), about Auth Service: only Maya Ortiz worked on it; "
            "5 others depend on Auth Service. "
            "API Gateway: Maya Ortiz is its only owner and the only one who knows it.",
        )
        self.assertIn("Maya Ortiz -[WORKED_ON]-> TKT-103", _cites(result))
        self.assertIn("TKT-103 -[ABOUT]-> Auth Service", _cites(result))

    def test_decisions_and_tickets_rank_by_what_they_are_about_and_the_reply_is_capped(self):
        result = _leaves("who covers for sara")
        self.assertEqual(
            result.answer,
            "If Sara Lindqvist leaves, 4 things rest on them alone, ranked by how much depends on each. "
            "Postgres: Sara Lindqvist is its only owner and the only one who knows it; 6 others depend on it. "
            "The standing decision *Back up Postgres nightly to cold storage*, about Postgres: only Sara Lindqvist proposed and approved it; "
            "6 others depend on Postgres. "
            "Open ticket TKT-191 (“Vacuum stalls on the notes table”), about Postgres: only Sara Lindqvist worked on it; 6 others depend on Postgres. "
            "And 1 more: Graph Store.",
        )
        cites = _cites(result)
        for cite in (
            "Sara Lindqvist -[PROPOSED]-> Back up Postgres nightly to cold storage",
            "Sara Lindqvist -[APPROVED]-> Back up Postgres nightly to cold storage",
            "Back up Postgres nightly to cold storage -[ABOUT]-> Postgres",
            "Sara Lindqvist -[WORKED_ON]-> TKT-191",
            # The item past the cap keeps the person's own edge as its proof.
            "Sara Lindqvist -[OWNS]-> Graph Store",
        ):
            self.assertIn(cite, cites)
        # A superseded decision and a closed ticket are not at risk.
        self.assertNotIn("weekly", result.answer)
        self.assertNotIn("TKT-190", result.answer + " ".join(cites))

    def test_nothing_alone_and_nothing_linked(self):
        self.assertEqual(
            _leaves("what if tomas quits").answer,
            "If Tomas Reyes leaves, nothing rests on them alone: everything they own, work on, or decided has someone else on it.",
        )
        self.assertEqual(_leaves("what if ines leaves").answer, "Ines Duarte is in the graph, and nothing is linked to them yet.")

    def test_a_name_not_in_the_graph_is_refused_the_way_the_resolver_words_it(self):
        result = _leaves("what breaks if Zed leaves")
        self.assertFalse(result.found)
        self.assertEqual(result.path, [])
        self.assertEqual(result.answer, "Nothing in the graph matches “zed”. Name a person that exists in the graph.")
        self.assertEqual(result.answer, resolve.describe_miss(resolve.resolve_name("what breaks if Zed leaves", ("Person",), ORG), "person"))
        # A service is not a person.
        self.assertFalse(bus_factor.if_person_leaves("what breaks if billing leaves", ORG).found)

    def test_a_private_thing_counts_only_for_a_reader_who_can_see_it(self):
        public = _leaves("what if kofi leaves")
        self.assertNotIn("Notebook Export", public.answer + " ".join(_cites(public)))
        private = _leaves("what if kofi leaves", ORG + [PRIVATE])
        self.assertIn("4 things rest on them alone", private.answer)
        self.assertIn("Kofi Mensah -[OWNS]-> Notebook Export", _cites(private))


class IfPersonLeavesSalesTest(_Graph):
    graph_name = SALES_GRAPH

    @classmethod
    def seed_more(cls):
        quillfern_sales.seed_sales()

    def test_a_process_reads_as_run_and_the_next_in_line_crosses_teams(self):
        result = _leaves("what if priya leaves")
        self.assertEqual(
            result.answer,
            "If Priya Shah leaves, 2 things rest on them alone, ranked by how much depends on each. "
            "Renewals process: Priya Shah is the only one running it; next in line: Dev Patel, then Noor Haddad; 1 other depends on it. "
            "Pricing playbook: Priya Shah is its only owner; next in line: Noor Haddad, then Tomas Reyes.",
        )
        self.assertEqual(
            _cites(result),
            [
                "Priya Shah -[OWNS]-> Renewals",
                "Dev Patel -[WORKED_ON]-> Renewals",
                "Noor Haddad -[WORKED_ON]-> Renewals",
                "Priya Shah -[OWNS]-> Pricing playbook",
                "Noor Haddad -[WORKED_ON]-> Pricing playbook",
                # Tomas is on Platform: the engineering manager is next in line for a Sales playbook.
                "Tomas Reyes -[WORKED_ON]-> Pricing playbook",
            ],
        )
        # Priya approved both pricing decisions, but someone else proposed each: neither rests on her alone.
        self.assertNotIn("decision", result.answer)

    def test_a_system_and_an_account_losing_their_only_owner(self):
        self.assertEqual(
            _leaves("what if noor leaves").answer,
            "If Noor Haddad leaves, 1 thing rests on them alone. "
            "HubSpot system: Noor Haddad is its only owner and the only one who knows it; 2 others depend on it.",
        )
        dev = _leaves("what breaks if dev leaves")
        self.assertEqual(
            dev.answer,
            "If Dev Patel leaves, 1 thing rests on them alone. "
            "Enterprise accounts: Dev Patel is its only owner and the only one who knows it.",
        )
        self.assertEqual(_cites(dev), ["Dev Patel -[OWNS]-> Enterprise accounts"])

    def test_engineering_answers_are_unchanged_by_the_sales_team(self):
        self.assertEqual(
            _leaves("what breaks if Arjun leaves").answer,
            "If Arjun Mehta leaves, 1 thing rests on them alone. "
            "Billing Service: Arjun Mehta is its only owner; next in line: Lena Fischer, then Tomas Reyes; 2 others depend on it.",
        )
        self.assertTrue(_leaves("what if tomas quits").answer.startswith("If Tomas Reyes leaves, nothing rests on them alone"))


if __name__ == "__main__":
    unittest.main()
