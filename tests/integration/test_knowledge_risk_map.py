"""``risk_map`` on a real FalkorDB: plain Quillfern, then with its Sales team imported, plus
small graphs for the scope rule and the edge cases.

PageRank figures move with everything else in the graph (FalkorDB's scores depend on the
node-id space), so they are read from ``pagerank_by_id`` and checked against ``load_bearing``,
never pinned as literals.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import algorithms, ask_api, ask_service, catalogue, mcp_tools, risk_map  # noqa: E402
from src.seed import quillfern, quillfern_sales, seed  # noqa: E402

GRAPH = f"company_risk_map_test{tests.TEST_GRAPH_SUFFIX}"
SALES_GRAPH = f"company_risk_map_sales_test{tests.TEST_GRAPH_SUFFIX}"
SMALL_GRAPH = f"company_risk_map_small_test{tests.TEST_GRAPH_SUFFIX}"
ORG = ["org_shared"]
PRIVATE = "slack_channel_C9"
ROW_KEYS = {"id", "name", "kind", "team", "owners", "owner_teams", "dependents", "pagerank", "people", "flags", "score"}


def _map(scopes=None):
    tool, args = ask_service.route("what are our riskiest services")
    assert tool == "risk_map", tool
    return catalogue.get_tool(tool).run(args, scopes or ORG)


def _rows(result) -> dict[str, dict]:
    return {row["name"]: row for row in result.grid["rows"]}


def _pagerank(service_id: str) -> str:
    _, graph = falkordb_client.connect()
    return f"{algorithms.pagerank_by_id(graph)[service_id]:.2f}"


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
        cls.seed_graph()
        ask_api.ANSWERS.clear()

    @classmethod
    def seed_graph(cls):
        seed.reset_and_seed(quillfern)

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(cls.graph_name).delete()
        finally:
            ask_api.ANSWERS.clear()
            for patch in cls._patches:
                patch.stop()


class RiskMapTest(_Graph):
    def test_the_answer_tells_three_in_full_and_names_the_rest(self):
        result = _map()
        self.assertEqual(result.tool, "risk_map")
        self.assertTrue(result.found)
        self.assertEqual(
            result.answer,
            "Risk sits with 6 of 9 things, ranked by how much depends on each and how many gaps it has. "
            f"Postgres (PageRank {_pagerank('postgres')}): 6 things depend on it, only Sara Lindqvist knows it, and no document describes it. "
            f"Graph Store (PageRank {_pagerank('graph-store')}): 2 things depend on it, only Sara Lindqvist knows it, no document describes it, "
            "and its owner Sara Lindqvist is on Platform while Data runs it. "
            f"Notification Service (PageRank {_pagerank('notification-service')}): 1 thing depends on it, only Lena Fischer knows it, "
            "no document describes it, and its owner Lena Fischer is on Payments while Platform runs it. "
            "And 3 more: API Gateway, Search Service and Invoice Worker.",
        )

    def test_rows_are_ranked_by_score_then_dependents_then_name(self):
        grid = _map().grid
        self.assertEqual(grid["kind"], "risk_map")
        self.assertEqual([column["key"] for column in grid["columns"]], ["dependents", "pagerank", "people", "one_person", "no_document", "owner_elsewhere"])
        self.assertEqual(grid["scoring"], "(1 + dependents) x flags raised; ties by dependents, then name")
        self.assertEqual(
            [(row["name"], row["score"]) for row in grid["rows"]],
            [
                ("Postgres", 14), ("Graph Store", 9), ("Notification Service", 6), ("API Gateway", 2), ("Search Service", 2),
                ("Invoice Worker", 1), ("Auth Service", 0), ("Billing Service", 0), ("Memory Layer", 0),
            ],
        )
        for row in grid["rows"]:
            self.assertEqual(set(row), ROW_KEYS, row["name"])

    def test_row_values(self):
        rows = _rows(_map())
        postgres = rows["Postgres"]
        self.assertEqual(
            {key: postgres[key] for key in ("id", "kind", "team", "owners", "owner_teams", "dependents", "people", "flags", "score")},
            {
                "id": "postgres", "kind": "service", "team": "Platform", "owners": ["Sara Lindqvist"], "owner_teams": ["Platform"],
                "dependents": 6, "people": ["Sara Lindqvist"], "flags": {"one_person": True, "no_document": True, "owner_elsewhere": False}, "score": 14,
            },
        )
        graph_store = rows["Graph Store"]
        self.assertEqual((graph_store["team"], graph_store["owner_teams"], graph_store["dependents"]), ("Data", ["Platform"], 2))
        self.assertEqual(graph_store["flags"], {"one_person": True, "no_document": True, "owner_elsewhere": True})
        notification = rows["Notification Service"]
        self.assertEqual((notification["team"], notification["owner_teams"], notification["dependents"], notification["score"]), ("Platform", ["Payments"], 1, 6))
        auth = rows["Auth Service"]
        self.assertEqual((auth["score"], auth["dependents"], len(auth["people"])), (0, 5, 3))
        self.assertEqual(auth["flags"], {"one_person": False, "no_document": False, "owner_elsewhere": False})

    def test_people_are_who_knows_ranking_and_dependents_agree_with_the_other_tools(self):
        rows = _rows(_map())
        for name, row in rows.items():
            with self.subTest(name=name):
                known = ask_service.who_knows(name, ORG)
                ranked = [person for person, _score, _reasons in ask_service.rank_people(known.path, ask_service.resolve.resolve_name(name, ("Service",), ORG).entity)]
                self.assertEqual(row["people"], ranked)
        # if_person_leaves counts what depends on Sara's Postgres the same way.
        self.assertIn("6 others depend on it", catalogue.get_tool("if_person_leaves").run({"name": "Sara Lindqvist"}, ORG).answer)
        # blast_radius names the same dependents.
        for name, count in (("Postgres", 6), ("Auth Service", 5)):
            with self.subTest(blast=name):
                self.assertEqual(rows[name]["dependents"], count)
                blast = algorithms.blast_radius(f"what breaks if {name} goes down", ORG)
                self.assertEqual(len({step.from_name for step in blast.path if step.rel == "DEPENDS_ON"}), count)

    def test_pagerank_is_load_bearing_s_number(self):
        rows = _rows(_map())
        self.assertIn(f"Postgres carries the most weight (PageRank {rows['Postgres']['pagerank']:.2f}, 1st of", algorithms.load_bearing().answer)
        self.assertGreater(rows["Postgres"]["pagerank"], rows["Graph Store"]["pagerank"])

    def test_the_path_cites_owners_teams_dependents_and_the_other_team(self):
        result = _map()
        cited = [step.cite() for step in result.path]
        for line in (
            "Sara Lindqvist -[OWNS]-> Postgres",
            "Billing Service -[DEPENDS_ON]-> Postgres",
            "Data -[RUNS]-> Graph Store",
            "Sara Lindqvist -[MEMBER_OF]-> Platform",
            # The rest get their owner's edge only.
            "Maya Ortiz -[OWNS]-> API Gateway",
        ):
            self.assertIn(line, cited)
        self.assertNotIn("Platform -[RUNS]-> API Gateway", cited)
        # Every step carries ids and labels, so the page can draw it.
        self.assertTrue(all(step.from_id and step.to_id and step.from_label and step.to_label for step in result.path))

    def test_the_ask_api_and_mcp_carry_the_grid(self):
        answer = ask_api.answer_question("what are our riskiest services").to_dict()
        self.assertEqual((answer["tool"], answer["grid"]["kind"]), ("risk_map", "risk_map"))
        self.assertTrue(answer["nodes"] and answer["edges"])
        self.assertIsNone(ask_api.answer_question("who owns billing").to_dict()["grid"])
        self.assertIn("risk_map", mcp_tools.tool_names())
        called = mcp_tools.call_tool("risk_map", {})
        self.assertEqual(called["grid"]["rows"][0]["name"], "Postgres")
        self.assertNotIn("grid", mcp_tools.call_tool("who_owns", {"name": "billing"}))
        self.assertIn("what are our riskiest services", ask_api.examples().questions)

    def test_a_private_service_is_seen_only_with_its_scope(self):
        _, graph = falkordb_client.connect()
        graph.query(
            "MATCH (kofi:Person {id: 'kofi'}) CREATE (kofi)-[:OWNS]->(:Service {id: 'notebook-export', name: 'Notebook Export', scope: $private})",
            {"private": PRIVATE},
        )
        try:
            self.assertNotIn("Notebook Export", _rows(_map()))
            self.assertNotIn("Notebook Export", _map().answer)
            rows = _rows(_map(ORG + [PRIVATE]))
            self.assertEqual(rows["Notebook Export"]["owners"], ["Kofi Mensah"])
            self.assertEqual(len(rows), 10)
        finally:
            graph.query("MATCH (s:Service {id: 'notebook-export'}) DETACH DELETE s")


class RiskMapWithSalesTest(_Graph):
    graph_name = SALES_GRAPH

    @classmethod
    def seed_graph(cls):
        seed.reset_and_seed(quillfern)
        quillfern_sales.seed_sales()

    def test_an_account_has_no_document_flag_and_no_pagerank(self):
        rows = _rows(_map())
        accounts = rows["Enterprise accounts"]
        self.assertEqual((accounts["kind"], accounts["dependents"], accounts["pagerank"]), ("account", 0, None))
        self.assertIsNone(accounts["flags"]["no_document"])

    def test_a_system_other_teams_lean_on_is_named_by_its_kind(self):
        result = _map()
        hubspot = _rows(result)["HubSpot"]
        self.assertEqual((hubspot["kind"], hubspot["dependents"]), ("system", 2))
        self.assertEqual({key: hubspot["flags"][key] for key in ("one_person", "no_document")}, {"one_person": True, "no_document": True})
        self.assertIn(f"The HubSpot system (PageRank {hubspot['pagerank']:.2f}): 2 things depend on it, only Noor Haddad knows it", result.answer)
        self.assertTrue(result.answer.startswith("Risk sits with 10 of 13 things"))

    def test_knowers_for_many_things_at_once_match_one_at_a_time(self):
        _, graph = falkordb_client.connect()
        things = [
            ask_service.Entity(service_id, name, "Service", kind)
            for service_id, name, kind in graph.ro_query("MATCH (s:Service) RETURN s.id, s.name, coalesce(s.kind, 'service')").result_set
        ]
        for scopes in (ORG, ["org_shared", "org_internal"]):
            together = ask_service.knowers_many(graph, things, scopes)
            for thing in things:
                alone = ask_service.knowers_many(graph, [thing], scopes)[thing.id]
                with self.subTest(thing=thing.name, scopes=scopes):
                    self.assertEqual(together[thing.id].result.path, alone.result.path)
                    self.assertEqual((together[thing.id].open_tickets, together[thing.id].more_open), (alone.open_tickets, alone.more_open))

    def test_the_map_costs_the_same_few_queries_however_many_things_there_are(self):
        _, graph = falkordb_client.connect()
        calls = []
        original = type(graph).ro_query

        def counted(self, *args, **kwargs):
            calls.append(args[0])
            return original(self, *args, **kwargs)

        with mock.patch.object(type(graph), "ro_query", counted):
            ranked = risk_map.rows(graph, ORG)
        self.assertEqual(len(ranked), 13)
        # things, PageRank, reach, and knowers' seven reads: never one per thing.
        self.assertLessEqual(len(calls), 12)


class RiskMapEdgeCasesTest(_Graph):
    graph_name = SMALL_GRAPH

    @classmethod
    def seed_graph(cls):
        _, graph = falkordb_client.connect()
        if cls.graph_name in {name.decode() if isinstance(name, bytes) else name for name in falkordb_client.client().list_graphs()}:
            graph.delete()
        graph.query(
            """
            CREATE (team:Team {id: 'ops', name: 'Ops'}),
                   (ana:Person {id: 'ana', name: 'Ana Ruiz'}), (ben:Person {id: 'ben', name: 'Ben Ito'}),
                   (ana)-[:MEMBER_OF]->(team), (ben)-[:MEMBER_OF]->(team),
                   (queue:Service {id: 'queue', name: 'Job Queue'}), (team)-[:RUNS]->(queue),
                   (ana)-[:OWNS]->(queue), (ben)-[:KNOWS]->(queue),
                   (:Document {id: 'queue-doc', name: 'Job Queue runbook'})-[:DESCRIBES]->(queue)
            """
        )

    def test_nothing_flagged_is_said_in_one_line(self):
        result = _map()
        self.assertEqual(
            result.answer,
            "No gaps: the one thing in the graph has more than one person who knows it, a document where one applies, and an owner on the team that runs it.",
        )
        self.assertEqual(result.grid["rows"][0]["score"], 0)

    def test_a_thing_no_one_is_linked_to_says_so(self):
        _, graph = falkordb_client.connect()
        graph.query("MATCH (queue:Service {id: 'queue'}) CREATE (:Service {id: 'cron', name: 'Cron Runner'})-[:DEPENDS_ON]->(queue)")
        try:
            result = _map()
            self.assertIn("Cron Runner (PageRank ", result.answer)
            self.assertIn("): no one is linked to it and no document describes it.", result.answer)
            self.assertEqual(_rows(result)["Job Queue"]["dependents"], 1)
        finally:
            graph.query("MATCH (s:Service {id: 'cron'}) DETACH DELETE s")

    def test_the_tool_needs_no_arguments(self):
        self.assertEqual(catalogue.get_tool("risk_map").args_schema, {"type": "object", "properties": {}, "required": [], "additionalProperties": False})
        self.assertEqual(risk_map.MAX_LISTED, 3)


if __name__ == "__main__":
    unittest.main()
