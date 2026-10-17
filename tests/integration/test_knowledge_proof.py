"""The proof report on the Quillfern graph: what collapses without FalkorDB, and the route in front of it."""

import dataclasses
import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from fastapi.testclient import TestClient  # noqa: E402

from src.api.v1 import proof as proof_route  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import proof  # noqa: E402
from src.main import app  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_proof_test{tests.TEST_GRAPH_SUFFIX}"


class ProofTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        cls.report = proof.run_proof()

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def test_every_capability_but_the_refusal_collapses_without_the_graph(self):
        report = self.report
        self.assertEqual(report.company, "quillfern")
        self.assertEqual(report.total, 6)
        self.assertEqual(report.collapsed, report.total - 1)
        self.assertEqual([row.tool for row in report.rows], ["who_owns", "who_knows", "decision_trace", "blast_radius", "path_between", "who_knows"])
        for row in report.rows[:-1]:
            self.assertTrue(row.with_graph.found, row.question)
            self.assertGreater(row.with_graph.steps, 0, row.question)
            self.assertTrue(row.collapsed, row.question)
            self.assertEqual(row.verdict, "collapsed")
            self.assertFalse(row.without_graph.found, row.question)
            self.assertEqual(row.without_graph.steps, 0)
            self.assertRegex(row.without_graph.answer, r"Nothing in the graph matches|I can't tell what that refers to")
        last = report.rows[-1]
        self.assertEqual(last.verdict, "refused both ways")
        self.assertFalse(last.collapsed)
        self.assertIn("payments gateway", last.with_graph.answer)
        self.assertIn("Nearest: Billing Service", last.with_graph.answer)

    def test_the_text_table_and_the_dict_carry_the_verdicts(self):
        text = self.report.to_text()
        self.assertTrue(text.startswith("Proof that the graph does the work (quillfern): 5 of 6 capabilities collapse without FalkorDB."))
        self.assertIn("refused both ways", text)
        self.assertEqual(text.count("collapsed"), 5)
        as_dict = self.report.to_dict()
        self.assertEqual((as_dict["collapsed"], as_dict["total"]), (5, 6))
        self.assertEqual(as_dict["rows"][0]["with_graph"]["found"], True)
        self.assertEqual(as_dict["rows"][-1]["verdict"], "refused both ways")

    def test_the_proof_leaves_the_graph_unchanged(self):
        _, graph = falkordb_client.connect()
        before = graph.ro_query("MATCH (n) RETURN count(n)").result_set[0][0]
        proof.run_proof()
        self.assertEqual(graph.ro_query("MATCH (n) RETURN count(n)").result_set[0][0], before)

    def test_the_route_is_behind_the_admin_token(self):
        http = TestClient(app)
        with mock.patch.object(proof_route, "settings", dataclasses.replace(settings, brain_admin_token="")):
            self.assertEqual(http.get("/api/proof").status_code, 503)
        with mock.patch.object(proof_route, "settings", dataclasses.replace(settings, brain_admin_token="proof-token")):
            self.assertEqual(http.get("/api/proof").status_code, 401)
            self.assertEqual(http.get("/api/proof", headers={"Authorization": "Bearer wrong"}).status_code, 403)
            ok = http.get("/api/proof", headers={"Authorization": "Bearer proof-token"})
            self.assertEqual(ok.status_code, 200)
            self.assertEqual((ok.json()["collapsed"], ok.json()["total"]), (5, 6))
            text = http.get("/api/proof?format=text", headers={"Authorization": "Bearer proof-token"})
            self.assertEqual(text.status_code, 200)
            self.assertIn("5 of 6 capabilities collapse", text.text)
