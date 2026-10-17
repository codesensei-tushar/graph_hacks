"""The page and the ask route on the Quillfern graph: one question per read tool, the refusal, the write guard, the model gate."""

import dataclasses
import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from fastapi.testclient import TestClient  # noqa: E402

from src.api.v1 import ask as ask_route  # noqa: E402
from src.api.v1 import proof as proof_route  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import ask_api  # noqa: E402
from src.main import app  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_web_test{tests.TEST_GRAPH_SUFFIX}"


class WebAskTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        cls.http = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def ask(self, question: str, **params) -> dict:
        response = self.http.get("/api/ask", params={"q": question, **params})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def assert_drawable(self, answer: dict) -> None:
        """Edges join nodes by id, and read as the same lines the Slack reply prints."""
        ids = {node["id"] for node in answer["nodes"]}
        names = {node["id"]: node["name"] for node in answer["nodes"]}
        self.assertTrue(answer["edges"], answer["answer"])
        self.assertEqual(len(answer["edges"]), len(answer["path"]))
        for edge, step in zip(answer["edges"], answer["path"]):
            self.assertIn(edge["source"], ids)
            self.assertIn(edge["target"], ids)
            self.assertEqual(f"{names[edge['source']]} -[{edge['rel']}]-> {names[edge['target']]}", f"{step['from']} -[{step['rel']}]-> {step['to']}")
        self.assertTrue(all(node["label"] for node in answer["nodes"]), answer["nodes"])

    def test_one_question_per_read_tool(self):
        expectations = {
            "who owns billing": ("who_owns", "Arjun Mehta"),
            "who owns billing, and who else has worked on it": ("who_knows", "Lena Fischer"),
            "why did we move the memory layer off postgres": ("decision_trace", "RFC: memory layer on a graph store"),
            "what breaks if auth goes down": ("blast_radius", "Invoice Worker"),
            "how is maya connected to the invoice worker": ("path_between", "Billing Service"),
            "which services are load-bearing": ("load_bearing", "Postgres"),
        }
        for question, (tool, mentioned) in expectations.items():
            with self.subTest(question=question):
                answer = self.ask(question)
                self.assertEqual((answer["tool"], answer["picked_by"], answer["found"]), (tool, "keywords", True))
                self.assertIn(mentioned, answer["answer"])
                self.assertEqual(answer["scopes"], ["org_shared"])
                self.assert_drawable(answer)
        blast = self.ask("what breaks if auth goes down")
        self.assertEqual(blast["depth"], 2)
        self.assertEqual({node["label"] for node in blast["nodes"]}, {"Service"})

    def test_the_memory_tools_answer_with_no_memories_yet(self):
        related = self.ask("what have people said about billing")
        self.assertEqual((related["tool"], related["found"], related["edges"]), ("related_memories", True, []))
        self.assertIn("No memory you can read mentions Billing Service", related["answer"])
        recall = self.ask("what did anyone say about billing")
        self.assertEqual((recall["tool"], recall["found"], recall["memories"]), ("recall", True, []))

    def test_something_the_graph_does_not_know_is_refused_with_the_nearest_names(self):
        answer = self.ask("who knows the payments gateway")
        self.assertEqual((answer["tool"], answer["found"], answer["edges"], answer["nodes"]), ("who_knows", False, [], []))
        self.assertTrue(answer["answer"].startswith("Nothing in the graph matches “payments gateway” as one thing."))
        self.assertEqual(answer["candidates"], ["Billing Service", "API Gateway"])

    def test_a_tool_named_outright_runs_and_a_writer_is_a_400(self):
        answer = self.ask("billing", tool="who_owns")
        self.assertEqual((answer["tool"], answer["picked_by"]), ("who_owns", "caller"))
        self.assertIn("Arjun Mehta", answer["answer"])
        for writer in ("ingest_ticket", "record_decision"):
            response = self.http.get("/api/ask", params={"q": "billing exports time out", "tool": writer})
            self.assertEqual(response.status_code, 400, writer)
            self.assertIn("writes the graph", response.json()["error"])
        response = self.http.get("/api/ask", params={"q": "billing", "tool": "drop_everything"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("unknown tool", response.json()["error"])
        _, graph = falkordb_client.connect()
        self.assertEqual(graph.ro_query("MATCH (t:Ticket) RETURN count(t)").result_set[0][0], 3)

    def test_the_model_picker_is_behind_the_admin_token(self):
        with mock.patch.object(ask_route, "settings", dataclasses.replace(settings, brain_admin_token="")):
            self.assertEqual(self.http.get("/api/ask", params={"q": "who owns billing", "model": "1"}).status_code, 503)
        with mock.patch.object(ask_route, "settings", dataclasses.replace(settings, brain_admin_token="secret-token")):
            self.assertEqual(self.http.get("/api/ask", params={"q": "who owns billing", "model": "1"}).status_code, 401)
            wrong = self.http.get("/api/ask", params={"q": "who owns billing", "model": "1"}, headers={"Authorization": "Bearer nope"})
            self.assertEqual(wrong.status_code, 403)
            right = self.http.get("/api/ask", params={"q": "who owns billing", "model": "1"}, headers={"Authorization": "Bearer secret-token"})
            self.assertEqual(right.status_code, 200)
            # No model in tests, so the keyword router answers; the reply says which.
            self.assertEqual((right.json()["tool"], right.json()["picked_by"]), ("who_owns", "keywords"))
        # Without the flag the token is never needed.
        self.assertEqual(self.ask("who owns billing")["picked_by"], "keywords")

    def test_the_catalogue_route_lists_read_tools_and_the_company_examples(self):
        listing = self.http.get("/api/ask/tools").json()
        self.assertEqual((listing["company"], listing["company_name"]), ("quillfern", "Quillfern"))
        self.assertEqual(listing["examples"], ask_api.EXAMPLES["quillfern"])
        self.assertEqual(listing["demo"], ask_api.DEMO["quillfern"])
        names = [tool["name"] for tool in listing["tools"]]
        self.assertEqual(names[:4], ["who_owns", "who_knows", "decision_trace", "decision_pressure"])
        self.assertIn("blast_radius", names)
        self.assertNotIn("ingest_ticket", names)
        self.assertNotIn("record_decision", names)

    def test_the_public_proof_collapses_without_a_token_and_the_admin_route_stays_shut(self):
        report = self.http.get("/api/ask/proof")
        self.assertEqual(report.status_code, 200)
        body = report.json()
        self.assertEqual((body["company"], body["collapsed"], body["total"]), ("quillfern", 5, 6))
        self.assertEqual(body["rows"][-1]["verdict"], "refused both ways")
        with mock.patch.object(proof_route, "settings", dataclasses.replace(settings, brain_admin_token="")):
            self.assertEqual(self.http.get("/api/proof").status_code, 503)

    def test_the_page_and_its_files_are_served_without_shadowing_the_api(self):
        page = self.http.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.headers["content-type"].startswith("text/html"))
        self.assertIn("Company Brain", page.text)
        self.assertIn("Take the graph out", page.text)
        self.assertIn("/static/app.js", page.text)
        script = self.http.get("/static/app.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("/api/ask/proof", script.text)
        self.assertIn("javascript", script.headers["content-type"])
        self.assertEqual(self.http.get("/static/style.css").status_code, 200)
        self.assertEqual(self.http.get("/static/nope.js").status_code, 404)
        self.assertEqual(self.http.get("/healthz").json()["status"], "ok")
        missing = self.http.get("/no-such-page")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json(), {"detail": "Not Found"})
