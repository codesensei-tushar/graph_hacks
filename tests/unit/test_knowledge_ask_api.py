"""The read-only ask surface with probe tools: tool choice, the write refusal, and the nodes and edges of a path. No database, no model."""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test settings before src loads

from src.domain.knowledge import agent, ask_api, catalogue
from src.domain.knowledge.dto import PathStep, ToolResult, ToolSpec

ARJUN = ("arjun", "Arjun Mehta", "Person")
BILLING = ("billing-service", "Billing Service", "Service")
POSTGRES = ("postgres", "Postgres", "Service")


def _step(source: tuple, rel: str, target: tuple) -> PathStep:
    return PathStep(source[1], rel, target[1], from_id=source[0], to_id=target[0], from_label=source[2], to_label=target[2])


def _probe_result(args: dict, scopes: list[str]) -> ToolResult:
    result = ToolResult(tool="probe_read", answer=f"Probe answer about {args.get('name')!r} for {scopes}.", depth=2)
    result.extend([_step(ARJUN, "OWNS", BILLING), _step(BILLING, "DEPENDS_ON", POSTGRES)])
    return result


READ_SPEC = ToolSpec(
    name="probe_read",
    description="Probe read tool.",
    args_schema={"type": "object", "properties": {"name": {"type": "string"}, "depth": {"type": "integer", "minimum": 1, "maximum": 6}}, "required": ["name"]},
    run=_probe_result,
)
PAIR_SPEC = ToolSpec(
    name="probe_pair",
    description="Probe tool that names two things.",
    args_schema={"type": "object", "properties": {"source": {"type": "string"}, "target": {"type": "string"}}, "required": ["source", "target"]},
    run=lambda args, scopes: ToolResult(tool="probe_pair", answer=f"{args['source']}|{args['target']}"),
)
WRITE_SPEC = ToolSpec(
    name="probe_write",
    description="Record a probe. Writes the graph, so it needs approval.",
    args_schema={"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
    run=lambda args, scopes: ToolResult(tool="probe_write", answer="never run here"),
    needs_approval=True,
)


class AskApiTest(unittest.TestCase):
    SPECS = (READ_SPEC, PAIR_SPEC, WRITE_SPEC)

    def setUp(self):
        for spec in self.SPECS:
            catalogue.register(spec)

    def tearDown(self):
        for spec in self.SPECS:
            catalogue._TOOLS.pop(spec.name, None)

    def test_the_keyword_choice_runs_and_the_path_becomes_nodes_and_edges(self):
        with mock.patch.object(agent, "choose_with_keywords", return_value=agent.ToolChoice("probe_read", {"name": "billing"}, "keywords")):
            answer = ask_api.answer_question("  who owns   billing ")
        self.assertEqual(answer.question, "who owns billing")
        self.assertEqual((answer.tool, answer.picked_by, answer.found, answer.depth), ("probe_read", "keywords", True, 2))
        self.assertEqual(answer.scopes, ["org_shared"])
        self.assertIn("['org_shared']", answer.answer)
        self.assertEqual(
            [node.to_dict() for node in answer.nodes],
            [{"id": node_id, "name": name, "label": label} for node_id, name, label in (ARJUN, BILLING, POSTGRES)],
        )
        self.assertEqual(
            [edge.to_dict() for edge in answer.edges],
            [{"source": "arjun", "rel": "OWNS", "target": "billing-service"}, {"source": "billing-service", "rel": "DEPENDS_ON", "target": "postgres"}],
        )
        # The same lines the Slack reply cites.
        self.assertEqual(answer.path, [{"from": "Arjun Mehta", "rel": "OWNS", "to": "Billing Service"}, {"from": "Billing Service", "rel": "DEPENDS_ON", "to": "Postgres"}])
        as_dict = answer.to_dict()
        self.assertEqual(
            set(as_dict), {"question", "tool", "picked_by", "answer", "found", "candidates", "depth", "path", "nodes", "edges", "memories", "scopes"}
        )
        self.assertEqual(len(as_dict["edges"]), len(as_dict["path"]))

    def test_a_write_tool_never_runs_whether_chosen_or_named(self):
        with mock.patch.object(agent, "choose_with_keywords", return_value=agent.ToolChoice("probe_write", {"title": "x"}, "keywords")):
            with self.assertRaisesRegex(ask_api.ToolRefused, "writes the graph"):
                ask_api.answer_question("record a probe")
        with self.assertRaisesRegex(ask_api.ToolRefused, "writes the graph"):
            ask_api.answer_question("record a probe", tool="probe_write")
        with self.assertRaisesRegex(ask_api.ToolRefused, "unknown tool 'probe_nope'"):
            ask_api.answer_question("anything", tool="probe_nope")

    def test_naming_a_read_tool_fills_its_arguments_from_the_question(self):
        answer = ask_api.answer_question("billing", tool="probe_read")
        self.assertEqual((answer.tool, answer.picked_by), ("probe_read", "caller"))
        self.assertIn("'billing'", answer.answer)
        pair = ask_api.answer_question("maya and the invoice worker", tool="probe_pair")
        self.assertEqual(pair.answer, "maya and the invoice worker|")

    def test_naming_the_tool_the_router_would_pick_keeps_the_router_arguments(self):
        with mock.patch.object(ask_api.ask_service, "route", return_value=("probe_read", {"name": "auth", "depth": 3})) as route:
            answer = ask_api.answer_question("what breaks if auth goes down, three hops", tool="probe_read")
        route.assert_called_once_with("what breaks if auth goes down, three hops")
        self.assertIn("'auth'", answer.answer)

    def test_asking_for_the_model_without_one_falls_back_to_keywords(self):
        with mock.patch.object(agent, "choose_with_keywords", return_value=agent.ToolChoice("probe_read", {"name": "billing"}, "keywords")) as keywords:
            answer = ask_api.answer_question("who owns billing", use_model=True)
        keywords.assert_called_once()
        self.assertEqual(answer.picked_by, "keywords")

    def test_an_empty_question_is_not_an_answer(self):
        answer = ask_api.answer_question("   ")
        self.assertEqual((answer.tool, answer.found, answer.nodes, answer.edges), ("none", False, [], []))

    def test_the_read_catalogue_leaves_out_every_writer(self):
        names = [tool.name for tool in ask_api.read_tools()]
        self.assertIn("probe_read", names)
        self.assertNotIn("probe_write", names)
        for name in names:
            self.assertFalse(catalogue.get_tool(name).needs_approval, name)
        probe = next(tool for tool in ask_api.read_tools() if tool.name == "probe_read")
        self.assertEqual(probe.to_dict(), {"name": "probe_read", "description": "Probe read tool.", "args": ["name", "depth"]})

    def test_graph_of_keys_nodes_by_id_then_by_name_and_keeps_each_edge_once(self):
        unnamed = PathStep("Billing Service", "ABOUT", "TKT-102")  # no ids: keyed by name, so it joins the id-bearing node
        nodes, edges = ask_api.graph_of([_step(ARJUN, "OWNS", BILLING), _step(ARJUN, "OWNS", BILLING), unnamed, PathStep("Billing Service", "ABOUT", "TKT-102", to_label="Ticket")])
        self.assertEqual([node.id for node in nodes], ["arjun", "billing-service", "Billing Service", "TKT-102"])
        self.assertEqual(nodes[3].label, "Ticket")  # a later step filled the label in
        self.assertEqual(len(edges), 2)
