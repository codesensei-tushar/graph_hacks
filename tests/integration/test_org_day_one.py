"""Day one of a company that signed up through the console: its own org and graph, nothing seeded.

The org is made with ``orgs.create_org`` (as the console's sign-up does), so its graph starts
with only its Company node. ``onboard team:`` from Slack then runs in that org, as a Slack turn
routed to it does, and the team joins the org's Company, never the legacy one. An org made
before create_org wrote the Company node gets it from the startup migration, and until then a
Slack user is told to finish setup in the console, never about a setting.
Needs FalkorDB on localhost:6379 (``docker compose up -d``).
"""

import re
import unittest
from types import SimpleNamespace

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.core import tenancy  # noqa: E402
from src.db import falkordb_client  # noqa: E402
from src.db.falkordb_client import connect  # noqa: E402
from src.domain import orgs  # noqa: E402
from src.domain.memory.writeback import SlackMemoryScope  # noqa: E402
from src.domain.onboarding import team_import  # noqa: E402
from src.domain.slack import compute  # noqa: E402
from src.domain.slack.compute import TurnRequest  # noqa: E402
from src.integrations.slack.events import SlackEventInner  # noqa: E402

PAYMENTS = "<@UBOT> onboard team: Payments | members: <@UPAULA> <@USAM> | owns: Billing Service (service), Ledger (system, <@USAM>)"
PROFILES = {
    "UPAULA": SimpleNamespace(id="UPAULA", real_name="Paula Ortiz", display_name="", name="paula", email="paula@dayone.example"),
    "USAM": SimpleNamespace(id="USAM", real_name="Sam Okafor", display_name="", name="sam", email="sam@dayone.example"),
}
WORKSPACE = SimpleNamespace(team_id="TDAYONE", bot_user_id="UBOT", client=SimpleNamespace(user_info=PROFILES.get))


def _request(raw: str, asker: str = "UPAULA", channel: str = "CPAY") -> TurnRequest:
    scope = SlackMemoryScope(kind="shared", channel_id=channel, channel_type="channel", slack_user_id=asker)
    event = SlackEventInner(type="app_mention", user=asker, channel=channel, channel_type="channel", text=raw, ts="1790000000.000100")
    question = " ".join(re.sub(r"<@[A-Z0-9]+(?:\|[^>]*)?>", "", raw).split())
    return TurnRequest(
        workspace=WORKSPACE, event=event, question=question, raw_text=raw, asker_slack_user_id=asker, asker_name="Paula",
        memory_scope=scope, read_tags=["org_shared"], thread=[], channel=channel, thread_ts=event.ts, is_dm=False,
    )


def _rows(org, query: str, **params) -> list:
    with tenancy.org_context(org):
        _, graph = connect()
        return graph.query(query, params).result_set


class OrgDayOneTest(unittest.TestCase):
    created: list = []

    @classmethod
    def setUpClass(cls):
        orgs.ensure_control_indexes()

    @classmethod
    def tearDownClass(cls):
        for org in cls.created:
            try:
                falkordb_client.client().select_graph(org.graph).delete()
            except Exception:
                pass
        tenancy.control_graph().query("MATCH (o:Org) WHERE o.id IN $ids DETACH DELETE o", {"ids": [org.id for org in cls.created]})

    def _org(self, name: str):
        org = orgs.create_org(name, created_by="TDAYONE:UPAULA")
        self.created.append(org)
        return org

    def _legacy_companies(self) -> int:
        with tenancy.org_context(tenancy.legacy_org()):
            _, graph = connect()
            return graph.query("MATCH (c:Company) RETURN count(c)").result_set[0][0]

    def test_a_new_org_starts_with_its_own_company(self):
        org = self._org("Day One Payments")
        self.assertEqual(_rows(org.ref(), "MATCH (c:Company) RETURN c.id, c.name"), [[org.slug, "Day One Payments"]])
        with tenancy.org_context(org.ref()):
            self.assertEqual(team_import.company_id(), org.slug)
        with tenancy.org_context(tenancy.legacy_org()):
            self.assertEqual(team_import.company_id(), team_import.default_company_id())

    def test_onboard_team_from_slack_joins_the_orgs_company(self):
        org = self._org("Day One Onboard")
        legacy_before = self._legacy_companies()
        with tenancy.org_context(org.ref()):
            outcome = compute.compute_turn(_request(PAYMENTS))
            self.assertEqual(outcome.status, "suspended", outcome.reply)
            approval = outcome.approval
            text = compute.execute_approved_action(approval.tool_name, approval.tool_input, approval.container_tag)
        self.assertIn("Onboarded *Payments*", text)
        self.assertEqual(_rows(org.ref(), "MATCH (t:Team {id: 'payments'})-[:PART_OF]->(c:Company) RETURN c.id"), [[org.slug]])
        self.assertEqual(
            sorted(row[0] for row in _rows(org.ref(), "MATCH (:Team {id: 'payments'})-[:RUNS]->(s:Service) RETURN s.name")),
            ["Billing Service", "Ledger"],
        )
        self.assertEqual(_rows(org.ref(), "MATCH (c:Company) RETURN count(c)"), [[1]], "no second, nameless company")
        self.assertEqual(self._legacy_companies(), legacy_before, "nothing reaches the legacy graph")

    def test_an_org_without_its_company_is_told_to_finish_setup_then_the_migration_fixes_it(self):
        org = self._org("Day One Legacy Shape")
        _rows(org.ref(), "MATCH (c:Company) DETACH DELETE c RETURN count(*)")  # an org made before create_org wrote one
        with tenancy.org_context(org.ref()):
            outcome = compute.compute_turn(_request(PAYMENTS))
        self.assertEqual(outcome.reply, team_import.NO_COMPANY)
        self.assertNotIn("SEED_COMPANY", outcome.reply)
        with tenancy.org_context(org.ref()):
            written = compute.execute_approved_action("onboard_team", {"team": "Payments", "members": ["<@UPAULA>"], "profiles": {"UPAULA": "Paula Ortiz"}}, "org_shared")
        self.assertTrue(written.startswith(team_import.NO_COMPANY), written)
        self.assertNotIn("SEED_COMPANY", written)

        for _ in range(2):  # startup runs it every boot: idempotent
            self.assertEqual(orgs.ensure_company(org.ref()), org.slug)
        self.assertEqual(_rows(org.ref(), "MATCH (c:Company) RETURN c.id, c.name"), [[org.slug, "Day One Legacy Shape"]])
        self.assertIsNone(orgs.ensure_company(tenancy.legacy_org()), "the legacy org's company is the seed's")
        with tenancy.org_context(org.ref()):
            self.assertEqual(compute.compute_turn(_request(PAYMENTS)).status, "suspended")


if __name__ == "__main__":
    unittest.main()
