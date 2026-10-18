"""Every public-reachable tool, with synced Jira and CRM data in the graph.

A Jira project is written through ``src/connectors/graph.py`` onto the Quillfern seed: an open
ticket about Billing assigned to Arjun's linked account and opened by an unlinked contractor,
and an account Arjun owns. For each tool, the company-wide reader (the public page, team pages,
MCP) never sees any of it, and a Slack asker, who also reads ``org_internal``, does where the
tool reads tickets, accounts or people.
"""

import json
import unittest
from datetime import UTC, datetime
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.connectors import graph
from src.connectors.base import Container, Edge, ExternalUser, Install, Item, Page
from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import (  # noqa: E402
    ask_api,
    explore,
    find,
    mcp_tools,
    org_health,
    person_profile,
    reading_list,
    risk_map,
    search,
    team_page,
    what_changed,
)
from src.domain.knowledge import decisions as decision_tools  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_connector_tools_test{tests.TEST_GRAPH_SUFFIX}"
PUBLIC = ["org_shared"]
SLACK = ["org_shared", "org_internal"]
INSTALL = Install(source="jira", config={})
SECRETS = ("PAY-501", "Webhook signature drift", "Zed Contractor", "Initech")


def _text(result) -> str:
    """Everything a reader could see of a result: the answer, the cited path, the grid, the memories."""
    return json.dumps(result.to_dict() if hasattr(result, "to_dict") else result, default=str) + getattr(result, "answer", "")


class ConnectorDataInEveryToolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        search.ensure_search_indexes()
        ask_api.ANSWERS.clear()
        project = Container(id=INSTALL.container_id("PAY"), name="Payments project", external_id="PAY", kind="project")
        graph.write_containers(INSTALL, [project])
        arjun = ExternalUser(id=INSTALL.user_id("a1"), source="jira", external_id="a1", name="arjun.m", email="arjun@quillfern.example.com")
        zed = ExternalUser(id=INSTALL.user_id("z9"), source="jira", external_id="z9", name="Zed Contractor", email="zed@contractor.example")
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        ticket = Item(
            label="Ticket", id=INSTALL.item_id("10501"), external_id="10501", container=project.id, name="PAY-501",
            title="Webhook signature drift", summary="Billing webhooks fail signature checks after the key rotation.",
            updated_at=now, about=("billing",),
        )
        account = Item(label="Service", kind="account", id=INSTALL.item_id("company:7"), external_id="company:7", container=project.id, name="Initech")
        graph.write_page(
            INSTALL,
            Page(
                items=[ticket, account],
                users=[arjun, zed],
                edges=[Edge("ASSIGNED", arjun.id, ticket.id), Edge("OPENED", zed.id, ticket.id), Edge("OWNS", arjun.id, account.id)],
            ),
        )

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in reversed(cls._patches):
                patch.stop()

    def assertHidden(self, result, *names):
        text = _text(result)
        for name in names or SECRETS:
            self.assertNotIn(name, text, f"{name} leaked to the company-wide scope")

    # --- the tools ----------------------------------------------------------------------------

    def test_risk_map(self):
        self.assertHidden(risk_map.risk_map(PUBLIC))
        rows = [row["name"] for row in risk_map.risk_map(SLACK).grid["rows"]]
        self.assertIn("Initech", rows)

    def test_reading_list(self):
        self.assertHidden(reading_list.reading_list("what should I read before touching billing", PUBLIC))
        self.assertTrue(reading_list.reading_list("what should I read before touching billing", SLACK).found)

    def test_person_profile(self):
        self.assertHidden(person_profile.person_profile("tell me about Arjun Mehta", PUBLIC))
        inside = person_profile.person_profile("tell me about Arjun Mehta", SLACK)
        self.assertIn("PAY-501", inside.answer)
        self.assertIn("Arjun Mehta -[ASSIGNED]-> PAY-501", [step.cite() for step in inside.path])

    def test_team_dependencies_and_decision_health(self):
        for result in (org_health.team_dependencies(None, PUBLIC), org_health.decision_health(None, PUBLIC)):
            self.assertHidden(result)
        self.assertIn("PAY-501", _text(org_health.decision_health(None, SLACK)))

    def test_decision_pressure(self):
        question = "is the billing retries decision still the last word"
        self.assertHidden(decision_tools.decision_pressure(question, PUBLIC))
        self.assertIn("PAY-501 (about Billing Service)", decision_tools.decision_pressure(question, SLACK).answer)

    def test_what_changed(self):
        self.assertHidden(what_changed.what_changed("what changed around billing", PUBLIC, days=30))
        self.assertIn("PAY-501", what_changed.what_changed("what changed around billing", SLACK, days=30).answer)

    def test_explore_and_neighbors(self):
        self.assertFalse(explore.explore("explore PAY-501", PUBLIC).found)
        self.assertHidden(explore.explore("explore billing", PUBLIC))
        self.assertHidden(explore.neighbors("billing", "ABOUT", PUBLIC))
        ticket = explore.explore("explore PAY-501", SLACK)
        cites = [step.cite() for step in ticket.path]
        self.assertIn("Arjun Mehta -[ASSIGNED]-> PAY-501", cites)
        self.assertIn("Zed Contractor (Jira) -[OPENED]-> PAY-501", cites)
        self.assertIn("PAY-501", _text(explore.neighbors("billing", "ABOUT", SLACK)))

    def test_find(self):
        self.assertHidden(find.find("Ticket", {}, PUBLIC))
        self.assertIn("PAY-501", _text(find.find("Ticket", {}, SLACK)))
        self.assertIn("PAY-501", _text(find.find("Ticket", {"person": "Arjun Mehta"}, SLACK)))

    def test_search_counts_jira_people_apart(self):
        self.assertHidden(search.search("search for webhook signature", PUBLIC))
        inside = search.search("search for webhook signature", SLACK)
        cites = [step.cite() for step in inside.path]
        self.assertIn("Arjun Mehta -[ASSIGNED]-> PAY-501", cites)
        self.assertIn("Zed Contractor (Jira) -[OPENED]-> PAY-501", cites)

    def test_team_page_and_brief(self):
        self.assertHidden(team_page.team_page("payments"))
        self.assertEqual([account.name for account in team_page.team_page("payments", SLACK).accounts], ["Initech"])

    def test_z_uncited_work_is_capped(self):
        # Runs last (it adds tickets): a tracker's backlog never floods one decision's answer.
        project = INSTALL.container_id("PAY")
        backlog = [
            Item(label="Ticket", id=INSTALL.item_id(f"20{number}"), external_id=f"20{number}", container=project, name=f"PAY-6{number:02d}", about=("billing",))
            for number in range(12)
        ]
        graph.write_page(INSTALL, Page(items=backlog))
        answer = decision_tools.decision_pressure("is the billing retries decision still the last word", SLACK).answer
        self.assertRegex(answer, r"and \d+ more\.$")
        self.assertLessEqual(answer.count("PAY-"), decision_tools.MAX_UNCITED)
        health = org_health.decision_health(None, SLACK).answer
        self.assertRegex(health, r"\d+ more landed on Billing Service")

    def test_public_surfaces_end_to_end(self):
        for question in (
            "what are our riskiest services",
            "what should I read before touching billing",
            "tell me about Arjun Mehta",
            "which teams depend on platform",
            "which decisions are stale",
            "what changed around billing this month",
            "explore billing",
            "list open tickets",
            "search for webhook signature",
            "who knows PAY-501",
        ):
            # Everything but the question itself, which a refusal may quote back.
            answer = {key: value for key, value in ask_api.answer_question(question).to_dict().items() if key != "question"}
            self.assertFalse(any(name in json.dumps(answer, default=str) for name in SECRETS), question)
        for tool, arguments in (
            ("risk_map", {}),
            ("find", {"label": "Ticket"}),
            ("search", {"text": "webhook signature"}),
            ("person_profile", {"name": "Arjun Mehta"}),
            ("explore", {"name": "billing"}),
        ):
            payload = mcp_tools.call_tool(tool, arguments)
            self.assertFalse(any(name in json.dumps(payload, default=str) for name in SECRETS), tool)


if __name__ == "__main__":
    unittest.main()
