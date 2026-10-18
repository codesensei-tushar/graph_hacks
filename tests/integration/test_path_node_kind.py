"""Path nodes carry a Service's kind, on Quillfern with its Sales team: the drawing can say what the reply says."""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from fastapi.testclient import TestClient  # noqa: E402

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import ask_api  # noqa: E402
from src.main import app  # noqa: E402
from src.seed import quillfern, quillfern_sales, seed  # noqa: E402

GRAPH = f"company_path_kind_test{tests.TEST_GRAPH_SUFFIX}"


class PathNodeKindTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        quillfern_sales.seed_sales()
        cls.http = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def setUp(self):
        ask_api.ANSWERS.clear()
        ask_api.LIMITER.reset()

    def get(self, path: str, **params) -> dict:
        response = self.http.get(path, params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def kinds(self, answer: dict) -> dict:
        return {node["name"]: (node["label"], node["kind"]) for node in answer["nodes"]}

    def test_a_sales_outage_draws_a_system_a_process_and_a_playbook(self):
        answer = self.get("/api/ask", q="what breaks if hubspot goes down")
        self.assertEqual(answer["tool"], "blast_radius")
        self.assertEqual(
            self.kinds(answer),
            {"HubSpot": ("Service", "system"), "Renewals": ("Service", "process"), "Pricing playbook": ("Service", "playbook")},
        )

    def test_an_engineering_service_reads_as_a_service_and_people_carry_no_kind(self):
        outage = self.get("/api/ask", q="what breaks if auth goes down")
        self.assertEqual(self.kinds(outage)["Auth Service"], ("Service", "service"))
        self.assertEqual({kind for _label, kind in self.kinds(outage).values()}, {"service"})
        owners = self.kinds(self.get("/api/ask", q="who owns billing"))
        self.assertEqual(owners["Billing Service"], ("Service", "service"))
        self.assertEqual(owners["Arjun Mehta"], ("Person", None))

    def test_the_team_page_draws_each_thing_with_its_kind(self):
        page = self.get("/api/team/sales")
        drawn = {node["name"]: (node["label"], node["kind"]) for node in page["nodes"]}
        self.assertEqual(drawn["Renewals"], ("Service", "process"))
        self.assertEqual(drawn["HubSpot"], ("Service", "system"))
        self.assertEqual(drawn["Enterprise accounts"], ("Service", "account"))
        self.assertEqual(drawn["Pricing playbook"], ("Service", "playbook"))
        self.assertEqual(drawn["Sales"], ("Team", None))
        self.assertEqual(drawn["Priya Shah"], ("Person", None))
