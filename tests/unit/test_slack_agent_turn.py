"""The Slack turn around the agent, with the model mocked: approvals, rendering, dates. No database."""

import unittest
from types import SimpleNamespace
from unittest import mock

from src.domain.knowledge import agent, catalogue
from src.domain.knowledge.dto import MemoryLine, PathStep, ToolResult, ToolSpec
from src.domain.memory.writeback import SlackMemoryScope
from src.domain.slack import agent_turn
from src.domain.slack.compute import TurnRequest
from src.integrations.llm.model import ModelUnavailable


def _probe_result(_args, _scopes):
    result = ToolResult(tool="probe_read", answer="Probe answer.")
    result.add(PathStep("A", "REL", "B", from_id="a", to_id="b"))
    return result


READ_SPEC = ToolSpec(name="probe_read", description="Probe read tool.", args_schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}, run=_probe_result)
DEPTH_SPEC = ToolSpec(
    name="probe_depth",
    description="Probe tool with a bounded depth.",
    args_schema={"type": "object", "properties": {"name": {"type": "string"}, "depth": {"type": "integer", "minimum": 1, "maximum": 6}}, "required": ["name"]},
    run=lambda args, scopes: ToolResult(tool="probe_depth", answer=f"depth {args.get('depth')}", depth=args.get("depth")),
)
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


def _quiet():
    return (
        mock.patch.object(agent_turn, "attached", return_value=[]),
        mock.patch.object(agent_turn, "channel_names", return_value={}),
    )


