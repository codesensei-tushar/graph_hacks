"""/healthz, the reserved /mcp path, and the Slack event clock. FalkorDB is mocked."""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api import ops  # noqa: E402


def _app() -> FastAPI:
    app = FastAPI()

    # Stand-in for the real Slack route: 200 when "signed", 401 otherwise.
    @app.post("/slack/events")
    def events(signed: bool = False):
        return {"ok": True} if signed else JSONResponse({"error": "invalid signature"}, status_code=401)

    ops.install(app)
    return app


class HealthzTest(unittest.TestCase):
    def setUp(self):
        ops._slack_clock["last_event_at"] = None
        self.client = TestClient(_app())

    def test_reports_sha_and_falkordb_ok(self):
        graph = mock.Mock()
        with mock.patch.object(ops, "connect", return_value=(None, graph)), mock.patch.object(ops, "GIT_SHA", "abc123"):
            body = self.client.get("/healthz").json()
        graph.ro_query.assert_called_once_with("RETURN 1")
        self.assertEqual(body, {"status": "ok", "git_sha": "abc123", "falkordb": "ok", "slack_last_event_at": None})

    def test_falkordb_failure_is_reported_not_raised(self):
        with mock.patch.object(ops, "connect", side_effect=ConnectionError("down")):
            response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["falkordb"], "error")

    def test_slack_clock_moves_only_on_verified_requests(self):
        graph = mock.Mock()
        with mock.patch.object(ops, "connect", return_value=(None, graph)):
            self.assertEqual(self.client.post("/slack/events").status_code, 401)
            self.assertIsNone(self.client.get("/healthz").json()["slack_last_event_at"])

            self.assertEqual(self.client.post("/slack/events?signed=true").status_code, 200)
            self.assertIsNotNone(self.client.get("/healthz").json()["slack_last_event_at"])

    def test_mcp_is_reserved(self):
        self.assertEqual(self.client.post("/mcp").status_code, 501)
        self.assertEqual(self.client.get("/mcp/anything").status_code, 501)


if __name__ == "__main__":
    unittest.main()
