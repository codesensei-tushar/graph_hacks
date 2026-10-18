"""explore, find and neighbors on a real FalkorDB: Quillfern, plus one pull request, one private ticket and one forgotten memory.

The pull request is written by Arjun through his linked GitHub account and reviewed by an
account linked to nobody, so the walks show the person for one and the account for the other.
The private ticket is scoped to one channel: a public reader never sees it, a reader of that
channel does. The forgotten memory mentions Billing Service and is never shown.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import agent, algorithms, ask_api, ask_service, catalogue, explore, fallback, find, mcp_tools  # noqa: E402
from src.seed import quillfern, quillfern_sales, seed  # noqa: E402

GRAPH = f"company_general_tools_test{tests.TEST_GRAPH_SUFFIX}"
ORG = ["org_shared"]
PRIVATE = "slack_channel_C9"
BOTH = ORG + [PRIVATE]

BILLING = (
    "Billing Service has 14 direct connections: owned by Arjun Mehta; run by the Payments team; worked on by Lena Fischer "
    "and Tomas Reyes; known by Sara Lindqvist; depends on Auth Service and Postgres; depended on by API Gateway and Invoice "
    "Worker; described by Billing runbook; decisions about it: *Retry renewal webhooks inside the billing service*; tickets "
    "about it: TKT-102; changed by notebook#52; mentioned in 1 memory."
)


def _cites(result) -> list[str]:
    return [step.cite() for step in result.path]


def _run(tool: str, args: dict, scopes=None):
    return catalogue.get_tool(tool).run(args, scopes or ORG)


def _ask(question: str, scopes=None):
    tool, args = ask_service.route(question)
    return catalogue.get_tool(tool).run(args, scopes or ORG)


class GeneralToolsTest(unittest.TestCase):
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
        graph.query(
            """
            MATCH (arjun:Person {id: 'arjun'}), (billing:Service {id: 'billing-service'})
            CREATE (gh:GitHubUser {id: 'gh-arjun', login: 'arjun-mehta'})-[:IS]->(arjun),
                   (outsider:GitHubUser {id: 'gh-outsider', login: 'outsider'}),
                   (pr:PullRequest {id: 'pr-52', name: 'notebook#52', title: 'Retry renewal webhooks once', state: 'open', merged: false, scope: 'org_shared'}),
                   (gh)-[:AUTHORED]->(pr), (outsider)-[:REVIEWED]->(pr), (pr)-[:TOUCHES]->(billing),
                   (merged:PullRequest {id: 'pr-40', name: 'notebook#40', title: 'Old invoice layout', state: 'closed', merged: true, scope: 'org_shared'}),
                   (gh)-[:AUTHORED]->(merged),
                   (secret:Ticket {id: 'TKT-900', name: 'TKT-900', title: 'Private refund bug', scope: $private})-[:ABOUT]->(billing),
                   (gone:Memory {id: 'memory-forgotten', scope: 'org_shared', is_forgotten: true, text: 'billing forgotten note', name: 'billing forgotten note'})-[:MENTIONS]->(billing)
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

    def setUp(self):
        ask_api.ANSWERS.clear()

    # --- explore -----------------------------------------------------------------------

    def test_explore_groups_every_edge_by_relationship_and_cites_it(self):
        result = _ask("explore billing")
        self.assertEqual((result.tool, result.found, result.depth), ("explore", True, 1))
        self.assertEqual(result.answer, BILLING)
        cites = _cites(result)
        self.assertIn("Arjun Mehta -[OWNS]-> Billing Service", cites)
        self.assertIn("Billing Service -[DEPENDS_ON]-> Auth Service", cites)
        self.assertIn("Payments -[RUNS]-> Billing Service", cites)
        self.assertIn("notebook#52 -[TOUCHES]-> Billing Service", cites)
        for step in result.path:
            self.assertTrue(step.from_id and step.to_id and step.from_label and step.to_label, step)
        # The memory is dated; the forgotten one and the private ticket are not shown.
        self.assertEqual([(memory.learned_at, memory.author) for memory in result.memories], [("2026-09-20", "Arjun Mehta")])
        self.assertNotIn("forgotten", " ".join(cites))
        self.assertNotIn("TKT-900", result.answer)

    def test_a_private_ticket_shows_only_to_a_reader_of_its_room(self):
        self.assertIn("tickets about it: TKT-102 and TKT-900", explore.explore("billing", BOTH).answer)
        self.assertIn("TKT-900 -[ABOUT]-> Billing Service", _cites(explore.explore("billing", BOTH)))
        self.assertFalse(explore.explore("TKT-900", ORG).found)
        self.assertEqual(explore.explore("TKT-900", BOTH).answer, "TKT-900 is “Private refund bug”, open. It has 1 direct connection: about Billing Service.")

    def test_depth_two_counts_the_second_ring_and_cites_a_few_of_its_edges(self):
        result = explore.explore("billing", ORG, 2)
        self.assertEqual(result.depth, 2)
        self.assertTrue(result.answer.startswith(BILLING), result.answer)
        self.assertTrue(result.answer.endswith("Two hops out there are 17 more: 3 services, 1 person, 1 team, 2 documents, 1 ticket, 2 decisions, 1 pull request, 5 memories and 1 GitHub account."), result.answer)
        self.assertLessEqual(len(result.path), explore.PATH_CAP)
        ring = [step for step in result.path if step.cite() not in _cites(explore.explore("billing", ORG))]
        self.assertTrue(0 < len(ring) <= explore.RING_CITES, ring)
        self.assertEqual(ask_service.route("what do we know about billing two hops out"), ("explore", {"name": "what do we know about billing two hops out", "depth": 2}))

    def test_explore_a_team_a_document_a_ticket_and_a_person(self):
        cases = {
            "what do we know about the platform team": (
                "The Platform team has 7 direct connections: runs API Gateway, Auth Service, Notification Service and Postgres; "
                "members: Maya Ortiz, Sara Lindqvist and Tomas Reyes."
            ),
            "explore the auth session design": "Auth session design has 2 direct connections: describes Auth Service; written by Maya Ortiz.",
            "everything on TKT-102": (
                "TKT-102 is “Annual plan renewals charge twice on retry”, open. It has 4 direct connections: worked on by Lena Fischer; "
                "about Billing Service; the basis of *Retry renewal webhooks inside the billing service*; mentioned in 1 memory."
            ),
        }
        for question, answer in cases.items():
            with self.subTest(question=question):
                result = _ask(question)
                self.assertEqual((result.tool, result.found, result.answer), ("explore", True, answer))
        # "payments" is also Billing Service's alias; before "team" it is the team.
        self.assertTrue(_ask("explore the payments team").answer.startswith("The Payments team has"), _ask("explore the payments team").answer)
        arjun = _ask("explore arjun")
        self.assertIn("wrote Billing runbook, notebook#40 and notebook#52", arjun.answer)
        self.assertIn("Arjun Mehta -[AUTHORED]-> notebook#52", _cites(arjun))
        self.assertIn("quoted in 1 memory", arjun.answer)

    def test_explore_a_replaced_decision_says_what_stands_now(self):
        result = _ask("explore Keep notebook links in Postgres join tables")
        self.assertEqual(
            result.answer,
            "*Keep notebook links in Postgres join tables* was decided on 1 Jun 2026 and no longer stands: *Move the memory layer off "
            "Postgres* does. It has 5 direct connections: about Memory Layer; proposed by Sara Lindqvist; approved by Tomas Reyes; "
            "replaced by *Move the memory layer off Postgres*; based on RFC: memory layer on a graph store.",
        )
        self.assertIn("Move the memory layer off Postgres -[SUPERSEDES]-> Keep notebook links in Postgres join tables", _cites(result))
        memory_layer = explore.explore("memory layer", ORG).answer
        self.assertIn("decisions about it: *Keep notebook links in Postgres join tables* (replaced) and *Move the memory layer off Postgres*", memory_layer)
        # The decision a question routes to is explored, not traced.
        standing = _ask("explore the billing retries decision")
        self.assertTrue(standing.answer.startswith("*Retry renewal webhooks inside the billing service* was decided on 20 Sep 2026 and still stands."), standing.answer)

    def test_a_pull_request_shows_people_not_accounts(self):
        result = explore.explore("notebook#52", ORG)
        self.assertEqual(
            result.answer,
            "notebook#52 is “Retry renewal webhooks once”, open. It has 3 direct connections: written by Arjun Mehta; changes "
            "Billing Service; reviewed by @outsider.",
        )
        authored = next(step for step in result.path if step.rel == "AUTHORED")
        self.assertEqual((authored.from_name, authored.from_id, authored.from_label), ("Arjun Mehta", "arjun", "Person"))
        reviewed = next(step for step in result.path if step.rel == "REVIEWED")
        self.assertEqual((reviewed.from_name, reviewed.from_label), ("@outsider", "GitHubUser"))

    def test_explore_takes_an_id_and_refuses_what_it_cannot_resolve(self):
        self.assertTrue(explore.explore("decision-billing-retries", ORG).answer.startswith("*Retry renewal webhooks inside the billing service* was decided"))
        miss = _ask("explore the payments gateway")
        self.assertEqual((miss.tool, miss.found), ("explore", False))
        self.assertTrue(miss.answer.startswith("Nothing in the graph matches “payments gateway” as one thing. Nearest:"), miss.answer)
        self.assertEqual(miss.candidates[:2], ["Billing Service", "API Gateway"])

    # --- find --------------------------------------------------------------------------

    def test_find_lists_by_filter_and_cites_the_edges_that_pass(self):
        tickets = _run("find", {"label": "Ticket", "state": "open", "about": "billing"})
        self.assertEqual(tickets.answer, "1 open ticket about Billing Service: TKT-102 (Annual plan renewals charge twice on retry).")
        self.assertEqual(_cites(tickets), ["TKT-102 -[ABOUT]-> Billing Service"])
        private = _run("find", {"label": "Ticket", "state": "open", "about": "billing"}, BOTH)
        self.assertEqual(
            private.answer,
            "2 open tickets about Billing Service: TKT-102 (Annual plan renewals charge twice on retry) and TKT-900 (Private refund bug).",
        )
        services = _run("find", {"label": "Service", "team": "payments"})
        self.assertEqual(services.answer, "2 services run by the Payments team: Billing Service and Invoice Worker.")
        self.assertEqual(_cites(services), ["Payments -[RUNS]-> Billing Service", "Payments -[RUNS]-> Invoice Worker"])
        standing = _run("find", {"label": "Decision", "about": "memory layer", "standing": True})
        self.assertEqual(standing.answer, "1 standing decision about Memory Layer: *Move the memory layer off Postgres* (15 Sep 2026).")
        replaced = _run("find", {"label": "Decision", "standing": "false"})
        self.assertEqual(replaced.answer, "1 replaced decision: *Keep notebook links in Postgres join tables* (1 Jun 2026, replaced).")
        people = _run("find", {"label": "Person", "holds": "billing"})
        self.assertEqual(
            people.answer,
            "4 people who own, worked on or know Billing Service: Arjun Mehta (Billing engineer), Lena Fischer (Backend engineer), "
            "Sara Lindqvist (Site reliability engineer) and Tomas Reyes (Engineering manager).",
        )
        documents = _run("find", {"label": "Document", "team": "payments"})
        self.assertEqual(documents.answer, "2 documents describing what the Payments team runs: Billing runbook and Invoice format spec.")
        self.assertIn("Payments -[RUNS]-> Invoice Worker", _cites(documents))

    def test_find_follows_a_person_through_their_github_account(self):
        authored = _run("find", {"label": "PullRequest", "author": "arjun"})
        self.assertEqual(authored.answer, "2 pull requests by Arjun Mehta: notebook#40 (Old invoice layout) and notebook#52 (Retry renewal webhooks once).")
        self.assertIn("Arjun Mehta -[AUTHORED]-> notebook#52", _cites(authored))
        merged = _run("find", {"label": "PullRequest", "state": "merged"})
        self.assertEqual(merged.answer, "1 merged pull request: notebook#40 (Old invoice layout).")
        touching = _run("find", {"label": "PullRequest", "state": "open", "touches": "billing"})
        self.assertEqual(touching.answer, "1 open pull request touching Billing Service: notebook#52 (Retry renewal webhooks once).")

    def test_find_refuses_what_it_cannot_match_and_says_none_when_nothing_passes(self):
        unknown = _run("find", {"label": "Ticket", "about": "the payments gateway"})
        self.assertFalse(unknown.found)
        self.assertEqual(unknown.candidates[:2], ["Billing Service", "API Gateway"])
        self.assertFalse(_run("find", {"label": "Service", "team": "marketing"}).found)
        not_allowed = _run("find", {"label": "Team", "kind": "process"})
        self.assertEqual((not_allowed.found, not_allowed.answer), (False, "Teams take no filters; not by kind."))
        bad_state = _run("find", {"label": "Ticket", "state": "merged"})
        self.assertEqual((bad_state.found, bad_state.answer), (False, "“merged” is not a state. A ticket is open or closed."))
        none = _run("find", {"label": "Ticket", "state": "closed"})
        self.assertEqual((none.found, none.answer, none.path), (True, "No closed tickets.", []))

    def test_find_from_the_keyword_router_and_from_a_tool_named_outright(self):
        open_tickets = _ask("which tickets are open")
        self.assertEqual((open_tickets.tool, open_tickets.found), ("find", True))
        self.assertTrue(open_tickets.answer.startswith("3 open tickets: TKT-101 (Memory queries time out on large workspaces)"), open_tickets.answer)
        self.assertIn("TKT-101 -[ABOUT]-> Memory Layer", _cites(open_tickets))
        decisions = _ask("what decisions have we made")
        self.assertTrue(decisions.answer.startswith("3 decisions: *Retry renewal webhooks inside the billing service* (20 Sep 2026)"), decisions.answer)
        # Named outright, the question fills the label; find reads the list it asks for from it.
        page = ask_api.answer_question("list the open tickets about billing", tool="find")
        self.assertEqual((page.tool, page.answer), ("find", "1 open ticket about Billing Service: TKT-102 (Annual plan renewals charge twice on retry)."))
        self.assertEqual([node.name for node in page.nodes], ["TKT-102", "Billing Service"])

    # --- neighbors ---------------------------------------------------------------------

    def test_neighbors_agrees_with_blast_radius(self):
        walked = _run("neighbors", {"name": "auth", "relation": "DEPENDS_ON", "direction": "in", "depth": 2})
        self.assertEqual(
            walked.answer,
            "Following DEPENDS_ON into Auth Service: API Gateway, Billing Service, Notification Service and Search Service (1 hop); then Invoice Worker (2 hops).",
        )
        blast = algorithms.blast_radius("what breaks if auth goes down", ORG, 2)
        reached = {name for step in walked.path for name in (step.from_name, step.to_name)}
        broken = {name for step in blast.path for name in (step.from_name, step.to_name)}
        self.assertEqual(reached, broken)
        self.assertIn("Invoice Worker -[DEPENDS_ON]-> Notification Service", _cites(walked))

    def test_neighbors_follows_one_relationship_either_way(self):
        members = _run("neighbors", {"name": "the platform team", "relation": "member_of", "direction": "in"})
        self.assertEqual(members.answer, "Following MEMBER_OF into the Platform team: Maya Ortiz, Sara Lindqvist and Tomas Reyes (1 hop).")
        needs = _run("neighbors", {"name": "invoice worker", "relation": "DEPENDS_ON", "direction": "out", "depth": 3})
        self.assertEqual(
            needs.answer,
            "Following DEPENDS_ON out of Invoice Worker: Billing Service and Notification Service (1 hop); then Auth Service and "
            "Postgres (2 hops). Nothing new past 2 hops.",
        )
        authors = _run("neighbors", {"name": "notebook#52", "relation": "AUTHORED", "direction": "in"})
        self.assertEqual(authors.answer, "Following AUTHORED into notebook#52: Arjun Mehta (1 hop).")
        bad = _run("neighbors", {"name": "auth", "relation": "LIKES"})
        self.assertFalse(bad.found)
        self.assertTrue(bad.answer.startswith("“LIKES” is not a relationship neighbors follows."), bad.answer)

    # --- the refusal fallback ----------------------------------------------------------

    def test_a_refusal_about_a_node_that_is_not_a_person_explores_it(self):
        team = ask_api.answer_question("what breaks if the platform team goes down")
        self.assertEqual((team.tool, team.found), ("explore", True))
        self.assertTrue(team.answer.startswith("The Platform team has 7 direct connections"), team.answer)
        # Nothing resolves: still refused, word for word, as the demo's refusal chip.
        gateway = ask_api.answer_question("who knows the payments gateway")
        self.assertEqual((gateway.tool, gateway.found), ("who_knows", False))
        # A person goes to person_profile, never to explore.
        self.assertIsNone(explore.before_refusing("what breaks if arjun goes down", ORG))
        self.assertIsNone(explore.before_refusing("what decisions have we made about postgres", ORG))
        self.assertFalse(ask_api.answer_question("what decisions have we made about postgres").found)

    def test_the_explore_step_sits_between_person_and_search(self):
        self.assertEqual(fallback.STEPS, (fallback._person, fallback._explore, fallback._search, fallback._recall))
        self.assertEqual(fallback._explore("who owns the platform team", ORG).tool, "explore")

    # --- surfaces ----------------------------------------------------------------------

    def test_mcp_lists_all_three_and_calls_them(self):
        for name in ("explore", "find", "neighbors"):
            self.assertIn(name, mcp_tools.tool_names())
        explored = mcp_tools.call_tool("explore", {"name": "billing"})
        self.assertEqual((explored["tool"], explored["found"]), ("explore", True))
        self.assertIn({"from": "Arjun Mehta", "rel": "OWNS", "to": "Billing Service"}, explored["path"])
        listed = mcp_tools.call_tool("find", {"label": "Ticket", "state": "open"})
        self.assertTrue(listed["answer"].startswith("3 open tickets"), listed["answer"])
        walked = mcp_tools.call_tool("neighbors", {"name": "auth", "relation": "DEPENDS_ON", "direction": "in", "depth": 2})
        self.assertEqual(walked["depth"], 2)
        with self.assertRaises(mcp_tools.ToolError):
            mcp_tools.call_tool("find", {"label": "Ticket", "drop": "everything"})

    def test_the_model_can_fill_find_and_the_page_draws_explore(self):
        spec = catalogue.get_tool("find")
        args = agent.validate_args(spec, {"label": "Decision", "about": " memory layer ", "standing": True, "junk": 1})
        self.assertEqual(args, {"label": "Decision", "about": "memory layer", "standing": True})
        self.assertEqual(spec.run(args, ORG).answer, "1 standing decision about Memory Layer: *Move the memory layer off Postgres* (15 Sep 2026).")
        page = ask_api.answer_question("explore billing")
        self.assertEqual((page.tool, page.picked_by, page.found), ("explore", "keywords", True))
        self.assertIn("Arjun Mehta", [node.name for node in page.nodes])
        self.assertIn("explore", [tool.name for tool in ask_api.read_tools()])