class AnswerTurnTest(unittest.TestCase):
    SPECS = (READ_SPEC, WRITE_SPEC, DEPTH_SPEC, PREVIEW_SPEC)

    def setUp(self):
        WRITE_RUNS.clear()
        for spec in self.SPECS:
            catalogue.register(spec)

    def tearDown(self):
        for spec in self.SPECS:
            catalogue._TOOLS.pop(spec.name, None)

    def test_reply_names_the_tool_cites_the_path_and_dates_memories(self):
        memory = MemoryLine(text="billing retries moved to the invoice worker", learned_at="2025-10-09", author="Bo", scope="org_shared")
        with (
            mock.patch.object(agent, "generate_object", return_value={"tool": "probe_read", "args": {"name": "billing"}}),
            mock.patch.object(agent_turn, "attached", return_value=[memory]),
            mock.patch.object(agent_turn, "channel_names", return_value={}),
        ):
            outcome = agent_turn.answer_turn(_request("probe billing"))
        self.assertEqual(outcome.status, "done")
        self.assertIn("Probe answer.", outcome.reply)
        self.assertIn("*Path* (`probe_read`, 1 edge)", outcome.reply)
        self.assertIn("• A -[REL]-> B", outcome.reply)
        self.assertIn("billing retries moved to the invoice worker", outcome.reply)
        self.assertIn("learned 2025-10-09", outcome.reply)
        self.assertIn("Tool `probe_read`, picked by the model. Read from: public channels", outcome.reply)

    def test_depth_shows_in_the_path_header_and_keywords_in_the_footer(self):
        search, names = _quiet()
        with mock.patch.object(agent, "generate_object", return_value={"tool": "probe_depth", "args": {"name": "auth", "depth": 4}}), search, names:
            outcome = agent_turn.answer_turn(_request("probe auth"))
        self.assertIn("depth 4", outcome.reply)
        search, names = _quiet()
        with (
            mock.patch.object(agent, "generate_object", side_effect=ModelUnavailable("no key")),
            mock.patch.object(agent.ask_service, "route", return_value=("probe_read", {"name": "x"})),
            search,
            names,
        ):
            outcome = agent_turn.answer_turn(_request("anything"))
        self.assertIn("picked by keywords", outcome.reply)

    def test_a_writing_tool_suspends_for_approval_instead_of_running(self):
        with (
            mock.patch.object(agent, "generate_object", return_value={"tool": "probe_write", "args": {"title": "Billing exports time out"}}),
            mock.patch.object(agent_turn, "channel_names", return_value={}),
        ):
            outcome = agent_turn.answer_turn(_request("file a ticket: billing exports time out"))
        self.assertEqual(WRITE_RUNS, [])
        self.assertEqual(outcome.status, "suspended")
        approval = outcome.approval
        self.assertEqual(approval.tool_name, "probe_write")
        self.assertEqual(approval.container_tag, "org_shared")
        self.assertEqual(
            approval.tool_input,
            {"title": "Billing exports time out", "asker_slack_user_id": "UALICE", "channel_id": "C1", "decided_at": 1_700_000_000_000},
        )
        self.assertEqual(approval.summary, "Record a probe in the graph. title: *Billing exports time out*. Visible to public channels.")

    def test_the_approval_card_shows_the_tool_preview_when_it_has_one(self):
        with (
            mock.patch.object(agent, "generate_object", return_value={"tool": "probe_preview", "args": {"title": "Exports"}}),
            mock.patch.object(agent_turn, "channel_names", return_value={}),
        ):
            outcome = agent_turn.answer_turn(_request("record a probe called Exports"))
        self.assertEqual(outcome.approval.summary, "Record *Exports* linked to *Billing Service*. Visible to public channels.")
        self.assertEqual(WRITE_RUNS, [])

    def test_other_names_the_question_matched_are_offered(self):
        def two_names(_args, _scopes):
            result = ToolResult(tool="probe_read", answer="Invoice Worker is owned by Lena Fischer.", candidates=["Billing Service"])
            result.add(PathStep("Lena Fischer", "OWNS", "Invoice Worker"))
            return result

        catalogue.register(ToolSpec(name="probe_read", description="Probe read tool.", args_schema=READ_SPEC.args_schema, run=two_names))
        search, names = _quiet()
        with mock.patch.object(agent, "generate_object", return_value={"tool": "probe_read", "args": {"name": "the invoice worker and billing"}}), search, names:
            outcome = agent_turn.answer_turn(_request("who owns the invoice worker and billing"))
        self.assertIn("_Also matched: Billing Service. Ask again with that name to switch._", outcome.reply)

    def test_a_refusal_shows_no_memories_so_it_never_reads_like_a_guess(self):
        miss = ToolSpec(
            name="probe_miss",
            description="Probe miss tool.",
            args_schema=READ_SPEC.args_schema,
            run=lambda args, scopes: ToolResult(tool="probe_miss", answer="Nothing in the graph matches “x”.", found=False),
        )
        catalogue.register(miss)
        try:
            with (
                mock.patch.object(agent, "generate_object", return_value={"tool": "probe_miss", "args": {"name": "x"}}),
                mock.patch.object(agent_turn, "attached") as search,
                mock.patch.object(agent_turn, "channel_names", return_value={}),
            ):
                outcome = agent_turn.answer_turn(_request("who knows x"))
        finally:
            catalogue._TOOLS.pop("probe_miss", None)
        search.assert_not_called()
        self.assertNotIn("From conversations", outcome.reply)
        self.assertIn("Nothing in the graph matches “x”.", outcome.reply)

    def test_passive_investigation_stays_silent(self):
        outcome = agent_turn.answer_turn(_request("", passive_investigation={"reason": "thread"}))
        self.assertTrue(outcome.silent_conclusion)

    def test_live_updates_join_the_question(self):
        search, names = _quiet()
        with mock.patch.object(agent_turn, "choose_tool", return_value=agent.ToolChoice("probe_read", {"name": "x"}, "model")) as choose, search, names:
            agent_turn.answer_turn(_request("what breaks if auth goes down", live_updates=["and two hops out"]))
        self.assertEqual(choose.call_args.args[0], "what breaks if auth goes down and two hops out")


class DatesTest(unittest.TestCase):
    def test_learned_prefers_first_save_then_message_ts_then_last_update(self):
        self.assertEqual(agent_turn.learned_at({"created_at": 1_760_000_000_000, "updated_at": 1_761_000_000_000}), "2025-10-09")
        self.assertEqual(agent_turn.learned_at({"ts": "1700000000.000100", "updated_at": 1_761_000_000_000}), "2023-11-14")
        self.assertEqual(agent_turn.learned_at({"updated_at": 1_761_000_000_000}), "2025-10-20")
        self.assertEqual(agent_turn.learned_at({}), "")

    def test_message_ts_becomes_milliseconds(self):
        self.assertEqual(agent_turn.epoch_ms("1700000000.000100"), 1_700_000_000_000)
        self.assertIsNone(agent_turn.epoch_ms(None))
        self.assertIsNone(agent_turn.epoch_ms("later"))
