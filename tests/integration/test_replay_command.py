"""``python scripts/replay_workspace.py --graph <name>`` as a judge runs it, end to end, in its own
process, against a real FalkorDB (throwaway graphs ``replay_cli_test`` and ``brain_control_replay_cli``
plus ``TEST_GRAPH_SUFFIX``).

test_demo_replay drives the same steps through ``run_in_process``'s pieces, so it never runs
``main()``. This test does: an import that loads ``src.core.config`` before the fake Slack API is
configured makes the script exit with "nothing was replayed" while every other test stays green.
It runs with TENANCY_WRITE_STRICT on, as production does.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db.falkordb_client import client  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
GRAPH = f"replay_cli_test{tests.TEST_GRAPH_SUFFIX}"
CONTROL = f"brain_control_replay_cli{tests.TEST_GRAPH_SUFFIX}"


class ReplayCommandTest(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        for name in (GRAPH, CONTROL):
            try:
                client().select_graph(name).delete()
            except Exception:
                pass

    def test_the_readme_command_replays_the_whole_demo(self):
        env = {**os.environ, "BRAIN_CONTROL_GRAPH": CONTROL, "TENANCY_WRITE_STRICT": "true", "PYTHONDONTWRITEBYTECODE": "1"}
        done = subprocess.run(
            [sys.executable, "scripts/replay_workspace.py", "--graph", GRAPH],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
        )
        output = done.stdout + done.stderr
        self.assertNotIn("nothing was replayed", output)
        self.assertEqual(done.returncode, 0, output[-2000:])
        self.assertIn(f"seeded quillfern graph={GRAPH}", done.stdout)
        self.assertIn("Billing Service is owned by Arjun Mehta.", done.stdout)
        # The README's "Try it in two minutes" promises this last line: 17 replies and two Approve cards.
        self.assertRegex(done.stdout.strip().splitlines()[-1], r"^fake Slack calls=\d+ bot messages=19$")
