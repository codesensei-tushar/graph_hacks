"""The real app boots with the env deploy/deploy.sh writes before the Slack and model
secrets exist, against a real FalkorDB (throwaway graph ``company_startup_test``).

Until the real values land in Secret Manager, deploy.sh writes an empty bot token and
model key, and a random signing secret. The app must start, report FalkorDB healthy on
/healthz, and reject unsigned Slack requests. It runs in a subprocess because
``settings`` is built once per process.
"""

import json
import os
import secrets
import subprocess
import sys
import unittest

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db.falkordb_client import client  # noqa: E402

GRAPH = "company_startup_test"

PROBE = """
import json
from fastapi.testclient import TestClient
from src.main import app

with TestClient(app) as c:
    health = c.get("/healthz")
    unsigned = c.post("/slack/events", json={"type": "event_callback", "event": {"type": "message"}})
    challenge = c.post("/slack/events", json={"type": "url_verification", "challenge": "x"})
    interaction = c.post("/slack/interactions", content=b"payload=%7B%7D")
print(json.dumps({
    "health": [health.status_code, health.json()],
    "unsigned": unsigned.status_code,
    "challenge": challenge.status_code,
    "interaction": interaction.status_code,
}))
"""


class DeployStartupTest(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        client().select_graph(GRAPH).delete()

    def test_boots_with_placeholder_secrets(self):
        env = {
            **os.environ,
            "FALKORDB_GRAPH": GRAPH,
            "SLACK_BOT_TOKEN": "",
            "SLACK_SIGNING_SECRET": secrets.token_hex(32),
            "LLM_API_KEY": "",
            "MODEL_API_KEY": "",
            "GIT_SHA": "startup-test",
        }
        done = subprocess.run([sys.executable, "-c", PROBE], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        result = json.loads(done.stdout.strip().splitlines()[-1])

        status, health = result["health"]
        self.assertEqual(status, 200)
        self.assertEqual(health["git_sha"], "startup-test")
        self.assertEqual(health["falkordb"], "ok")
        self.assertIsNone(health["slack_last_event_at"])
        self.assertEqual(result["unsigned"], 401)
        self.assertEqual(result["challenge"], 401)
        self.assertEqual(result["interaction"], 401)


if __name__ == "__main__":
    unittest.main()
