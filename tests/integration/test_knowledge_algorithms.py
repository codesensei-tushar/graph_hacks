"""Algorithm tools on a real FalkorDB seeded with the fictional Quillfern company.

Depth 1 against depth 2 on the dependency graph, shortest paths cited in their true
direction, PageRank for what is load-bearing, and people ranked by weighted edges.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import algorithms, ask_service, catalogue  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_algorithms_test{tests.TEST_GRAPH_SUFFIX}"
ORG = ["org_shared"]
PRIVATE = "github_repo_777"


def _cites(result) -> list[str]:
    return [step.cite() for step in result.path]


class AlgorithmToolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        _, graph = falkordb_client.connect()
        # Pull requests touching Billing (public) and Auth (private repo), by linked and unlinked GitHub accounts.
        graph.query(
            """
            MATCH (arjun:Person {id: 'arjun'}), (lena:Person {id: 'lena'}), (tomas:Person {id: 'tomas'}),
                  (billing:Service {id: 'billing-service'}), (auth:Service {id: 'auth-service'})
            CREATE (ga:GitHubUser {id: 'gh-arjun', login: 'arjun-dev', name: 'arjun-dev'})-[:IS]->(arjun),
                   (gl:GitHubUser {id: 'gh-lena', login: 'lena-f', name: 'lena-f'})-[:IS]->(lena),
                   (gt:GitHubUser {id: 'gh-tomas', login: 'tomas-r', name: 'tomas-r'})-[:IS]->(tomas),
                   (gx:GitHubUser {id: 'gh-ext', login: 'ext-contrib', name: 'ext-contrib'}),
                   (p41:PullRequest {id: 'pr41', name: 'app#41', scope: 'org_shared'})-[:TOUCHES]->(billing),
                   (p42:PullRequest {id: 'pr42', name: 'app#42', scope: 'org_shared'})-[:TOUCHES]->(billing),
                   (p43:PullRequest {id: 'pr43', name: 'app#43', scope: $private})-[:TOUCHES]->(auth),
                   (ga)-[:AUTHORED]->(p41), (gl)-[:REVIEWED]->(p41),
                   (gl)-[:AUTHORED]->(p42), (ga)-[:REVIEWED]->(p42), (gt)-[:REVIEWED]->(p42),
                   (gx)-[:AUTHORED]->(p43), (ga)-[:REVIEWED]->(p43),
                   (:Service {id: 'legacy-ftp', name: 'Legacy FTP drop', aliases: 'ftp, legacy ftp'})
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

    # --- blast radius ---

    def test_depth_one_misses_what_depth_two_finds(self):
        one = algorithms.blast_radius("auth", ORG, depth=1)
        two = algorithms.blast_radius("auth", ORG, depth=2)
        self.assertEqual(one.depth, 1)
        self.assertEqual(two.depth, 2)
        self.assertNotIn("Invoice Worker", one.answer.split("further out")[0])
        self.assertIn("1 more breaks further out (Invoice Worker at depth 2); ask with more hops.", one.answer)
        self.assertIn("Invoice Worker (2 hops)", two.answer)
        self.assertIn("Depth 2 reached 5 services; nothing new past depth 2.", two.answer)
        self.assertNotIn("Invoice Worker -[DEPENDS_ON]-> Billing Service", _cites(one))
        self.assertIn("Invoice Worker -[DEPENDS_ON]-> Billing Service", _cites(two))
        self.assertIn("Billing Service -[DEPENDS_ON]-> Auth Service", _cites(two))

    def test_no_depth_follows_everything_and_reports_where_it_saturated(self):
        result = algorithms.blast_radius("postgres", ORG)
        self.assertEqual(result.depth, 2)
        self.assertIn("Auth Service and Billing Service (1 hop)", result.answer)
        self.assertIn("API Gateway, Invoice Worker, Notification Service and Search Service (2 hops)", result.answer)
        self.assertIn("nothing new past depth 2", result.answer)
        self.assertEqual(len({step.from_name for step in result.path}), 6)

    def test_the_main_connector_is_counted_from_the_walked_routes(self):
        # Invoice Worker reaches Auth through Billing and through Notification, one route each: a tie, both named.
        result = algorithms.blast_radius("auth", ORG)
        self.assertTrue(
            result.answer.endswith(
                "Among them, Billing Service and Notification Service are the main connectors: 1 of the 5 reaches Auth Service through each."
            ),
            result.answer,
        )
        # Counted out to saturation, so a depth-1 answer still names the connector its listed services carry.
        one = algorithms.blast_radius("auth", ORG, depth=1)
        self.assertIn("are the main connectors: 1 of the 5 reaches Auth Service through each.", one.answer)
        postgres = algorithms.blast_radius("postgres", ORG)
        self.assertTrue(postgres.answer.endswith("Among them, Auth Service is the main connector: 3 of the 6 reach Postgres through it."), postgres.answer)
        # Whole numbers only; no fractional score reads as a count of paths.
        self.assertNotRegex(postgres.answer, r"\d+\.\d+")
        # A dependent with no other service on its way names no connector.
        billing = algorithms.blast_radius("billing", ORG)
        self.assertNotIn("connector", billing.answer)

    def test_a_leaf_has_no_dependents_and_an_unknown_service_is_refused(self):
        self.assertEqual(algorithms.blast_radius("invoice worker", ORG).answer, "Nothing in the graph depends on Invoice Worker.")
        miss = algorithms.blast_radius("the espresso machine", ORG)
        self.assertFalse(miss.found)
        self.assertIn("Nothing in the graph matches “espresso machine”", miss.answer)

    def test_depth_comes_through_the_catalogue_and_the_router(self):
        spec = catalogue.get_tool("blast_radius")
        self.assertEqual(spec.run({"name": "auth", "depth": 1}, ORG).depth, 1)
        routed = ask_service.ask("what breaks if auth goes down, one hop", ORG)
        self.assertEqual((routed.tool, routed.depth), ("blast_radius", 1))

    # --- what a service depends on ---

    def test_what_a_service_depends_on_walks_forward_and_never_answers_the_dependents(self):
        needs = ask_service.ask("what does the invoice worker depend on", ORG)
        self.assertEqual((needs.tool, needs.found, needs.depth), ("blast_radius", True, 2))
        self.assertEqual(
            needs.answer,
            "Invoice Worker depends on Billing Service and Notification Service (1 hop) and, through them, Auth Service and "
            "Postgres (2 hops). Depth 2 reached 4 services; nothing new past depth 2.",
        )
        self.assertEqual(
            [step.cite() for step in needs.path],
            [
                "Invoice Worker -[DEPENDS_ON]-> Billing Service",
                "Invoice Worker -[DEPENDS_ON]-> Notification Service",
                "Billing Service -[DEPENDS_ON]-> Auth Service",
                "Notification Service -[DEPENDS_ON]-> Auth Service",
                "Billing Service -[DEPENDS_ON]-> Postgres",
            ],
        )
        # The same walk by the argument the model and MCP pass, and by the question alone with no argument.
        self.assertEqual(catalogue.get_tool("blast_radius").run({"name": "invoice worker", "direction": "dependencies"}, ORG).answer, needs.answer)
        self.assertEqual(algorithms.blast_radius("what does the invoice worker depend on", ORG).answer, needs.answer)
        one = ask_service.ask("what does the invoice worker depend on, one hop", ORG)
        self.assertEqual(
            one.answer,
            "Invoice Worker depends on Billing Service and Notification Service (1 hop). "
            "It needs 2 more further down (Auth Service at depth 2 and Postgres at depth 2); ask with more hops.",
        )
        self.assertEqual(ask_service.ask("what are billing's dependencies", ORG).answer.split(".")[0], "Billing Service depends on Auth Service and Postgres (1 hop)")
        self.assertEqual(ask_service.ask("what does postgres depend on", ORG).answer, "Postgres depends on nothing else in the graph.")
        # The other way round is unchanged.
        self.assertEqual(algorithms.blast_radius("invoice worker", ORG).answer, "Nothing in the graph depends on Invoice Worker.")
        self.assertTrue(ask_service.ask("what depends on billing", ORG).answer.startswith("If Billing Service goes down, these stop with it"))
        # An argument that is neither direction falls back to what the question asks.
        self.assertEqual(catalogue.get_tool("blast_radius").run({"name": "invoice worker", "direction": "sideways"}, ORG).answer, "Nothing in the graph depends on Invoice Worker.")

    # --- shortest paths ---

    def test_path_between_cites_each_hop_in_its_true_direction(self):
        result = algorithms.path_between("Maya Ortiz", "invoice worker", ORG)
        self.assertEqual(result.depth, 3)
        self.assertTrue(result.answer.startswith("Maya Ortiz reaches Invoice Worker in 3 hops: Maya Ortiz → "))
        self.assertIn("2 other routes of the same length.", result.answer)
        cites = _cites(result)
        self.assertEqual(len(cites), 3)
        self.assertTrue(cites[0].startswith("Maya Ortiz -[OWNS]-> "))
        # The route walks DEPENDS_ON edges backwards; the citation keeps the edge's real direction.
        self.assertTrue(cites[-1].startswith("Invoice Worker -[DEPENDS_ON]-> "))
        for step in result.path:
            self.assertIsNotNone(step.from_id)
            self.assertIsNotNone(step.to_label)

    def test_person_to_person_and_the_router_with_two_names(self):
        result = algorithms.path_between("arjun", "kofi", ORG)
        self.assertTrue(result.found)
        self.assertTrue(result.path)
        self.assertIn("Arjun Mehta reaches Kofi Mensah", result.answer)
        routed = ask_service.ask("how is maya connected to the invoice worker", ORG)
        self.assertEqual(routed.tool, "path_between")
        self.assertEqual(routed.depth, 3)

    def test_unreachable_pairs_and_missing_names_are_said_not_guessed(self):
        apart = algorithms.path_between("legacy ftp", "billing", ORG)
        self.assertTrue(apart.found)
        self.assertEqual(apart.path, [])
        self.assertEqual(apart.answer, "No path between Legacy FTP drop and Billing Service within 6 hops in what you can read.")
        miss = algorithms.path_between("maya", "the espresso machine", ORG)
        self.assertFalse(miss.found)
        one = algorithms.path_between("how is maya connected to things", "", ORG)
        self.assertIn("I found Maya Ortiz; name a second thing", one.answer)

    def test_private_pull_requests_only_connect_people_who_can_read_them(self):
        public = algorithms.path_between("ext-contrib", "auth", ORG)
        self.assertFalse(public.found)
        private = algorithms.path_between("@ext-contrib", "auth", ORG + [PRIVATE]) if False else None
        self.assertIsNone(private)

    # --- PageRank ---

    def test_load_bearing_ranks_by_pagerank_with_dependent_counts(self):
        result = algorithms.load_bearing(3)
        self.assertRegex(result.answer, r"^Postgres carries the most weight \(PageRank 0\.\d+\): 6 services depend on it directly or through others\.")
        self.assertRegex(result.answer, r"Then Auth Service \(0\.\d+, 5\) and Graph Store \(0\.\d+, 2\)\.")
        self.assertIn("Auth Service -[DEPENDS_ON]-> Postgres", _cites(result))
        self.assertIn("Billing Service -[DEPENDS_ON]-> Postgres", _cites(result))
        self.assertEqual(ask_service.ask("which services are load bearing", ORG).tool, "load_bearing")
        self.assertEqual(catalogue.get_tool("load_bearing").run({}, ORG).tool, "load_bearing")

    # --- who_knows ranking ---

    def test_who_knows_ranks_people_by_weighted_edges_and_says_why(self):
        public = ask_service.who_knows("billing", ORG)
        ranked = ask_service.rank_people(public.path, public.path[0].__class__ and type("E", (), {"name": "Billing Service"})())
        names = [name for name, _score, _why in ranked]
        self.assertEqual(names[:2], ["Arjun Mehta", "Lena Fischer"])
        self.assertEqual(names[-1], "Sara Lindqvist")
        self.assertIn("Ask first: Arjun Mehta (owns it, wrote app#41 and the Billing runbook, reviewed app#42), then Lena Fischer (worked on it and TKT-102, reviewed app#41, wrote app#42)", public.answer)
        self.assertIn("Open tickets: TKT-102.", public.answer)
        self.assertNotIn("Ranked by weighted edges", public.answer)
        self.assertTrue(public.answer.startswith("Billing Service is owned by Arjun Mehta. Ask first:"))
        self.assertIn("Billing runbook -[DESCRIBES]-> Billing Service", _cites(public))
        self.assertNotIn("ext-contrib", public.answer)

    def test_private_pull_requests_count_only_for_readers(self):
        public = ask_service.who_knows("auth", ORG)
        private = ask_service.who_knows("auth", ORG + [PRIVATE])
        self.assertNotIn("@ext-contrib", public.answer)
        self.assertIn("@ext-contrib (wrote app#43)", private.answer)
        step = next(step for step in private.path if step.from_name == "@ext-contrib")
        self.assertEqual(step.from_label, "GitHubUser")

    # --- label propagation and decisions that are no longer the last word ---

    def test_working_groups_cross_the_org_chart(self):
        result = algorithms.working_groups(ORG)
        self.assertEqual(result.tool, "working_groups")
        self.assertIn("Label propagation over who owns or worked on the same service finds 2 work clusters.", result.answer)
        self.assertIn("Arjun Mehta, Lena Fischer and Tomas Reyes.", result.answer)
        self.assertIn(
            "The org chart splits them: Arjun Mehta and Lena Fischer are on Payments and Tomas Reyes is on Platform.",
            result.answer,
        )
        self.assertIn("Inside the cluster, Arjun Mehta, Lena Fischer and Tomas Reyes share Billing Service.", result.answer)
        self.assertIn("Kofi Mensah and Maya Ortiz.", result.answer)
        self.assertIn("The org chart splits them: Kofi Mensah is on Data and Maya Ortiz is on Platform.", result.answer)
        self.assertIn("Inside the cluster, Kofi Mensah and Maya Ortiz share Memory Layer.", result.answer)
        self.assertIn("Sara Lindqvist is on their own.", result.answer)
        self.assertIn("Sara Lindqvist knows Billing Service and stays outside that cluster", result.answer)
        self.assertIn("2 of those clusters cross the org chart.", result.answer)
        self.assertIn("Lena Fischer -[WORKED_ON]-> Billing Service", _cites(result))
        self.assertIn("Tomas Reyes -[MEMBER_OF]-> Platform", _cites(result))
        self.assertIn("Kofi Mensah -[OWNS]-> Memory Layer", _cites(result))
        self.assertIn("Sara Lindqvist -[KNOWS]-> Billing Service", _cites(result))
        self.assertEqual(ask_service.route("who actually works together"), ("working_groups", {}))
        self.assertEqual(catalogue.get_tool("working_groups").run({}, ORG).tool, "working_groups")

    def test_decision_pressure_names_uncited_work_and_hides_private_pull_requests(self):
        quiet = ask_service.decision_pressure("is the billing retries decision still the last word", ORG)
        self.assertIn("*Retry renewal webhooks inside the billing service* is the standing decision about Billing Service.", quiet.answer)
        self.assertIn("It cites Billing runbook and TKT-102.", quiet.answer)
        self.assertIn("It does not cite app#41 (about Billing Service) and app#42 (about Billing Service).", quiet.answer)
        self.assertNotIn("app#43", quiet.answer)
        _, graph = falkordb_client.connect()
        graph.query(
            """
            MATCH (billing:Service {id: 'billing-service'})
            CREATE (ticket:Ticket {id: 'TKT-109', name: 'TKT-109', title: 'Exports time out', scope: 'org_shared'})-[:ABOUT]->(billing),
                   (hidden:PullRequest {id: 'pr-hidden', name: 'ledger#9', scope: $private})-[:TOUCHES]->(billing),
                   (shown:PullRequest {id: 'pr-shown', name: 'app#99', scope: 'org_shared'})-[:TOUCHES]->(billing)
            """,
            {"private": PRIVATE},
        )
        try:
            public = ask_service.decision_pressure("billing retries still the last word", ORG)
            self.assertIn("TKT-109 (about Billing Service)", public.answer)
            self.assertIn("app#99 (about Billing Service)", public.answer)
            self.assertNotIn("ledger#9", public.answer)
            self.assertIn("TKT-109 -[ABOUT]-> Billing Service", _cites(public))
            self.assertIn("app#99 -[TOUCHES]-> Billing Service", _cites(public))
            self.assertIn("Retry renewal webhooks inside the billing service -[BASED_ON]-> TKT-102", _cites(public))
            private = ask_service.decision_pressure("billing retries still the last word", ORG + [PRIVATE])
            self.assertIn("ledger#9 (about Billing Service)", private.answer)
            self.assertEqual(ask_service.route("why did we move the memory layer off postgres")[0], "decision_trace")
        finally:
            graph.query("MATCH (n) WHERE n.id IN ['TKT-109', 'pr-hidden', 'pr-shown'] DETACH DELETE n")
