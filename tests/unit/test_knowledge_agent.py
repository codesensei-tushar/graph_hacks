"""The agent loop with the model mocked: tool choice, validation, the keyword fallback, approvals, rendering. No database."""

import unittest
from types import SimpleNamespace
from unittest import mock

from src.domain.knowledge import agent, catalogue
from src.domain.knowledge.dto import PathStep, ToolResult, ToolSpec
from src.domain.memory.writeback import SlackMemoryScope
from src.domain.slack.compute import TurnRequest
from src.integrations.llm.model import ModelUnavailable

DEPTH_SPEC = ToolSpec(
    name="probe_depth",
    description="Probe tool with a bounded depth.",
    args_schema={
        "type": "object",
        "properties": {"name": {"type": "string"}, "depth": {"type": "integer", "minimum": 1, "maximum": 6}},
        "required": ["name"],
    },
    run=lambda args, scopes: ToolResult(tool="probe_depth", answer=f"depth {args.get('depth')}", depth=args.get("depth")),
)


def _probe_result(_args, _scopes):
    result = ToolResult(tool="probe_read", answer="Probe answer.")
    result.add(PathStep("A", "REL", "B", from_id="a", to_id="b"))
    return result


READ_SPEC = ToolSpec(name="probe_read", description="Probe read tool.", args_schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}, run=_probe_result)
WRITE_RUNS: list[dict] = []
WRITE_SPEC = ToolSpec(
    name="probe_write",
    description="Record a probe in the graph. Writes the graph, so it needs approval.",
    args_schema={"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
    run=lambda args, scopes: WRITE_RUNS.append(args) or ToolResult(tool="probe_write", answer="never run here"),
    needs_approval=True,
)
PREVIEW_SPEC = ToolSpec(
    name="probe_preview",
    description="Record a probe with a preview.",
    args_schema=WRITE_SPEC.args_schema,
    run=WRITE_SPEC.run,
    needs_approval=True,
    preview=lambda args, scopes: f"Record *{args['title']}* linked to *Billing Service*",
)


def _request(question: str, **overrides) -> TurnRequest:
    fields = dict(
        workspace=SimpleNamespace(team_id="T1", bot_user_id="UBOT"),
        event=SimpleNamespace(ts="1700000000.000100"),
        question=question,
        raw_text=question,
        asker_slack_user_id="UALICE",
        asker_name="Alice",
        memory_scope=SlackMemoryScope(kind="shared", channel_id="C1"),
        read_tags=["org_shared"],
        thread=[],
        channel="C1",
        thread_ts="1700000000.000100",
        is_dm=False,
    )
    fields.update(overrides)
    return TurnRequest(**fields)


class ProbeTools(unittest.TestCase):
    SPECS = (READ_SPEC, WRITE_SPEC, DEPTH_SPEC, PREVIEW_SPEC)

    def setUp(self):
        WRITE_RUNS.clear()
        for spec in self.SPECS:
            catalogue.register(spec)

    def tearDown(self):
        for spec in self.SPECS:
            catalogue._TOOLS.pop(spec.name, None)


class SchemaAndValidationTest(ProbeTools):
    def test_schema_offers_every_tool_and_the_union_of_their_arguments(self):
        schema = agent.catalogue_schema()
        self.assertEqual(set(schema["properties"]["tool"]["enum"]), set(catalogue.tool_names()))
        args = schema["properties"]["args"]
        for key in ("name", "topic", "title", "about", "depth"):
            self.assertIn(key, args["properties"])
        self.assertFalse(args["additionalProperties"])
        self.assertEqual(schema["required"], ["tool", "args"])

    def test_validation_keeps_declared_arguments_only_and_coerces_integers(self):
        self.assertEqual(agent.validate_args(READ_SPEC, {"name": " billing ", "junk": 1}), {"name": "billing"})
        self.assertEqual(agent.validate_args(DEPTH_SPEC, {"name": "auth", "depth": "9"}), {"name": "auth", "depth": 6})
        self.assertEqual(agent.validate_args(DEPTH_SPEC, {"name": "auth", "depth": 0}), {"name": "auth", "depth": 1})
        self.assertEqual(agent.validate_args(DEPTH_SPEC, {"name": "auth"}), {"name": "auth"})

    def test_validation_rejects_missing_or_wrong_arguments(self):
        for bad in ({}, {"name": ""}, {"name": 3}, "billing", None):
            with self.subTest(bad=bad), self.assertRaises(agent.InvalidToolChoice):
                agent.validate_args(READ_SPEC, bad)
        with self.assertRaises(agent.InvalidToolChoice):
            agent.validate_args(DEPTH_SPEC, {"name": "auth", "depth": "deep"})


class ChooseToolTest(ProbeTools):
    def test_the_model_picks_and_its_arguments_are_cleaned(self):
        with mock.patch.object(agent, "generate_object", return_value={"tool": "who_owns", "args": {"name": "billing", "extra": "x"}}) as model:
            choice = agent.choose_tool("who owns billing", thread=[{"user": "U1", "text": "earlier message"}])
        self.assertEqual(choice, agent.ToolChoice("who_owns", {"name": "billing"}, "model"))
        request = model.call_args.args[0]
        self.assertEqual(request.profile, "fast")
        self.assertIn("who_owns", request.prompt)
        self.assertIn("U1: earlier message", request.prompt)
        self.assertEqual(request.schema["properties"]["tool"]["enum"], catalogue.tool_names())
        self.assertEqual(model.call_count, 1)

    def test_no_model_means_the_keyword_router(self):
        with mock.patch.object(agent, "generate_object", side_effect=ModelUnavailable("no key")):
            choice = agent.choose_tool("why did we move the memory layer off postgres")
        self.assertEqual(choice, agent.ToolChoice("decision_trace", {"topic": "why did we move the memory layer off postgres"}, "keywords"))

    def test_an_unknown_tool_or_bad_arguments_fall_back_to_keywords(self):
        for reply in ({"tool": "drop_graph", "args": {}}, {"tool": "who_owns", "args": {}}, {"tool": "who_owns", "args": "billing"}):
            with self.subTest(reply=reply), mock.patch.object(agent, "generate_object", return_value=reply):
                choice = agent.choose_tool("who owns billing")
            self.assertEqual(choice.via, "keywords")
            self.assertEqual(choice.tool, "who_owns")


class AnswerTurnTest(ProbeTools):
    def test_reply_names_the_tool_cites_the_path_and_dates_memories(self):
        memory = {"memory": "billing retries moved to the invoice worker", "updated_at": 1_760_000_000_000, "container_tag": "org_shared", "author": "Bo"}
        with (
            mock.patch.object(agent, "generate_object", return_value={"tool": "probe_read", "args": {"name": "billing"}}),
            mock.patch.object(agent, "search_brain", return_value={"results": [memory]}),
            mock.patch.object(agent, "channel_names", return_value={}),
        ):
            outcome = agent.answer_turn(_request("probe billing"))
        self.assertEqual(outcome.status, "done")
        self.assertIn("Probe answer.", outcome.reply)
        self.assertIn("*Path* (`probe_read`)", outcome.reply)
        self.assertIn("• A -[REL]-> B", outcome.reply)
        self.assertIn("billing retries moved to the invoice worker", outcome.reply)
        self.assertIn("learned 2025-10-09", outcome.reply)
        self.assertIn("Tool `probe_read`, picked by the model. Read from: public channels", outcome.reply)

    def test_depth_shows_in_the_path_header_and_keywords_in_the_footer(self):
        with (
            mock.patch.object(agent, "generate_object", return_value={"tool": "probe_depth", "args": {"name": "auth", "depth": 4}}),
            mock.patch.object(agent, "search_brain", return_value={"results": []}),
            mock.patch.object(agent, "channel_names", return_value={}),
        ):
            outcome = agent.answer_turn(_request("probe auth"))
        self.assertIn("depth 4", outcome.reply)
        with (
            mock.patch.object(agent, "generate_object", side_effect=ModelUnavailable("no key")),
            mock.patch.object(agent, "search_brain", return_value={"results": []}),
            mock.patch.object(agent, "channel_names", return_value={}),
            mock.patch.object(agent.ask_service, "route", return_value=("probe_read", {"name": "x"})),
        ):
            outcome = agent.answer_turn(_request("anything"))
        self.assertIn("picked by keywords", outcome.reply)

    def test_a_writing_tool_suspends_for_approval_instead_of_running(self):
        with (
            mock.patch.object(agent, "generate_object", return_value={"tool": "probe_write", "args": {"title": "Billing exports time out"}}),
            mock.patch.object(agent, "channel_names", return_value={}),
        ):
            outcome = agent.answer_turn(_request("record a probe"))
        self.assertEqual(WRITE_RUNS, [])
        self.assertEqual(outcome.status, "suspended")
        approval = outcome.approval
        self.assertEqual(approval.tool_name, "probe_write")
        self.assertEqual(approval.container_tag, "org_shared")
        self.assertEqual(
            approval.tool_input,
            {"title": "Billing exports time out", "asker_slack_user_id": "UALICE", "channel_id": "C1", "decided_at": 1_700_000_000},
        )
        self.assertEqual(approval.summary, "Record a probe in the graph. title: *Billing exports time out*. Visible to public channels.")

    def test_the_approval_card_shows_the_tool_preview_when_it_has_one(self):
        with (
            mock.patch.object(agent, "generate_object", return_value={"tool": "probe_preview", "args": {"title": "Exports"}}),
            mock.patch.object(agent, "channel_names", return_value={}),
        ):
            outcome = agent.answer_turn(_request("record a probe"))
        self.assertEqual(outcome.approval.summary, "Record *Exports* linked to *Billing Service*. Visible to public channels.")
        self.assertEqual(WRITE_RUNS, [])

    def test_other_names_the_question_matched_are_offered(self):
        def two_names(_args, _scopes):
            result = ToolResult(tool="probe_read", answer="Invoice Worker is owned by Lena Fischer.", candidates=["Billing Service"])
            result.add(PathStep("Lena Fischer", "OWNS", "Invoice Worker"))
            return result

        catalogue.register(ToolSpec(name="probe_read", description="Probe read tool.", args_schema=READ_SPEC.args_schema, run=two_names))
        with (
            mock.patch.object(agent, "generate_object", return_value={"tool": "probe_read", "args": {"name": "the invoice worker and billing"}}),
            mock.patch.object(agent, "search_brain", return_value={"results": []}),
            mock.patch.object(agent, "channel_names", return_value={}),
        ):
            outcome = agent.answer_turn(_request("who owns the invoice worker and billing"))
        self.assertIn("_Also matched: Billing Service. Ask again with that name to switch._", outcome.reply)

    def test_passive_investigation_stays_silent(self):
        outcome = agent.answer_turn(_request("", passive_investigation={"reason": "thread"}))
        self.assertTrue(outcome.silent_conclusion)

    def test_live_updates_join_the_question(self):
        with mock.patch.object(agent, "choose_tool", return_value=agent.ToolChoice("probe_read", {"name": "x"}, "model")) as choose, \
             mock.patch.object(agent, "search_brain", return_value={"results": []}), mock.patch.object(agent, "channel_names", return_value={}):
            agent.answer_turn(_request("what breaks if auth goes down", live_updates=["and two hops out"]))
        self.assertEqual(choose.call_args.args[0], "what breaks if auth goes down and two hops out")


class LearnedAtTest(unittest.TestCase):
    def test_dates_come_from_updated_at_then_slack_ts(self):
        self.assertEqual(agent.learned_at(1_760_000_000_000, None), "2025-10-09")
        self.assertEqual(agent.learned_at(None, "1700000000.000100"), "2023-11-14")
        self.assertEqual(agent.learned_at(None, None), "")
        self.assertEqual(agent.learned_at(0, "not a ts"), "")
