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


class McpInstructionsTest(unittest.TestCase):
    def test_the_chain_runs_search_then_explore_then_what_to_ask(self):
        text = mcp_tools.instructions()
        for name in ("search", "explore", "who_knows", "reading_list", "what_changed"):
            self.assertIn(name, mcp_tools.tool_names())
        self.assertLess(text.index("start with search"), text.index("Pass a name you found to explore"))
        self.assertLess(text.index("Pass a name you found to explore"), text.index("what_changed for what changed around it and upstream lately"))
        self.assertIn("reading_list for what to read before changing it", text)

    def test_a_tool_that_lands_later_joins_the_chain_and_one_that_is_gone_leaves_it(self):
        from unittest import mock

        with mock.patch.object(mcp_tools, "tool_names", return_value=["search", "explore", "who_knows"]):
            text = mcp_tools.instructions()
        self.assertIn("Pass a name you found to explore for its neighbourhood.", text)
        self.assertIn("Then ask about that name: who_knows for who to ask.", text)
        self.assertNotIn("what_changed", text)

    def test_the_server_sends_them(self):
        from src.api import mcp

        self.assertEqual(mcp.INSTRUCTIONS, mcp_tools.instructions())


class McpNextCallsTest(unittest.TestCase):
    def _found(self, *steps: PathStep) -> ToolResult:
        result = ToolResult(tool="search", answer="found")
        result.extend(steps)
        return result

    def test_the_services_a_search_hit_is_about_come_back_as_calls_to_make_next(self):
        result = self._found(
            PathStep("Arjun Mehta", "AUTHORED", "Billing runbook", "arjun", "doc-billing-runbook", "Person", "Document"),
            PathStep("Billing runbook", "DESCRIBES", "Billing Service", "doc-billing-runbook", "billing-service", "Document", "Service"),
            PathStep("TKT-102", "ABOUT", "Billing Service", "TKT-102", "billing-service", "Ticket", "Service"),
            PathStep("Invoice format spec", "DESCRIBES", "Invoice Worker", "doc-invoice-format", "invoice-worker", "Document", "Service"),
        )
        from unittest import mock

        with mock.patch.object(mcp_tools, "tool_names", return_value=["search", "explore", "what_changed", "reading_list", "who_knows"]):
            calls = mcp_tools.next_calls("search", result)
        self.assertEqual(
            [(call["tool"], call["arguments"]) for call in calls[:4]],
            [("explore", {"name": "Billing Service"}), ("what_changed", {"name": "Billing Service"}), ("reading_list", {"name": "Billing Service"}), ("who_knows", {"name": "Billing Service"})],
        )
        self.assertEqual(calls[4]["arguments"], {"name": "Invoice Worker"})
        self.assertEqual(calls[1]["why"], "what changed around it lately (Billing Service)")

    def test_no_calls_after_a_refusal_or_for_a_tool_that_is_not_listed(self):
        refused = ToolResult(tool="search", answer="nothing", found=False)
        self.assertEqual(mcp_tools.next_calls("search", refused), [])
        from unittest import mock

        result = self._found(PathStep("Billing runbook", "DESCRIBES", "Billing Service", "d", "billing-service", "Document", "Service"))
        with mock.patch.object(mcp_tools, "tool_names", return_value=["search", "who_knows"]):
            self.assertEqual([call["tool"] for call in mcp_tools.next_calls("search", result)], ["who_knows"])
