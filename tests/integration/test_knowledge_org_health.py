"""Org health on a real FalkorDB: team_dependencies and decision_health on plain Quillfern, with
Sales imported, and with fixtures for the cases the seed does not hold.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import ask_api, ask_service, catalogue, mcp_tools, org_health  # noqa: E402
from src.seed import quillfern, quillfern_sales, seed  # noqa: E402

GRAPH = f"company_org_health_test{tests.TEST_GRAPH_SUFFIX}"
SALES_GRAPH = f"company_org_health_sales_test{tests.TEST_GRAPH_SUFFIX}"
FIXTURE_GRAPH = f"company_org_health_fixture_test{tests.TEST_GRAPH_SUFFIX}"
ORG = ["org_shared"]
PRIVATE = "slack_channel_C9"

PLATFORM = (
    "2 teams depend on Platform: Payments through 3 links (Billing Service → Auth Service, Billing Service → Postgres, "
    "Invoice Worker → Notification Service) and Data through 1 (Search Service → Auth Service). "
    "Platform also depends on Payments (API Gateway → Billing Service), so the two depend on each other."
)
PAYMENTS_NEEDS = (
    "Payments depends on Platform for Auth Service, Postgres and Notification Service "
    "(Billing Service → Auth Service, Billing Service → Postgres, Invoice Worker → Notification Service)."
)
HEALTHY = (
    "2 standing decisions, 1 replaced, and none needs attention: each cites its evidence, has an approver, and nothing it "
    "does not cite has landed on what it is about. History: *Keep notebook links in Postgres join tables* (1 Jun 2026) "
    "was replaced by *Move the memory layer off Postgres* (15 Sep 2026)."
)


def _ask(question: str, scopes=None):
    return ask_service.ask(question, scopes or ORG)


def _cites(result) -> list[str]:
    return [step.cite() for step in result.path]


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
        ask_api.ANSWERS.clear()

    @classmethod
    def seed_more(cls):
        pass

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(cls.graph_name).delete()
        finally:
            ask_api.ANSWERS.clear()
            for patch in cls._patches:
                patch.stop()


class TeamDependenciesTest(_Graph):
    def test_which_teams_depend_on_platform(self):
        result = _ask("which teams depend on platform")
        self.assertEqual((result.tool, result.found, result.answer), ("team_dependencies", True, PLATFORM))
        cited = _cites(result)
        for line in (
            "Payments -[RUNS]-> Billing Service",
            "Billing Service -[DEPENDS_ON]-> Auth Service",
            "Platform -[RUNS]-> Auth Service",
            "Data -[RUNS]-> Search Service",
            "Search Service -[DEPENDS_ON]-> Auth Service",
            # The way back that makes the pair mutual.
            "API Gateway -[DEPENDS_ON]-> Billing Service",
        ):
            self.assertIn(line, cited)
        self.assertTrue(all(step.from_id and step.to_id and step.from_label and step.to_label for step in result.path))
        self.assertEqual(result.grid["mutual"], [["Payments", "Platform"]])
        self.assertEqual([(row["from"], row["to"], row["count"]) for row in result.grid["rows"]], [("Payments", "Platform", 3), ("Data", "Platform", 1), ("Platform", "Payments", 1)])

    def test_what_a_team_needs_from_other_teams(self):
        result = _ask("what does payments need from other teams")
        self.assertEqual((result.tool, result.answer), ("team_dependencies", PAYMENTS_NEEDS))
        self.assertIn("Invoice Worker -[DEPENDS_ON]-> Notification Service", _cites(result))
        self.assertEqual(_ask("what does the data team depend on").answer, "Data depends on Platform for Auth Service (Search Service → Auth Service).")

    def test_the_company_view(self):
        for question in ("cross-team dependencies", "which teams depend on each other", "dependencies between teams"):
            with self.subTest(question=question):
                result = _ask(question)
                self.assertEqual(result.tool, "team_dependencies")
                self.assertEqual(
                    result.answer,
                    "Across teams, 3 pairs of teams are tied by 5 DEPENDS_ON edges, the busiest first. Payments depends on Platform "
                    "through 3 links (Billing Service → Auth Service, Billing Service → Postgres, Invoice Worker → Notification Service); "
                    "Data depends on Platform through 1 (Search Service → Auth Service); Platform depends on Payments through 1 "
                    "(API Gateway → Billing Service). Payments and Platform depend on each other.",
                )
                self.assertEqual(result.grid["teams"], ["Data", "Payments", "Platform"])

    def test_a_team_nothing_depends_on_and_a_team_miss(self):
        self.assertEqual(_ask("which teams depend on data").answer, "No other team depends on what Data runs.")
        # A name that is not a team goes through the shared fallback before the refusal.
        with mock.patch.object(org_health.fallback, "before_refusing", wraps=org_health.fallback.before_refusing) as fallback:
            missed = _ask("which teams depend on the espresso machine")
        fallback.assert_called_once()
        self.assertEqual((missed.tool, missed.found), ("team_dependencies", False))
        self.assertEqual(missed.answer, "Nothing in the graph matches “espresso machine”. Name a team that exists in the graph.")

    def test_the_catalogue_mcp_and_the_chip(self):
        self.assertIn("team_dependencies", mcp_tools.tool_names())
        called = mcp_tools.call_tool("team_dependencies", {"name": "platform"})
        self.assertEqual((called["answer"], called["grid"]["kind"]), (PLATFORM, "team_dependencies"))
        self.assertEqual(catalogue.get_tool("team_dependencies").run({}, ORG).grid["mutual"], [["Payments", "Platform"]])
        self.assertIn("which teams depend on platform", ask_api.examples().questions)
        answer = ask_api.answer_question("which teams depend on platform").to_dict()
        self.assertEqual((answer["tool"], answer["grid"]["kind"]), ("team_dependencies", "team_dependencies"))
        self.assertTrue(answer["nodes"] and answer["edges"])


class TeamDependenciesWithSalesTest(_Graph):
    graph_name = SALES_GRAPH

    @classmethod
    def seed_more(cls):
        quillfern_sales.seed_sales()

    def test_sales_has_no_cross_team_links(self):
        self.assertEqual(_ask("which teams depend on sales").answer, "No other team depends on what Sales runs.")
        self.assertEqual(_ask("what does the sales team depend on").answer, "Sales depends on no other team's things.")
        # Sales leans on its own HubSpot only, so the company view is unchanged.
        self.assertTrue(_ask("cross-team dependencies").answer.startswith("Across teams, 3 pairs of teams are tied by 5 DEPENDS_ON edges"))


class OrgHealthFixturesTest(_Graph):
    graph_name = FIXTURE_GRAPH

    @classmethod
    def seed_more(cls):
        _, graph = falkordb_client.connect()
        graph.query(
            """
            MATCH (lena:Person {id: 'lena'}), (tomas:Person {id: 'tomas'}), (kofi:Person {id: 'kofi'}),
                  (billing:Service {id: 'billing-service'}), (memory:Service {id: 'memory-layer'}), (search:Service {id: 'search-service'}),
                  (invoices:Service {id: 'invoice-worker'}), (tkt102:Ticket {id: 'TKT-102'}), (retries:Decision {id: 'decision-billing-retries'})
            CREATE (queue:Decision {id: 'decision-billing-queue', name: 'Queue renewal webhooks for the invoice worker', decided_at: 1791000000000}),
                   (queue)-[:ABOUT]->(billing), (queue)-[:BASED_ON]->(tkt102), (lena)-[:PROPOSED]->(queue), (tomas)-[:APPROVED]->(queue),
                   (cache:Decision {id: 'decision-private-cache', name: 'Cache search results per notebook', decided_at: 1791100000000, scope: $private}),
                   (cache)-[:ABOUT]->(search), (kofi)-[:PROPOSED]->(cache),
                   (hidden:Decision {id: 'decision-private-retries', name: 'Retry renewals from a queue', decided_at: 1791200000000, scope: $private}),
                   (hidden)-[:SUPERSEDES]->(retries),
                   (:Ticket {id: 'TKT-150', name: 'TKT-150', title: 'Graph store reads spike at noon'})-[:ABOUT]->(memory),
                   (ops:Team {id: 'ops', name: 'Ops'}), (cron:Service {id: 'cron-runner', name: 'Cron Runner'}),
                   (ops)-[:RUNS]->(cron), (cron)-[:DEPENDS_ON]->(invoices)
            """,
            {"private": PRIVATE},
        )

    def test_a_team_reached_only_through_other_things(self):
        answer = _ask("which teams depend on platform").answer
        self.assertIn("Ops reaches Platform only through other things (Cron Runner → Invoice Worker → Notification Service).", answer)
        self.assertIn("Cron Runner -[DEPENDS_ON]-> Invoice Worker", _cites(_ask("which teams depend on platform")))

    def test_flags_for_an_org_reader(self):
        result = _ask("which decisions are stale")
        self.assertEqual(result.tool, "decision_health")
        self.assertEqual(
            result.answer,
            "3 standing decisions, 1 replaced. Needs attention: *Queue renewal webhooks for the invoice worker* and "
            "*Retry renewal webhooks inside the billing service* are both standing decisions about Billing Service; "
            "*Move the memory layer off Postgres*: TKT-150 landed on Memory Layer and it is not cited. "
            "History: *Keep notebook links in Postgres join tables* (1 Jun 2026) was replaced by *Move the memory layer off Postgres* (15 Sep 2026).",
        )
        rows = {row["id"]: row for row in result.grid["rows"]}
        # A private superseder does not hide a standing decision from an org reader.
        self.assertTrue(rows["decision-billing-retries"]["standing"])
        self.assertNotIn("decision-private-cache", rows)
        self.assertEqual(rows["decision-billing-queue"]["conflicts_with"], ["decision-billing-retries"])
        self.assertEqual(rows["decision-memory-off-postgres"]["uncited"], ["TKT-150"])
        self.assertIn("TKT-150 -[ABOUT]-> Memory Layer", _cites(result))

    def test_a_scoped_reader_sees_the_private_decisions(self):
        result = catalogue.get_tool("decision_health").run({}, ORG + [PRIVATE])
        rows = {row["id"]: row for row in result.grid["rows"]}
        self.assertEqual(rows["decision-private-cache"]["flags"], {"no_evidence": True, "no_approver": True, "conflict": False, "may_be_stale": False, "old": False})
        # The private decision that replaces the retries decision now counts.
        self.assertFalse(rows["decision-billing-retries"]["standing"])
        self.assertEqual(rows["decision-billing-retries"]["replaced_by"], "Retry renewals from a queue")
        self.assertIn("*Cache search results per notebook*: cites no evidence and has no approver", result.answer)

    def test_age_and_narrowing(self):
        # A year on, every standing decision is over 180 days old.
        with mock.patch.object(org_health, "now_millis", return_value=1_822_000_000_000):
            rows = catalogue.get_tool("decision_health").run({}, ORG).grid["rows"]
        self.assertTrue(all(row["flags"]["old"] for row in rows if row["standing"]))
        billing = _ask("decision health for billing")
        self.assertTrue(billing.answer.startswith("2 standing decisions about Billing Service, 0 replaced. Needs attention:"), billing.answer)
        team = catalogue.get_tool("decision_health").run({"name": "the data team"}, ORG)
        self.assertTrue(team.answer.startswith("1 standing decision about what Data runs, 1 replaced."), team.answer)
        self.assertIn("decision_health", mcp_tools.tool_names())
        self.assertEqual(mcp_tools.call_tool("decision_health", {})["grid"]["kind"], "decision_health")


class DecisionHealthTest(_Graph):
    def test_the_plain_seed_needs_no_attention(self):
        result = _ask("which decisions are stale")
        self.assertEqual((result.tool, result.found, result.answer), ("decision_health", True, HEALTHY))
        cited = _cites(result)
        for line in (
            "Move the memory layer off Postgres -[ABOUT]-> Memory Layer",
            "Move the memory layer off Postgres -[BASED_ON]-> TKT-101",
            "Tomas Reyes -[APPROVED]-> Move the memory layer off Postgres",
            "Move the memory layer off Postgres -[SUPERSEDES]-> Keep notebook links in Postgres join tables",
        ):
            self.assertIn(line, cited)
        rows = {row["id"]: row for row in result.grid["rows"]}
        self.assertEqual(rows["decision-postgres-join-tables"]["replaced_by"], "Move the memory layer off Postgres")
        self.assertEqual(rows["decision-billing-retries"]["decided_at"], "20 Sep 2026")
        for question in ("are any decisions in conflict", "decision health", "which decisions need attention"):
            with self.subTest(question=question):
                self.assertEqual(_ask(question).answer, HEALTHY)

    def test_the_must_stays(self):
        self.assertEqual(_ask("is the billing retries decision still the last word").tool, "decision_pressure")
        self.assertEqual(_ask("what decisions have we made about billing").tool, "decision_trace")
        self.assertEqual(_ask("what does the invoice worker depend on").tool, "blast_radius")
        self.assertEqual(_ask("what breaks if auth goes down").tool, "blast_radius")


if __name__ == "__main__":
    unittest.main()
