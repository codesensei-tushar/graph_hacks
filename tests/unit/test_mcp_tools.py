"""The MCP tool list is the read-only catalogue, name for name and schema for schema."""

import dataclasses
import unittest

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.domain.knowledge import catalogue, mcp_tools  # noqa: E402
from src.domain.knowledge.dto import PathStep, ToolResult  # noqa: E402


class McpToolListTest(unittest.TestCase):
    def test_lists_every_read_only_tool_with_its_schema(self):
        expected = [(spec.name, spec.description, spec.args_schema) for spec in catalogue.list_tools() if not spec.needs_approval]
        self.assertTrue(expected)
        self.assertEqual([(t.name, t.description, t.input_schema) for t in mcp_tools.read_only_tools()], expected)

    def test_writing_tools_are_not_listed(self):
        writers = {spec.name for spec in catalogue.list_tools() if spec.needs_approval}
        self.assertIn("ingest_ticket", writers)
        self.assertFalse(writers & set(mcp_tools.tool_names()))

    def test_call_refuses_writers_unknown_tools_and_bad_arguments(self):
        with self.assertRaises(mcp_tools.ToolError):
            mcp_tools.call_tool("ingest_ticket", {"title": "x"})
        with self.assertRaises(mcp_tools.ToolError):
            mcp_tools.call_tool("drop_everything", {})
        with self.assertRaises(mcp_tools.ToolError):
            mcp_tools.call_tool("who_owns", {})
        with self.assertRaises(mcp_tools.ToolError):
            mcp_tools.call_tool("who_owns", {"name": "billing", "scope": "github_repo_1"})

    def test_call_runs_the_spec_with_the_org_scope_and_returns_the_dict_shape(self):
        original = catalogue.get_tool("who_owns")
        result = ToolResult(tool="who_owns", answer="Billing Service is owned by Arjun Mehta.", found=True)
        result.add(PathStep("Arjun Mehta", "OWNS", "Billing Service"))
        calls = []

        def run(args, scopes):
            calls.append((args, scopes))
            return result

        catalogue.register(dataclasses.replace(original, run=run))
        try:
            payload = mcp_tools.call_tool("who_owns", {"name": "billing"})
        finally:
            catalogue.register(original)
        self.assertEqual(calls, [({"name": "billing"}, ["org_shared"])])
        self.assertEqual(payload["tool"], "who_owns")
        self.assertEqual(payload["path"], [{"from": "Arjun Mehta", "rel": "OWNS", "to": "Billing Service"}])
        self.assertEqual((payload["found"], payload["candidates"]), (True, []))


if __name__ == "__main__":
    unittest.main()
