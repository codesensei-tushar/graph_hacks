"""People insights on a real FalkorDB: people_ranking and go_to on plain Quillfern, and with
GitHub pull requests for the reviews, the open work and the dated touches the seed does not
hold; plus the team filter on risk_map and find's owner none and limit.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import ask_api, ask_service, catalogue, mcp_tools, people_insights  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_people_insights_test{tests.TEST_GRAPH_SUFFIX}"
PR_GRAPH = f"company_people_insights_pr_test{tests.TEST_GRAPH_SUFFIX}"
ORG = ["org_shared"]
PRIVATE_REPO = "github_repo_9"

HOLDS = (
    "Holds the most: Sara Lindqvist (4: owns Graph Store and Postgres, knows Auth Service and Billing Service), "
    "then Lena Fischer (3) and Maya Ortiz (3)."
)
POSTGRES_NOW = (
    "Go-to for Postgres: Sara Lindqvist (owns it; no dated activity on it). Most recent dated touch: 8 Sep 2026 by "
    "Kofi Mensah (said “TKT-101 timings: linked-note lookups five links…”)."
)
BILLING_LAST = (
    "Last touched 20 Sep 2026 by Arjun Mehta (said “billing now retries a failed renewal webhook on…”, proposed "
    "*Retry renewal webhooks inside the billing service*) and Tomas Reyes (approved it). Go-to for Billing Service: "
    "Arjun Mehta (owns it, wrote the Billing runbook; last 20 Sep 2026), Lena Fischer (worked on it and TKT-102; no "
    "dated activity on it), Tomas Reyes (worked on it; last 20 Sep 2026) and 1 more."
)
# A year after the seed's dates, every touch is past STALE_DAYS.
A_YEAR_ON = 1_822_000_000_000


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


class PeopleRankingTest(_Graph):
    def test_who_holds_the_most(self):
        result = _ask("who knows the most")
        self.assertEqual((result.tool, result.found, result.answer), ("people_ranking", True, HOLDS))
        cited = _cites(result)
        self.assertEqual(
            cited[:4],
            ["Sara Lindqvist -[OWNS]-> Graph Store", "Sara Lindqvist -[OWNS]-> Postgres", "Sara Lindqvist -[KNOWS]-> Auth Service", "Sara Lindqvist -[KNOWS]-> Billing Service"],
        )
        # One edge for each runner-up.
        self.assertIn("Lena Fischer -[OWNS]-> Invoice Worker", cited)
        self.assertIn("Maya Ortiz -[OWNS]-> API Gateway", cited)
        self.assertTrue(all(step.from_id and step.to_id and step.from_label and step.to_label for step in result.path))

    def test_the_grid_has_every_measure_for_everyone(self):
        grid = _ask("who knows the most").grid
        self.assertEqual((grid["kind"], grid["measure"]), ("people_ranking", "holds"))
        self.assertEqual([column["key"] for column in grid["columns"]], list(people_insights.MEASURES))
        self.assertEqual([column["key"] for column in grid["columns"] if column.get("hot")], ["holds"])
        rows = {row["id"]: row for row in grid["rows"]}
        self.assertEqual(
            rows["sara"],
            {"id": "sara", "name": "Sara Lindqvist", "team": "Platform", "holds": 4, "owns": 2, "reviews": 0, "authored": 0, "decisions": 1, "load": 2, "bridges": 2},
        )
        self.assertEqual(rows["tomas"]["decisions"], 3)
        self.assertEqual(grid["rows"][0]["id"], "sara")

    def test_each_measure_from_its_question(self):
        for question, answer in (
            ("who owns the most", "Owns the most: Kofi Mensah, Lena Fischer, Maya Ortiz and Sara Lindqvist tie at 2; Kofi Mensah (owns Memory Layer and Search Service)."),
            ("who is the busiest person", "Carries the most right now: Kofi Mensah, Lena Fischer and Maya Ortiz tie at 3; Kofi Mensah (owns Memory Layer and Search Service, works on TKT-101)."),
            ("top contributors", "Wrote the most: Arjun Mehta, Kofi Mensah, Lena Fischer and Maya Ortiz tie at 1; Arjun Mehta (wrote the Billing runbook)."),
            (
                "who approved the most decisions",
                "On the most decisions: Tomas Reyes (3: approved *Keep notebook links in Postgres join tables*, *Move the memory layer off Postgres* "
                "and *Retry renewal webhooks inside the billing service*), then Arjun Mehta (1) and Kofi Mensah (1).",
            ),
            (
                "who bridges the most teams",
                "Bridges the most teams: Sara Lindqvist (2 other teams: Data through Graph Store and Payments through Billing Service), "
                "then Arjun Mehta (1) and Lena Fischer (1).",
            ),
            ("who reviewed the most code", "No pull request reviews are in the graph you can read yet."),
        ):
            with self.subTest(question=question):
                result = _ask(question)
                self.assertEqual((result.tool, result.answer), ("people_ranking", answer))

    def test_bridges_cite_the_other_teams_runs(self):
        cited = _cites(_ask("who bridges the most teams"))
        for line in ("Sara Lindqvist -[OWNS]-> Graph Store", "Data -[RUNS]-> Graph Store", "Sara Lindqvist -[KNOWS]-> Billing Service", "Payments -[RUNS]-> Billing Service"):
            self.assertIn(line, cited)

    def test_a_team_narrows_the_field(self):
        result = _ask("who owns the most in the platform team")
        self.assertEqual(result.answer, "Owns the most on Platform: Maya Ortiz and Sara Lindqvist tie at 2; Maya Ortiz (owns API Gateway and Auth Service).")
        self.assertEqual({row["team"] for row in result.grid["rows"]}, {"Platform"})
        self.assertEqual(_ask("who reviewed the most on payments").answer, "No pull request reviews are on Payments in the graph you can read yet.")
        # A team that is not one keeps everyone.
        self.assertEqual(catalogue.get_tool("people_ranking").run({"measure": "holds", "team": "the espresso machine"}, ORG).answer, HOLDS)

    def test_limit_and_an_unknown_measure(self):
        tool = catalogue.get_tool("people_ranking")
        one = tool.run({"measure": "decisions", "team": "payments", "limit": 1}, ORG)
        self.assertEqual(one.answer, "On the most decisions on Payments: Arjun Mehta (1: proposed *Retry renewal webhooks inside the billing service*).")
        self.assertEqual(_cites(one), ["Arjun Mehta -[PROPOSED]-> Retry renewal webhooks inside the billing service"])
        # A tie for the lead is never cut by the limit.
        self.assertTrue(tool.run({"measure": "owns", "limit": 1}, ORG).answer.startswith("Owns the most: Kofi Mensah, Lena Fischer, Maya Ortiz and Sara Lindqvist tie at 2;"))
        self.assertEqual(tool.run({"measure": "bogus", "limit": "lots"}, ORG).answer, HOLDS)

    def test_the_catalogue_mcp_and_the_price(self):
        self.assertIn("people_ranking", mcp_tools.tool_names())
        called = mcp_tools.call_tool("people_ranking", {"measure": "holds"})
        self.assertEqual((called["answer"], called["grid"]["kind"]), (HOLDS, "people_ranking"))
        schema = catalogue.get_tool("people_ranking").args_schema
        self.assertEqual((schema["required"], schema["properties"]["measure"]["enum"]), (["measure"], list(people_insights.MEASURES)))
        # A ranking reads every person on every measure: it costs what the risk map costs.
        self.assertEqual(ask_api.tool_cost("people_ranking"), ask_api.HEAVY_COST)
        self.assertEqual(ask_api.question_cost("who reviewed the most code"), ask_api.HEAVY_COST)
        self.assertEqual(ask_api.tool_cost("go_to"), 1)


class GoToTest(_Graph):
    def test_the_go_to_right_now_leads_with_the_ranking(self):
        result = _ask("who is the go-to for postgres right now")
        self.assertEqual((result.tool, result.found, result.answer), ("go_to", True, POSTGRES_NOW))
        cited = _cites(result)
        self.assertEqual(cited[0], "Sara Lindqvist -[OWNS]-> Postgres")
        self.assertIn("Kofi Mensah -[SAID]-> TKT-101 timings: linked-note lookups five links…", cited)
        self.assertIn("TKT-101 timings: linked-note lookups five links… -[MENTIONS]-> Postgres", cited)
        self.assertEqual([(memory.author, memory.learned_at) for memory in result.memories], [("Kofi Mensah", "2026-09-08")])

    def test_who_last_worked_on_leads_with_the_date(self):
        result = _ask("who last worked on billing")
        self.assertEqual((result.tool, result.answer), ("go_to", BILLING_LAST))
        cited = _cites(result)
        for line in (
            "Arjun Mehta -[OWNS]-> Billing Service",
            "Arjun Mehta -[PROPOSED]-> Retry renewal webhooks inside the billing service",
            "Retry renewal webhooks inside the billing service -[ABOUT]-> Billing Service",
            "Tomas Reyes -[APPROVED]-> Retry renewal webhooks inside the billing service",
        ):
            self.assertIn(line, cited)
        self.assertEqual(
            _ask("when did we last touch the auth service").answer,
            "Last touched 2 Oct 2026 by Maya Ortiz (said “auth session refresh rotates tokens every hour…”). Go-to for Auth Service: "
            "Maya Ortiz (owns it, worked on TKT-103, wrote the Auth session design; last 2 Oct 2026), Arjun Mehta (knows it; no dated "
            "activity on it) and Sara Lindqvist (knows it; no dated activity on it).",
        )

    def test_the_order_is_who_knows_order(self):
        go = _ask("who last worked on billing")
        knows = _ask("who knows billing")
        self.assertIn("Ask first: Arjun Mehta (owns it, wrote the Billing runbook), then Lena Fischer (worked on it and TKT-102), Tomas Reyes", knows.answer)
        self.assertIn("Go-to for Billing Service: Arjun Mehta (owns it, wrote the Billing runbook; last 20 Sep 2026), Lena Fischer (worked on it and TKT-102;", go.answer)

    def test_stale_after_ninety_days(self):
        with mock.patch.object(people_insights, "now_millis", return_value=A_YEAR_ON):
            billing = catalogue.get_tool("go_to").run({"name": "who last worked on billing"}, ORG)
        self.assertTrue(billing.answer.startswith("Last touched 20 Sep 2026, over 90 days ago, by Arjun Mehta"), billing.answer)
        self.assertIn("Arjun Mehta (owns it, wrote the Billing runbook; last 20 Sep 2026, stale)", billing.answer)

    def test_a_ticket_and_a_miss(self):
        ticket = catalogue.get_tool("go_to").run({"name": "TKT-102"}, ORG)
        self.assertTrue(ticket.answer.startswith("Last touched 20 Sep 2026 by Arjun Mehta (said “billing now retries a failed renewal webhook on…”). Go-to for TKT-102:"), ticket.answer)
        # The miss goes through the shared fallback, and "go-to" is no part of the name quoted.
        with mock.patch.object(people_insights.fallback, "before_refusing", wraps=people_insights.fallback.before_refusing) as fallback:
            missed = _ask("who is the go-to for the espresso machine")
        fallback.assert_called_once()
        self.assertEqual((missed.tool, missed.found), ("go_to", False))
        self.assertEqual(missed.answer, "Nothing in the graph matches “espresso machine”. Name a service, document, or ticket that exists in the graph.")

    def test_the_catalogue_mcp_and_the_chip(self):
        self.assertIn("go_to", mcp_tools.tool_names())
        self.assertEqual(mcp_tools.call_tool("go_to", {"name": "who is the go-to for postgres right now"})["answer"], POSTGRES_NOW)
        self.assertIn("who is the go-to for postgres right now", ask_api.examples().questions)
        answer = ask_api.answer_question("who is the go-to for postgres right now").to_dict()
        self.assertEqual((answer["tool"], answer["answer"]), ("go_to", POSTGRES_NOW))
        self.assertTrue(answer["nodes"] and answer["edges"])


class WithPullRequestsTest(_Graph):
    graph_name = PR_GRAPH

    @classmethod
    def seed_more(cls):
        _, graph = falkordb_client.connect()
        graph.query(
            """
            MATCH (sara:Person {id: 'sara'}), (maya:Person {id: 'maya'}), (postgres:Service {id: 'postgres'})
            CREATE (gs:GitHubUser {id: 'gh-sara', login: 'sara-l', name: 'sara-l'})-[:IS]->(sara),
                   (gm:GitHubUser {id: 'gh-maya', login: 'maya-o', name: 'maya-o'})-[:IS]->(maya),
                   (pool:PullRequest {id: 'pr-notebook-60', name: 'notebook#60', title: 'Tune the Postgres pool', state: 'closed', merged: true,
                                      scope: 'org_shared', updated_at: '2026-10-10T09:00:00Z'}),
                   (replica:PullRequest {id: 'pr-notebook-61', name: 'notebook#61', title: 'Add a Postgres read replica', state: 'open', merged: false,
                                         scope: 'org_shared', updated_at: '2026-10-12T09:00:00Z'}),
                   (hidden:PullRequest {id: 'pr-vault-7', name: 'vault#7', title: 'Rotate the Postgres password', state: 'closed', merged: true,
                                        scope: $private, updated_at: '2026-10-16T09:00:00Z'}),
                   (gm)-[:AUTHORED]->(pool), (gs)-[:REVIEWED]->(pool), (pool)-[:TOUCHES]->(postgres),
                   (gs)-[:AUTHORED]->(replica), (gm)-[:REVIEW_REQUESTED]->(replica), (replica)-[:TOUCHES]->(postgres),
                   (gm)-[:REVIEWED]->(hidden), (hidden)-[:TOUCHES]->(postgres)
            """,
            {"private": PRIVATE_REPO},
        )

    def test_reviews_count_through_the_github_account(self):
        result = _ask("who reviewed the most code")
        self.assertEqual(result.answer, "Reviewed the most pull requests: Sara Lindqvist (1: reviewed notebook#60).")
        self.assertEqual(_cites(result), ["Sara Lindqvist -[REVIEWED]-> notebook#60"])
        # A reader of the private repo also sees Maya's review there.
        both = catalogue.get_tool("people_ranking").run({"measure": "reviews"}, ORG + [PRIVATE_REPO])
        self.assertEqual(both.answer, "Reviewed the most pull requests: Maya Ortiz and Sara Lindqvist tie at 1; Maya Ortiz (reviewed vault#7).")

    def test_open_pull_requests_are_load_and_merged_ones_are_not(self):
        self.assertEqual(
            _ask("who is the busiest person").answer,
            "Carries the most right now: Maya Ortiz (4: owns API Gateway and Auth Service, works on TKT-103, asked to review notebook#61), "
            "then Kofi Mensah (3) and Lena Fischer (3).",
        )
        self.assertEqual(
            _ask("top authors").answer,
            "Wrote the most: Maya Ortiz (2: wrote the Auth session design and notebook#60), then Arjun Mehta (1) and Kofi Mensah (1).",
        )

    def test_pull_requests_date_the_go_to(self):
        result = _ask("who is the go-to for postgres right now")
        self.assertEqual(
            result.answer,
            "Go-to for Postgres: Sara Lindqvist (owns it, reviewed notebook#60, wrote notebook#61; last 12 Oct 2026) and Maya Ortiz "
            "(wrote notebook#60; last 10 Oct 2026). Most recent dated touch: 12 Oct 2026 by Sara Lindqvist (wrote notebook#61).",
        )
        for line in ("Sara Lindqvist -[AUTHORED]-> notebook#61", "notebook#61 -[TOUCHES]-> Postgres"):
            self.assertIn(line, _cites(result))
        self.assertNotIn("vault#7", result.answer)
        private = catalogue.get_tool("go_to").run({"name": "when did we last touch postgres"}, ORG + [PRIVATE_REPO])
        self.assertTrue(private.answer.startswith("Last touched 16 Oct 2026 by Maya Ortiz (reviewed vault#7)."), private.answer)
        self.assertIn("vault#7 -[TOUCHES]-> Postgres", _cites(private))


class RiskAndFindTest(_Graph):
    def test_risk_map_for_one_team(self):
        result = _ask("what are the biggest risks for the payments team")
        self.assertEqual(
            (result.tool, result.answer),
            ("risk_map", "Risk in Payments sits with 1 of 2 things, ranked by how much depends on each and how many gaps it has. Invoice Worker (PageRank 0.02): only Lena Fischer knows it."),
        )
        self.assertEqual([row["id"] for row in result.grid["rows"]], ["invoice-worker", "billing-service"])
        self.assertEqual(_cites(result), ["Lena Fischer -[OWNS]-> Invoice Worker", "Payments -[RUNS]-> Invoice Worker"])
        # No team, or a name that is no team, keeps the company view.
        company = _ask("what are the biggest risks").answer
        self.assertTrue(company.startswith("Risk sits with 6 of 9 things"), company)
        self.assertEqual(catalogue.get_tool("risk_map").run({"team": "the espresso machine"}, ORG).answer, company)
        self.assertEqual(mcp_tools.call_tool("risk_map", {"team": "payments"})["grid"]["rows"][0]["id"], "invoice-worker")

    def test_the_newest_decision_and_the_newest_few(self):
        newest = _ask("what is the newest decision")
        self.assertEqual((newest.tool, newest.answer), ("find", "2 standing decisions: *Retry renewal webhooks inside the billing service* (20 Sep 2026) and 1 more."))
        self.assertEqual(
            _ask("show me the latest decisions").answer,
            "2 standing decisions: *Retry renewal webhooks inside the billing service* (20 Sep 2026) and *Move the memory layer off Postgres* (15 Sep 2026).",
        )
        self.assertEqual(catalogue.get_tool("find").run({"label": "Decision", "limit": 1}, ORG).answer, "3 decisions: *Retry renewal webhooks inside the billing service* (20 Sep 2026) and 2 more.")
        # About something, it is that thing's decision.
        self.assertEqual(_ask("what was the latest decision about billing").tool, "decision_trace")

    def test_services_nobody_owns(self):
        self.assertEqual((_ask("which services have no owner").answer), "No unowned services.")
        _, graph = falkordb_client.connect()
        graph.query("MATCH (p:Team {id: 'platform'}) CREATE (p)-[:RUNS]->(:Service {id: 'cron-runner', name: 'Cron Runner'})")
        try:
            for owner in ("none", "nobody", "no one"):
                with self.subTest(owner=owner):
                    result = catalogue.get_tool("find").run({"label": "Service", "owner": owner}, ORG)
                    self.assertEqual(result.answer, "1 unowned service: Cron Runner.")
            self.assertEqual(_ask("what services are unowned").answer, "1 unowned service: Cron Runner.")
            # Owned by a person still resolves the person.
            self.assertEqual(catalogue.get_tool("find").run({"label": "Service", "owner": "sara"}, ORG).answer, "2 services owned by Sara Lindqvist: Graph Store and Postgres.")
        finally:
            graph.query("MATCH (s:Service {id: 'cron-runner'}) DETACH DELETE s")


if __name__ == "__main__":
    unittest.main()
