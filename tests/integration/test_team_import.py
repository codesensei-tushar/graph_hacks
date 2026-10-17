"""Importing a team file into a live graph: merge-only, checked whole before any write.

Seeds Quillfern into a throwaway graph and imports its Sales team from
examples/teams/quillfern-sales.toml, through ``import_team`` and through ``POST /ops/import``.
Needs FalkorDB on localhost:6379 (``docker compose up -d``).
"""

import dataclasses
import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api import ops  # noqa: E402
from src.api.v1 import slack_install  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import algorithms, ask_service, team_page  # noqa: E402
from src.domain.onboarding.team_import import MAX_FILE_BYTES, TeamFileError, import_team  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402
from src.seed.quillfern_sales import SALES_FILE, seed_sales  # noqa: E402

GRAPH = f"company_team_import_test{tests.TEST_GRAPH_SUFFIX}"
TOKEN = "import-test-admin-token"
SMALL = b"""
[team]
name = "Support"

[[members]]
name = "Alex Kim"

[[runs]]
name = "Ticket triage"
kind = "process"
owner = "Alex Kim"
"""


def _counts() -> tuple[int, int]:
    _, graph = falkordb_client.connect()
    nodes = graph.query("MATCH (n) RETURN count(n)").result_set[0][0]
    edges = graph.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]
    return int(nodes), int(edges)


class TeamImportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def setUp(self):
        seed.reset_and_seed(quillfern)

    def test_importing_twice_writes_nothing_the_second_time(self):
        first = seed_sales().report
        self.assertEqual((first.nodes_created, first.edges_created), (13, 32))
        again = seed_sales().report
        self.assertEqual((again.nodes_created, again.edges_created, again.properties_set), (0, 0, 0))
        self.assertEqual(again.nodes, first.nodes)

    def test_the_team_page_shows_what_the_file_wrote(self):
        seed_sales()
        page = team_page.team_page("sales").to_dict()
        self.assertEqual(page["summary"], "Renewals, pricing and the enterprise accounts that pay for Quillfern.")
        self.assertEqual(page["company"], "Quillfern")
        self.assertEqual({m["name"]: m["role"] for m in page["members"]}, {"Priya Shah": "Head of sales", "Dev Patel": "Account manager", "Noor Haddad": "Sales operations"})
        things = {item["name"]: (group["kind"], item["owners"]) for group in page["owns"] for item in group["items"]}
        self.assertEqual(things["Renewals"], ("process", ["Priya Shah"]))
        self.assertEqual(things["HubSpot"], ("system", ["Noor Haddad"]))
        current = page["decisions"][0]
        self.assertEqual((current["name"], current["decided_on"]), ("Sell annual plans only at renewal", "1 Sep 2026"))
        self.assertEqual((current["replaced"], current["replaced_on"]), ("Discount monthly plans at renewal", "2 Mar 2026"))

    def test_tomas_bridges_the_teams_without_merging_their_work_clusters(self):
        seed_sales()
        knows = ask_service.ask("who knows the pricing playbook", ["org_shared"])
        self.assertIn("Ask first: Priya Shah (owns it), then Noor Haddad (worked on it) and Tomas Reyes (worked on it).", knows.answer)
        # Label propagation is deterministic, so the clusters pin: Sales stays its own, Tomas stays with Billing.
        groups = algorithms.working_groups(["org_shared"]).answer
        self.assertIn("finds 3 work clusters. Arjun Mehta, Lena Fischer and Tomas Reyes.", groups)
        self.assertIn("Dev Patel, Noor Haddad and Priya Shah. That matches the org chart: Dev Patel, Noor Haddad and Priya Shah are on Sales.", groups)

    def test_a_refused_file_writes_nothing(self):
        before = _counts()
        broken = SALES_FILE.read_text().replace('kind = "system"', 'kind = "crm"')
        with self.assertRaises(TeamFileError) as caught:
            import_team(broken, quillfern.COMPANY["id"])
        self.assertEqual(caught.exception.problems, ["kind in [[runs]] 'HubSpot' is 'crm'; use one of service, system, process, product, account, policy, playbook"])
        self.assertEqual(_counts(), before)

    def test_a_reference_can_name_something_already_in_the_graph(self):
        text = SMALL + b'depends_on = ["Billing Service"]\nworked_on_by = ["Tomas Reyes"]\n'
        import_team(text, quillfern.COMPANY["id"])
        _, graph = falkordb_client.connect()
        rows = graph.query("MATCH (:Service {id: 'ticket-triage'})-[:DEPENDS_ON]->(s:Service) RETURN s.id").result_set
        self.assertEqual(rows, [["billing-service"]])
        worked = graph.query("MATCH (:Person {id: 'tomas'})-[:WORKED_ON]->(s:Service {id: 'ticket-triage'}) RETURN count(s)").result_set[0][0]
        self.assertEqual(worked, 1)

    def test_a_file_cannot_take_over_another_teams_service(self):
        before = _counts()
        text = SMALL.replace(b'name = "Ticket triage"', b'name = "Billing Service"')
        with self.assertRaises(TeamFileError) as caught:
            import_team(text, quillfern.COMPANY["id"])
        self.assertIn("'billing-service' is already run by Payments", caught.exception.problems[0])
        self.assertEqual(_counts(), before)

    def test_ops_import_checks_then_merges_behind_the_admin_token(self):
        app = FastAPI()
        ops.install(app)
        client = TestClient(app)
        body = SALES_FILE.read_bytes()
        unset = dataclasses.replace(settings, brain_admin_token="", seed_company="quillfern")
        with mock.patch.object(slack_install, "settings", unset), mock.patch.object(ops, "settings", unset):
            self.assertEqual(client.post("/ops/import", content=body).status_code, 503)
        armed = dataclasses.replace(settings, brain_admin_token=TOKEN, seed_company="quillfern")
        auth = {"Authorization": f"Bearer {TOKEN}"}
        with mock.patch.object(slack_install, "settings", armed), mock.patch.object(ops, "settings", armed):
            self.assertEqual(client.post("/ops/import", content=body).status_code, 403)
            self.assertEqual(client.post("/ops/import", content=body, headers={"Authorization": "Bearer nope"}).status_code, 403)

            before = _counts()
            refused = client.post("/ops/import", content=body.replace(b"owner =", b"ownr ="), headers=auth)
            self.assertEqual(refused.status_code, 400)
            self.assertEqual(refused.json()["error"], "refused")
            self.assertIn("unknown field 'ownr' in [[runs]] 'Renewals'; did you mean 'owner'?", refused.json()["problems"])
            too_big = client.post("/ops/import", content=b"#" * (MAX_FILE_BYTES + 1), headers=auth)
            self.assertEqual(too_big.status_code, 413)
            checked = client.post("/ops/import?check=true", content=body, headers=auth)
            self.assertEqual(checked.status_code, 200)
            self.assertEqual(checked.json()["contents"], "Sales: 3 people, 1 system, 1 process, 1 account, 1 playbook, 2 decisions")
            self.assertEqual(_counts(), before, "a refused, oversized or checked file writes nothing")

            imported = client.post("/ops/import", content=body, headers=auth)
            self.assertEqual(imported.status_code, 200)
            self.assertEqual(imported.json()["mode"], "merge")
            self.assertEqual((imported.json()["team"], imported.json()["nodes_created"]), ("sales", 13))
            self.assertEqual(client.post("/ops/import", content=body, headers=auth).json()["nodes_created"], 0)


if __name__ == "__main__":
    unittest.main()
