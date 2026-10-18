"""/docs, /redoc and /openapi.json list every admin route: off unless DOCS_ENABLED is set."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

import tests  # noqa: F401  pins the test settings before src loads

from fastapi.testclient import TestClient  # noqa: E402

from src.core.config import settings  # noqa: E402
from src.main import app  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PATHS = ("/docs", "/redoc", "/openapi.json")

# A fresh interpreter, so the app is built with DOCS_ENABLED as given; prints each path's status.
PROBE = """
import tests
from fastapi.testclient import TestClient
from src.main import app
http = TestClient(app)
print(" ".join(str(http.get(path).status_code) for path in ("/docs", "/redoc", "/openapi.json")))
"""


def _statuses(docs_enabled: str) -> list[int]:
    env = {**os.environ, "DOCS_ENABLED": docs_enabled}
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, check=True)
    return [int(status) for status in out.stdout.split()[-3:]]


class ApiDocsTest(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(settings.docs_enabled)
        http = TestClient(app)
        for path in PATHS:
            self.assertEqual(http.get(path).status_code, 404, path)

    def test_the_flag_turns_them_on_and_only_a_true_value_does(self):
        self.assertEqual(_statuses("1"), [200, 200, 200])
        self.assertEqual(_statuses("no"), [404, 404, 404])


if __name__ == "__main__":
    unittest.main()
