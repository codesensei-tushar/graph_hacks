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
        ask_api.ANSWERS.clear()
        for spec in self.SPECS:
            catalogue.register(spec)
        # Shaping an answer reads each Service's kind from the graph; these tests have no graph.
        kinds = mock.patch.object(ask_api, "service_kinds", return_value={})
        kinds.start()
        self.addCleanup(kinds.stop)

    def tearDown(self):
        for spec in self.SPECS:
            catalogue._TOOLS.pop(spec.name, None)

    def test_the_keyword_choice_runs_and_the_path_becomes_nodes_and_edges(self):
        with (
            mock.patch.object(agent, "choose_with_keywords", return_value=agent.ToolChoice("probe_read", {"name": "billing"}, "keywords")),
            mock.patch.object(ask_api, "service_kinds", return_value={"billing-service": "service", "postgres": "system"}) as service_kinds,
        ):
            answer = ask_api.answer_question("  who owns   billing ")
        service_kinds.assert_called_once()
        self.assertEqual(answer.question, "who owns billing")
        self.assertEqual((answer.tool, answer.picked_by, answer.found, answer.depth), ("probe_read", "keywords", True, 2))
        self.assertEqual(answer.scopes, ["org_shared"])
        self.assertIn("['org_shared']", answer.answer)
        self.assertEqual(
            [node.to_dict() for node in answer.nodes],
            [{"id": node_id, "name": name, "label": label, "kind": kind} for (node_id, name, label), kind in ((ARJUN, None), (BILLING, "service"), (POSTGRES, "system"))],
        )
        self.assertEqual(
            [edge.to_dict() for edge in answer.edges],
            [{"source": "arjun", "rel": "OWNS", "target": "billing-service"}, {"source": "billing-service", "rel": "DEPENDS_ON", "target": "postgres"}],
        )
        # The same lines the Slack reply cites.
        self.assertEqual(answer.path, [{"from": "Arjun Mehta", "rel": "OWNS", "to": "Billing Service"}, {"from": "Billing Service", "rel": "DEPENDS_ON", "to": "Postgres"}])
        as_dict = answer.to_dict()
        self.assertEqual(
            set(as_dict), {"notice", "question", "tool", "picked_by", "answer", "found", "candidates", "depth", "path", "nodes", "edges", "memories", "scopes"}
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

    def test_a_slack_write_command_runs_the_matching_read_tool_and_never_the_writer(self):
        def run(choice, question, scopes):
            return ToolResult(tool=choice.tool, answer=f"read {question!r} with {choice.args}"), choice

        with (
            mock.patch.object(agent, "choose_with_keywords") as keywords,
            mock.patch.object(ask_api, "_read_spec", wraps=ask_api._read_spec) as read_spec,
            mock.patch.object(agent, "run_choice", side_effect=run),
        ):
            answer = ask_api.answer_question("ticket: billing exports time out")
        keywords.assert_not_called()
        read_spec.assert_called_once_with("who_knows")
        self.assertEqual((answer.tool, answer.picked_by), ("who_knows", "caller"))
        self.assertEqual(answer.answer, "read 'billing exports time out' with {'name': 'billing exports time out'}")
        self.assertEqual(answer.question, "ticket: billing exports time out")
        self.assertTrue(answer.notice.startswith("Filing a ticket writes the graph, so it happens in Slack"))

    def test_a_bare_command_or_a_question_about_a_decision_gets_no_notice(self):
        with mock.patch.object(agent, "choose_with_keywords", return_value=agent.ToolChoice("probe_read", {"name": "x"}, "keywords")):
            self.assertEqual(ask_api.answer_question("ticket:").notice, "")
            self.assertEqual(ask_api.answer_question("why did we decide to move billing").notice, "")


class ServiceKindTest(unittest.TestCase):
    """The drawing's kind for each Service, with no graph."""

    def test_graph_of_gives_a_service_its_kind_and_no_other_node_one(self):
        renewals = ("renewals", "Renewals", "Service")
        steps = [_step(ARJUN, "OWNS", BILLING), _step(renewals, "DEPENDS_ON", ("hubspot", "HubSpot", "Service")), PathStep("Pricing", "DEPENDS_ON", "Renewals", to_id="renewals", to_label="Service")]
        nodes, _ = ask_api.graph_of(steps, {"billing-service": "service", "renewals": "process", "arjun": "process"})
        # The person keeps no kind even when the map names it; HubSpot is not in the map; Pricing has no id.
        self.assertEqual({node.id: node.kind for node in nodes}, {"arjun": None, "billing-service": "service", "renewals": "process", "hubspot": None, "Pricing": None})
        self.assertEqual(nodes[1].to_dict(), {"id": "billing-service", "name": "Billing Service", "label": "Service", "kind": "service"})
        self.assertEqual([node.kind for node in ask_api.graph_of(steps)[0]], [None] * 5)

    def test_no_kind_query_runs_when_the_path_names_no_service_by_id(self):
        with mock.patch.object(ask_api, "connect") as connect:
            self.assertEqual(ask_api.service_kinds([PathStep("Billing Service", "ABOUT", "TKT-102", to_label="Ticket")], ["org_shared"]), {})
        connect.assert_not_called()