class GeneralToolsWithSalesTest(unittest.TestCase):
    """With the Sales team imported, as production has it."""

    @classmethod
    def setUpClass(cls):
        graph = f"company_general_tools_sales_test{tests.TEST_GRAPH_SUFFIX}"
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=graph),
            mock.patch.object(seed, "graph_name", return_value=graph),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        quillfern_sales.seed_sales()

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(falkordb_client.graph_name()).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def setUp(self):
        ask_api.ANSWERS.clear()

    def test_a_team_question_another_tool_refuses_explores_the_team(self):
        sales = ask_api.answer_question("what breaks if the sales team goes down")
        self.assertEqual((sales.tool, sales.found), ("explore", True))
        self.assertEqual(
            sales.answer,
            "The Sales team has 7 direct connections: runs Enterprise accounts, HubSpot system, Pricing playbook and Renewals process; "
            "members: Dev Patel, Noor Haddad and Priya Shah.",
        )
        self.assertIn("Sales -[RUNS]-> Renewals", [step["from"] + " -[" + step["rel"] + "]-> " + step["to"] for step in sales.path])
        platform = ask_api.answer_question("who owns the platform team")
        self.assertEqual((platform.tool, platform.found), ("explore", True))

    def test_find_lists_a_teams_processes_and_its_decisions(self):
        processes = find.find("Service", {"kind": "process", "team": "sales"}, ORG)
        self.assertEqual(processes.answer, "1 process run by the Sales team: Renewals process.")
        decisions = _ask("list the standing decisions in sales")
        self.assertEqual((decisions.tool, decisions.found), ("find", True))
        self.assertEqual(decisions.answer, "1 standing decision about what the Sales team runs: *Sell annual plans only at renewal* (1 Sep 2026).")
