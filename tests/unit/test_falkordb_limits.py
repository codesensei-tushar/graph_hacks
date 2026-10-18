"""Both compose files set FalkorDB's limits on purpose, and to the same values.

The image default, ``MAX_QUEUED_QUERIES 25 TIMEOUT 1000 RESULTSET_SIZE 10000``, cuts every
result at 10k rows without an error and kills reads after 1 s (docs/DEPLOY.md, "FalkorDB limits").
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = [ROOT / "deploy" / "docker-compose.yml", ROOT / "docker-compose.yml"]


def falkordb_args(path: Path) -> dict[str, int]:
    found = re.findall(r"^\s*FALKORDB_ARGS:\s*(.+?)\s*$", path.read_text(), re.M)
    assert len(found) == 1, f"{path}: expected one FALKORDB_ARGS, found {found}"
    words = found[0].strip("\"'").split()
    return {words[i]: int(words[i + 1]) for i in range(0, len(words), 2)}


class FalkorDBLimitsTest(unittest.TestCase):
    def test_limits_are_set_deliberately_in_both_compose_files(self):
        for path in COMPOSE:
            with self.subTest(path=path.name):
                args = falkordb_args(path)
                self.assertEqual(args["RESULTSET_SIZE"], -1)  # no silent row cap
                self.assertGreaterEqual(args["TIMEOUT"], 10000)  # reads get 10 s, not 1 s
                self.assertGreaterEqual(args["MAX_QUEUED_QUERIES"], 100)

    def test_dev_and_production_match(self):
        self.assertEqual(falkordb_args(COMPOSE[0]), falkordb_args(COMPOSE[1]))


if __name__ == "__main__":
    unittest.main()
