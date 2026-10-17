"""The knowledge tool contract: path steps, tool results, and the catalogue registry. No database."""

import unittest

from src.domain.knowledge import catalogue
from src.domain.knowledge.dto import MemoryLine, PathStep, ToolResult, ToolSpec


class PathStepTest(unittest.TestCase):
    def test_dict_shape_is_what_the_formatter_reads(self):
        step = PathStep("Arjun Mehta", "OWNS", "Billing Service", from_id="arjun", to_id="billing-service", from_label="Person", to_label="Service")
        self.assertEqual(step.to_dict(), {"from": "Arjun Mehta", "rel": "OWNS", "to": "Billing Service"})

    def test_citation_names_the_edge(self):
        self.assertEqual(PathStep("A", "DEPENDS_ON", "B").cite(), "A -[DEPENDS_ON]-> B")


class ToolResultTest(unittest.TestCase):
    def test_defaults_and_dict_shape(self):
        result = ToolResult(tool="who_owns", answer="Billing Service is owned by Arjun Mehta.")
        self.assertTrue(result.found)
        self.assertEqual(result.candidates, [])
        self.assertEqual(
            result.to_dict(),
            {"tool": "who_owns", "answer": "Billing Service is owned by Arjun Mehta.", "path": [], "found": True, "candidates": []},
        )

    def test_mutable_defaults_are_per_instance(self):
        first, second = ToolResult(tool="t", answer=""), ToolResult(tool="t", answer="")
        first.add(PathStep("A", "R", "B"))
        first.candidates.append("x")
        self.assertEqual(second.path, [])
        self.assertEqual(second.candidates, [])

    def test_add_skips_duplicate_edges_and_depth_is_optional(self):
        result = ToolResult(tool="blast_radius", answer="", depth=2)
        result.add(PathStep("A", "DEPENDS_ON", "B"))
        result.extend([PathStep("A", "DEPENDS_ON", "B"), PathStep("B", "DEPENDS_ON", "C")])
        self.assertEqual([step.cite() for step in result.path], ["A -[DEPENDS_ON]-> B", "B -[DEPENDS_ON]-> C"])
        self.assertEqual(result.to_dict()["path"], [{"from": "A", "rel": "DEPENDS_ON", "to": "B"}, {"from": "B", "rel": "DEPENDS_ON", "to": "C"}])
        self.assertEqual(result.to_dict()["depth"], 2)

    def test_not_found_carries_candidates(self):
        result = ToolResult(tool="who_knows", answer="The graph doesn't know that.", found=False, candidates=["Billing Service", "API Gateway"])
        self.assertFalse(result.found)
        self.assertEqual(result.candidates, ["Billing Service", "API Gateway"])
        self.assertFalse(result.to_dict()["found"])
        self.assertEqual(result.to_dict()["candidates"], ["Billing Service", "API Gateway"])

    def test_memories_carry_when_they_were_learned(self):
        line = MemoryLine(text="billing retries moved to the invoice worker", learned_at="2026-10-17", author="Bo", scope="slack_channel_G1", channel_id="G1")
        result = ToolResult(tool="recall", answer="", memories=[line])
        self.assertEqual(result.to_dict()["memories"], [line.to_dict()])
        self.assertEqual(line.to_dict()["learned_at"], "2026-10-17")
        self.assertNotIn("memories", ToolResult(tool="recall", answer="").to_dict())


class CatalogueTest(unittest.TestCase):
    def test_built_in_tools_are_registered_with_object_schemas(self):
        names = catalogue.tool_names()
        for name in ("who_owns", "who_knows", "decision_trace", "blast_radius", "ingest_ticket"):
            self.assertIn(name, names)
        for spec in catalogue.list_tools():
            self.assertEqual(spec.args_schema["type"], "object")
            self.assertTrue(spec.description)
            self.assertTrue(callable(spec.run))

    def test_only_the_graph_writers_need_approval(self):
        writers = sorted(spec.name for spec in catalogue.list_tools() if spec.needs_approval)
        self.assertEqual(writers, ["ingest_ticket", "record_decision"])
        self.assertIsNone(catalogue.get_tool("drop_everything"))

    def test_register_adds_and_replaces_by_name(self):
        def run(_args, _scopes):
            return ToolResult(tool="probe", answer="ok")

        spec = ToolSpec(name="probe_tool_for_test", description="probe", args_schema={"type": "object", "properties": {}}, run=run)
        try:
            catalogue.register(spec)
            self.assertIs(catalogue.get_tool("probe_tool_for_test"), spec)
            replacement = ToolSpec(name="probe_tool_for_test", description="probe 2", args_schema=spec.args_schema, run=run)
            catalogue.register(replacement)
            self.assertIs(catalogue.get_tool("probe_tool_for_test"), replacement)
            self.assertEqual(catalogue.tool_names().count("probe_tool_for_test"), 1)
        finally:
            catalogue._TOOLS.pop("probe_tool_for_test", None)
