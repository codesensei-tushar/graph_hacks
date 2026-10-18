"""Team pages on the Quillfern graph: the list, one team's page, kinds, standing decisions, scopes, and the routes."""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from fastapi.testclient import TestClient  # noqa: E402

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import ask_api, risk_map, team_page  # noqa: E402
from src.main import app  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_team_test{tests.TEST_GRAPH_SUFFIX}"


class TeamPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        cls.http = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def setUp(self):
        seed.reset_and_seed(quillfern)
        ask_api.ANSWERS.clear()
        ask_api.LIMITER.reset()
        _, self.graph = falkordb_client.connect()

    def get(self, path: str) -> dict:
        response = self.http.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_the_list_counts_people_and_what_each_team_runs(self):
        teams = self.get("/api/teams")["teams"]
        self.assertEqual(
            [(team["id"], team["name"], team["members"], team["owns"], team["kinds"]) for team in teams],
            [("data", "Data", 1, 3, ["service"]), ("payments", "Payments", 2, 2, ["service"]), ("platform", "Platform", 3, 4, ["service"])],
        )

    def test_a_team_page_lists_people_things_decisions_and_tickets_with_the_edges(self):
        page = self.get("/api/team/data")
        self.assertEqual((page["name"], page["company"], page["scopes"]), ("Data", "Quillfern", ["org_shared"]))
        self.assertEqual([(member["name"], member["owns"]) for member in page["members"]], [("Kofi Mensah", ["Memory Layer", "Search Service"])])
        self.assertEqual([group["kind"] for group in page["owns"]], ["service"])
        self.assertEqual(
            [(item["name"], item["owners"]) for item in page["owns"][0]["items"]],
            [("Graph Store", ["Sara Lindqvist"]), ("Memory Layer", ["Kofi Mensah"]), ("Search Service", ["Kofi Mensah"])],
        )
        [decision] = page["decisions"]
        self.assertEqual(
            (decision["name"], decision["decided_on"], decision["proposed_by"], decision["replaced"], decision["replaced_on"]),
            ("Move the memory layer off Postgres", "15 Sep 2026", ["Kofi Mensah"], "Keep notebook links in Postgres join tables", "1 Jun 2026"),
        )
        self.assertEqual(page["tickets"], [{"id": "TKT-101", "title": "Memory queries time out on large workspaces", "about": "Memory Layer"}])
        # The seed's public memories about what Data runs, newest first, each about the first such service it names.
        self.assertEqual(
            [(memory["text"], memory["author"], memory["about"], memory["learned_at"]) for memory in page["memories"]],
            [
                ("signed off on moving the memory layer to the graph store, Kofi owns the migration", "Tomas Reyes", "Graph Store", "2026-09-15"),
                ("I'll run the graph store for the memory layer, same box as search", "Sara Lindqvist", "Graph Store", "2026-09-11"),
            ],
        )
        self.assertIn({"from": "Kofi Mensah", "rel": "MEMBER_OF", "to": "Data"}, page["path"])
        self.assertIn({"from": "Data", "rel": "RUNS", "to": "Graph Store"}, page["path"])
        self.assertIn({"from": "Sara Lindqvist", "rel": "OWNS", "to": "Graph Store"}, page["path"])
        self.assertEqual(len(page["edges"]), len(page["path"]))

    def test_a_team_page_names_the_teams_it_depends_on_and_the_ones_that_depend_on_it(self):
        page = self.get("/api/team/payments")
        self.assertEqual(
            page["depends_on"],
            [{"id": "platform", "name": "Platform", "pairs": [["Billing Service", "Auth Service"], ["Billing Service", "Postgres"], ["Invoice Worker", "Notification Service"]]}],
        )
        self.assertEqual(page["depended_on_by"], [{"id": "platform", "name": "Platform", "pairs": [["API Gateway", "Billing Service"]]}])
        for step in (
            {"from": "Billing Service", "rel": "DEPENDS_ON", "to": "Auth Service"},
            {"from": "Platform", "rel": "RUNS", "to": "Auth Service"},
            {"from": "API Gateway", "rel": "DEPENDS_ON", "to": "Billing Service"},
            {"from": "Platform", "rel": "RUNS", "to": "API Gateway"},
        ):
            self.assertIn(step, page["path"])
        self.assertEqual(len(page["edges"]), len(page["path"]))
        self.assertEqual(len({node["name"] for node in page["nodes"]}), len(page["nodes"]))

    def test_a_team_page_carries_its_rows_of_the_risk_map_ranked_as_the_tool_ranks_them(self):
        page = self.get("/api/team/payments")
        whole = risk_map.risk_map(["org_shared"]).grid
        ours = {"Billing Service", "Invoice Worker"}
        self.assertEqual(page["risk"]["rows"], [row for row in whole["rows"] if row["name"] in ours])
        self.assertEqual((page["risk"]["columns"], page["risk"]["scoring"]), (whole["columns"], whole["scoring"]))
        self.assertEqual([row["name"] for row in page["risk"]["rows"]], ["Invoice Worker", "Billing Service"])
        self.assertEqual(
            page["glance"],
            "Payments depends on Platform (3 edges). Platform (1 edge) depends on Payments. 1 of the 2 things it runs rests on one person: Invoice Worker.",
        )

    def test_a_team_with_nothing_yet_says_so_in_one_line_instead_of_empty_sections(self):
        self.graph.query("MATCH (c:Company) CREATE (:Team {id: 'ops', name: 'Ops', scope: 'org_shared'})-[:PART_OF]->(c)")
        page = self.get("/api/team/ops")
        self.assertEqual(page["empty"], ["links to other teams", "decisions", "open tickets", "chat"])
        self.assertIsNone(page["risk"])
        self.assertEqual((page["depends_on"], page["depended_on_by"]), ([], []))
        payments = self.get("/api/team/payments")
        self.assertNotIn("links to other teams", payments["empty"])
        self.assertNotIn("decisions", payments["empty"])

    def test_things_are_grouped_by_kind_once_kind_is_set(self):
        self.graph.query("MATCH (s:Service {id: 'invoice-worker'}) SET s.kind = 'process'")
        page = team_page.team_page("payments")
        self.assertEqual([(group.kind, [item.name for item in group.items]) for group in page.owns], [("service", ["Billing Service"]), ("process", ["Invoice Worker"])])
        self.assertEqual(team_page.list_teams()[1].kinds, ["service", "process"])

    def test_a_superseded_decision_drops_off_and_a_private_one_never_shows(self):
        self.graph.query(
            """
            MATCH (old:Decision {id: 'decision-billing-retries'}), (s:Service {id: 'billing-service'})
            CREATE (new:Decision {id: 'decision-retries-in-worker', name: 'Move billing retries to the invoice worker', decided_at: 1792000000000}),
                   (new)-[:SUPERSEDES]->(old), (new)-[:ABOUT]->(s),
                   (secret:Decision {id: 'decision-secret', name: 'A private pricing call', scope: 'slack_channel_CPRIV', decided_at: 1793000000000}),
                   (secret)-[:ABOUT]->(s)
            """
        )
        decisions = team_page.team_page("payments").decisions
        self.assertEqual([decision.name for decision in decisions], ["Move billing retries to the invoice worker"])
        self.assertEqual((decisions[0].replaced, decisions[0].replaced_on), ("Retry renewal webhooks inside the billing service", "20 Sep 2026"))
        private_view = team_page.team_page("payments", scopes=("org_shared", "slack_channel_CPRIV"))
        self.assertEqual([decision.name for decision in private_view.decisions], ["A private pricing call", "Move billing retries to the invoice worker"])

    def test_a_private_newer_decision_does_not_hide_the_public_one(self):
        self.graph.query(
            """
            MATCH (old:Decision {id: 'decision-billing-retries'})
            CREATE (:Decision {id: 'decision-private-replacement', name: 'Private replacement', scope: 'slack_channel_CPRIV'})-[:SUPERSEDES]->(old)
            """
        )
        self.assertEqual([decision.name for decision in team_page.team_page("payments").decisions], ["Retry renewal webhooks inside the billing service"])

    def test_memories_about_what_the_team_runs_show_in_public_scope_only(self):
        # Only these three: the seed's own memory about billing would join the list.
        self.graph.query("MATCH (n) WHERE n:Memory OR n:SlackUser DETACH DELETE n")
        self.graph.query(
            """
            MATCH (s:Service {id: 'billing-service'})
            CREATE (:SlackUser {id: 'UARJUN', name: 'Arjun Mehta'})-[:SAID]->(m:Memory {id: 'm1', text: 'Retries now go through the invoice worker', scope: 'org_shared', created_at: 1792000000000})-[:MENTIONS]->(s),
                   (:Memory {id: 'm2', text: 'private note', scope: 'slack_channel_CPRIV', created_at: 1792000000000})-[:MENTIONS]->(s),
                   (:Memory {id: 'm3', text: 'forgotten note', scope: 'org_shared', is_forgotten: true, created_at: 1792000000000})-[:MENTIONS]->(s)
            """
        )
        memories = team_page.team_page("payments").memories
        self.assertEqual([(memory.text, memory.author, memory.about, memory.learned_at) for memory in memories], [("Retries now go through the invoice worker", "Arjun Mehta", "Billing Service", "2026-10-14")])

    def test_routes_404_an_unknown_team_and_serve_the_page(self):
        missing = self.http.get("/api/team/nope")
        self.assertEqual(missing.status_code, 404)
        self.assertIn("no team with id 'nope'", missing.json()["error"])
        self.assertEqual(self.http.get("/api/team/" + "x" * 200).status_code, 404)
        page = self.http.get("/team/payments")
        self.assertEqual(page.status_code, 200)
        self.assertIn("/static/team.js", page.text)
        self.assertIn("/static/graph.js", page.text)
        self.assertEqual(self.http.get("/static/team.js").status_code, 200)
        self.assertEqual(self.http.get("/static/graph.js").status_code, 200)
        # A team the graph does not have: still the page, which says so, but with a 404 status.
        for unknown in ("/team/nope", "/team/" + "x" * 200):
            missing_page = self.http.get(unknown)
            self.assertEqual(missing_page.status_code, 404, unknown)
            self.assertTrue(missing_page.headers["content-type"].startswith("text/html"))
            self.assertIn("/static/team.js", missing_page.text)
        landing = self.http.get("/").text
        self.assertIn('id="teams"', landing)
        self.assertIn("onboard team:", landing)

    def test_team_routes_share_the_public_rate_limit_and_cache(self):
        with mock.patch.object(ask_api, "LIMITER", ask_api.RateLimiter(limit=2, window=60)):
            self.assertEqual(self.http.get("/api/teams", headers={"X-Real-IP": "203.0.113.7"}).status_code, 200)
            self.assertEqual(self.http.get("/api/team/data", headers={"X-Real-IP": "203.0.113.7"}).status_code, 200)
            self.assertEqual(self.http.get("/api/team/data", headers={"X-Real-IP": "203.0.113.7"}).status_code, 429)
        with mock.patch.object(team_page, "team_page", wraps=team_page.team_page) as build:
            self.get("/api/team/platform")
            self.get("/api/team/platform")
        self.assertEqual(build.call_count, 1)

    def test_the_catalogue_route_names_the_tools_that_write(self):
        writes = self.get("/api/ask/tools")["writes"]
        self.assertIn("ingest_ticket", writes)
        self.assertIn("record_decision", writes)

    def test_suggested_questions_have_answers_with_a_path(self):
        platform = team_page.team_page("platform")
        self.assertIn("what breaks if Auth Service goes down", platform.questions)
        self.assertEqual({thing.name: thing.dependents for group in platform.owns for thing in group.items}["Auth Service"], 4)
        for team_id in ("data", "payments", "platform"):
            for question in team_page.team_page(team_id).questions:
                with self.subTest(team=team_id, question=question):
                    answer = ask_api.answer_question(question)
                    self.assertTrue(answer.found and answer.edges, f"{question}: {answer.answer}")

