"""A tool's graph queries are counted under its name: the agent's dispatcher and MCP set the scope."""

import unittest

import tests  # noqa: F401  pins the test settings before src loads

from src.core import query_metrics  # noqa: E402
from src.domain.knowledge import agent, catalogue, mcp_tools  # noqa: E402
from src.domain.knowledge.dto import ToolResult, ToolSpec  # noqa: E402

SEEN: list = []


def _probe(args, scopes):
    SEEN.append(query_metrics.query_tool.get())
    return ToolResult(tool="probe_scope", answer="ok")


SPEC = ToolSpec(name="probe_scope", description="Probe.", args_schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": []}, run=_probe)


class ToolScopeTest(unittest.TestCase):
    def setUp(self):
        SEEN.clear()
        catalogue.register(SPEC)

    def tearDown(self):
        catalogue._TOOLS.pop("probe_scope", None)

    def test_the_agent_dispatcher_names_the_tool(self):
        agent.run_choice(agent.ToolChoice("probe_scope", {"name": "x"}, "model"), "x", ["org_shared"])
        self.assertEqual(SEEN, ["probe_scope"])

    def test_mcp_names_the_tool(self):
        mcp_tools.call_tool("probe_scope", {"name": "x"})
        self.assertEqual(SEEN, ["probe_scope"])


if __name__ == "__main__":
    unittest.main()
