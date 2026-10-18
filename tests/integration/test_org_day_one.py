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

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api.v1 import console_teams  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.core import tenancy  # noqa: E402
from src.db import falkordb_client  # noqa: E402
from src.db.falkordb_client import connect  # noqa: E402
from src.domain import orgs  # noqa: E402
from src.domain.memory.writeback import SlackMemoryScope  # noqa: E402
from src.domain.onboarding import team_import  # noqa: E402
from src.domain.slack import compute  # noqa: E402
from src.domain.slack.compute import TurnRequest  # noqa: E402
from src.integrations.slack.events import SlackEventInner  # noqa: E402

ORIGIN = "https://console.test"
CSRF = "csrf-token-of-the-session"
PAYMENTS_FILE = """
[team]
name = "Payments"
summary = "Money in and out."

[[members]]
name = "Paula Ortiz"
email = "paula@dayone.example"

[[members]]
name = "Sam Okafor"

[[runs]]
name = "Billing Service"
kind = "service"
owner = "Paula Ortiz"

[[runs]]
name = "Ledger"
kind = "system"
owner = "Sam Okafor"
depends_on = ["Billing Service"]
"""
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



class ConsoleTeamsTest(unittest.TestCase):
    """``/console/api/teams``: an admin imports a team file into their org; members list the teams."""

    @classmethod
    def setUpClass(cls):
        orgs.ensure_control_indexes()
        cls.x = orgs.create_org("Teams X", created_by="TX:UX")
        cls.y = orgs.create_org("Teams Y", created_by="TY:UY")
        cls.saved = {name: getattr(settings, name) for name in ("console_enabled", "public_url")}
        for name, value in (("console_enabled", True), ("public_url", ORIGIN)):
            object.__setattr__(settings, name, value)
        cls.app = FastAPI()
        cls.app.include_router(console_teams.router)

    @classmethod
    def tearDownClass(cls):
        for name, value in cls.saved.items():
            object.__setattr__(settings, name, value)
        for org in (cls.x, cls.y):
            try:
                falkordb_client.client().select_graph(org.graph).delete()
            except Exception:
                pass
        tenancy.control_graph().query("MATCH (o:Org) WHERE o.id IN $ids DETACH DELETE o", {"ids": [cls.x.id, cls.y.id]})

    def setUp(self):
        self.session = {"org": self.x.id, "person": {"id": "p-ada", "name": "Ada"}, "role": "admin", "csrf": CSRF, "slack_user": "UADA", "team": "TX"}
        self.app.dependency_overrides[console_teams.console_user] = lambda: self.session
        self.app.dependency_overrides[console_teams.console_admin] = self._admin
        self.client = TestClient(self.app)
        self.headers = {"x-csrf-token": CSRF, "origin": ORIGIN}

    def _admin(self):
        if self.session["role"] not in ("owner", "admin"):
            raise HTTPException(status_code=403, detail="admins only")
        return self.session

    def _post(self, body, **kwargs):
        return self.client.post("/console/api/teams/import", json=body, headers=self.headers, **kwargs)

    def test_check_then_import_then_list_in_the_orgs_own_graph(self):
        checked = self._post({"file": PAYMENTS_FILE, "check": True})
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertEqual(
            checked.json(),
            {"mode": "check", "company": self.x.slug, "team": "payments", "contents": "Payments: 2 people, 1 service, 1 system", "counts": {"people": 2, "runs": {"service": 1, "system": 1}, "decisions": 0}},
        )
        self.assertEqual(_rows(self.x.ref(), "MATCH (t:Team) RETURN count(t)"), [[0]], "a check writes nothing")

        merged = self._post({"file": PAYMENTS_FILE})
        self.assertEqual(merged.status_code, 200, merged.text)
        body = merged.json()
        self.assertEqual((body["mode"], body["company"], body["team"]), ("merge", self.x.slug, "payments"))
        self.assertGreater(body["nodes_created"], 0)
        self.assertEqual(_rows(self.x.ref(), "MATCH (:Team {id: 'payments'})-[:PART_OF]->(c:Company) RETURN c.id"), [[self.x.slug]])
        self.assertEqual(self._post({"file": PAYMENTS_FILE}).json()["nodes_created"], 0, "a second import creates nothing")
        audit = tenancy.control_graph().query("MATCH (a) WHERE a.action = 'teams.import' AND a.org = $org RETURN count(a)", {"org": self.x.id}).result_set
        self.assertGreaterEqual(audit[0][0], 1)

        listed = self.client.get("/console/api/teams").json()
        self.assertEqual(
            listed,
            {
                "teams": [
                    {
                        "id": "payments", "name": "Payments", "summary": "Money in and out.",
                        "members": [{"id": "paula-ortiz", "name": "Paula Ortiz", "role": None}, {"id": "sam-okafor", "name": "Sam Okafor", "role": None}],
                        "runs": [
                            {"id": "billing-service", "name": "Billing Service", "kind": "service", "owners": [{"id": "paula-ortiz", "name": "Paula Ortiz"}]},
                            {"id": "ledger", "name": "Ledger", "kind": "system", "owners": [{"id": "sam-okafor", "name": "Sam Okafor"}]},
                        ],
                    }
                ]
            },
        )
        self.session = {**self.session, "org": self.y.id}
        self.assertEqual(self.client.get("/console/api/teams").json(), {"teams": []}, "another org sees none of it")
        self.assertEqual(_rows(self.y.ref(), "MATCH (t:Team) RETURN count(t)"), [[0]])

    def test_a_refused_file_writes_nothing_and_says_why(self):
        bad = PAYMENTS_FILE.replace('owner = "Sam Okafor"', 'owner = "Nobody Here"')
        response = self._post({"file": bad})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "refused")
        self.assertTrue(any("Nobody Here" in problem for problem in response.json()["problems"]), response.json())
        self.assertEqual(_rows(self.y.ref(), "MATCH (t:Team) RETURN count(t)"), [[0]])

    def test_only_admins_import_and_the_body_is_checked(self):
        self.assertEqual(self._post({"file": ""}).status_code, 400)
        self.assertEqual(self._post({"file": PAYMENTS_FILE, "check": "yes"}).status_code, 400)
        self.assertEqual(self.client.post("/console/api/teams/import", content=PAYMENTS_FILE, headers={**self.headers, "content-type": "text/plain"}).status_code, 415)
        huge = self._post({"file": "#" * (team_import.MAX_FILE_BYTES + 10)})
        self.assertEqual((huge.status_code, huge.json()["error"]), (413, "too_large"))
        self.session = {**self.session, "role": "member"}
        self.assertEqual(self._post({"file": PAYMENTS_FILE}).status_code, 403)
        self.assertEqual(self.client.get("/console/api/teams").status_code, 200, "members may list")


if __name__ == "__main__":
    unittest.main()
