"""``reading_list`` on a real FalkorDB: Quillfern engineering, its Sales team, and one private decision.

The private decision is the supersession trap: scoped to a channel, it supersedes the billing
retries decision. A public reader must still see the old one standing; a reader of that
channel sees the new one, once.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import agent, ask_service, catalogue, mcp_tools, reading_list, resolve  # noqa: E402
from src.domain.memory.read_scope import resolve_brain_read_container_tags  # noqa: E402
from src.domain.memory.writeback import SlackMemoryScope  # noqa: E402
from src.domain.slack import agent_turn  # noqa: E402
from src.domain.slack.compute import TurnRequest  # noqa: E402
from src.integrations.llm.model import ModelUnavailable  # noqa: E402
from src.integrations.slack.events import SlackEventInner  # noqa: E402
from src.seed import quillfern, quillfern_sales, seed  # noqa: E402

GRAPH = f"company_reading_list_test{tests.TEST_GRAPH_SUFFIX}"
ORG = ["org_shared"]
PRIVATE = "slack_channel_C9"


def _cites(result) -> list[str]:
    return [step.cite() for step in result.path]


INVOICE_WORKER = (
    "Before touching Invoice Worker, read 3 documents, nearest first: Invoice format spec by Lena Fischer (Invoice Worker itself), "
    "Billing runbook by Arjun Mehta (Billing Service, which it depends on) and Auth session design by Maya Ortiz (Auth Service, 2 hops out). "
    "Standing decision: Retry renewal webhooks inside the billing service (Billing Service, 20 Sep 2026), based on TKT-102 and the Billing runbook. "
    "Nothing documents Notification Service or Postgres; ask Lena Fischer and Sara Lindqvist."
)
CHANNEL = "CREAD"
BOT = SimpleNamespace(team_id="T1", bot_user_id="UBOT")


def _turn(question: str, thread: list[dict] | None = None) -> TurnRequest:
    scope = SlackMemoryScope(kind="shared", channel_id=CHANNEL, channel_type="channel")
    event = SlackEventInner(type="app_mention", user="UALICE", channel=CHANNEL, channel_type="channel", text=question, ts="1700000000.000100")
    return TurnRequest(
        workspace=BOT,
        event=event,
        question=question,
        raw_text=question,
        asker_slack_user_id="UALICE",
        asker_name="Alice",
        memory_scope=scope,
        read_tags=resolve_brain_read_container_tags(scope),
        thread=thread or [],
        channel=CHANNEL,
        thread_ts=event.ts,
        is_dm=False,
    )


def _read(question: str, scopes=None):
    tool, args = ask_service.route(question)
    assert tool == "reading_list", (question, tool)
    return catalogue.get_tool(tool).run(args, scopes or ORG)


class ReadingListTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        quillfern_sales.seed_sales()
        _, graph = falkordb_client.connect()
        graph.query(
            f"""
            MATCH (old:Decision {{id: 'decision-billing-retries'}}), (billing:Service {{id: 'billing-service'}})
            CREATE (new:Decision {{id: 'decision-private-retries', name: 'Move retries to a private queue', scope: '{PRIVATE}', decided_at: 1792000000000}}),
                   (new)-[:SUPERSEDES]->(old), (new)-[:ABOUT]->(billing)
            """
        )

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def test_the_invoice_worker_reading_list_nearest_first_with_its_gaps(self):
        result = _read("what should I read before touching the invoice worker")
        self.assertTrue(result.found)
        self.assertEqual(result.answer, INVOICE_WORKER)
        cites = _cites(result)
        for line in (
            "Lena Fischer -[AUTHORED]-> Invoice format spec",
            "Invoice format spec -[DESCRIBES]-> Invoice Worker",
            "Invoice Worker -[DEPENDS_ON]-> Billing Service",
            "Billing Service -[DEPENDS_ON]-> Auth Service",
            "Auth session design -[DESCRIBES]-> Auth Service",
            "Retry renewal webhooks inside the billing service -[ABOUT]-> Billing Service",
            "Retry renewal webhooks inside the billing service -[BASED_ON]-> TKT-102",
            "Lena Fischer -[OWNS]-> Notification Service",
            "Sara Lindqvist -[OWNS]-> Postgres",
        ):
            self.assertIn(line, cites)
        # Auth Service is 2 hops out through Billing and through Notification; the route by name wins.
        self.assertNotIn("Notification Service -[DEPENDS_ON]-> Auth Service", cites)
        self.assertTrue(all(step.from_id and step.to_id and step.from_label and step.to_label for step in result.path))

    def test_a_private_replacement_stands_only_for_a_reader_who_can_see_it(self):
        public = _read("what should I read before touching the invoice worker")
        self.assertIn("Standing decision: Retry renewal webhooks inside the billing service", public.answer)
        self.assertNotIn("private queue", public.answer)
        private = _read("what should I read before touching the invoice worker", ORG + [PRIVATE])
        self.assertIn("Standing decision: Move retries to a private queue (Billing Service, 14 Oct 2026).", private.answer)
        self.assertNotIn("Retry renewal webhooks", private.answer)
        self.assertEqual(private.answer.count("Move retries to a private queue"), 1)

    def test_the_memory_layer_reads_its_rfc_and_only_the_decision_that_stands(self):
        result = _read("reading list for the memory layer")
        self.assertTrue(result.answer.startswith("Before touching Memory Layer, read 1 document: RFC: memory layer on a graph store by Kofi Mensah (Memory Layer itself)."))
        self.assertIn("Standing decision: Move the memory layer off Postgres (Memory Layer, 15 Sep 2026), based on TKT-101 and the RFC: memory layer on a graph store.", result.answer)
        self.assertIn("Nothing documents Graph Store; ask Sara Lindqvist.", result.answer)
        self.assertNotIn("Keep notebook links in Postgres join tables", result.answer)
        self.assertNotIn("Keep notebook links in Postgres join tables", " ".join(_cites(result)))

    def test_nothing_documented_reads_by_kind_and_names_who_to_ask(self):
        renewals = _read("reading list for renewals")
        self.assertEqual(
            renewals.answer,
            "Nothing documents the Renewals process or what it depends on; ask Priya Shah and Noor Haddad. "
            "Standing decision: Sell annual plans only at renewal (the Renewals process, 1 Sep 2026), based on the Pricing playbook.",
        )
        self.assertIn("Renewals -[DEPENDS_ON]-> HubSpot", _cites(renewals))
        self.assertEqual(_read("docs before touching postgres").answer, "Nothing documents Postgres; ask Sara Lindqvist.")

    def test_a_name_not_in_the_graph_is_refused_quoting_only_the_name(self):
        miss = _read("what should I read before touching the payments gateway")
        self.assertFalse(miss.found)
        self.assertTrue(miss.answer.startswith("Nothing in the graph matches “payments gateway”"), miss.answer)
        self.assertEqual(miss.path, [])

    def test_the_reading_words_stay_out_of_the_resolver(self):
        # Stripped only from a reading-list question, so an Ops "Change management" process still resolves everywhere.
        self.assertEqual(resolve.core("change management"), "change management")
        self.assertEqual(resolve.core("price list before a change"), "price list before change")
        self.assertEqual(reading_list.named("reading list for change management"), "change management")
        self.assertEqual(reading_list.named("what should I read before touching the payments gateway"), "the payments gateway")
        self.assertEqual(reading_list.named("invoice worker"), "invoice worker")

    def test_it_is_a_read_tool_served_over_mcp(self):
        spec = catalogue.get_tool("reading_list")
        self.assertFalse(spec.needs_approval)
        self.assertEqual(spec.args_schema["required"], ["name"])
        self.assertIn("reading_list", mcp_tools.tool_names())

    def test_a_coding_agent_gets_the_same_pack_over_mcp(self):
        pack = mcp_tools.call_tool("reading_list", {"name": "invoice worker"})
        self.assertTrue(pack["found"])
        self.assertEqual(pack["answer"], INVOICE_WORKER)
        self.assertTrue(pack["path"])
        self.assertIn({"from": "Invoice Worker", "rel": "DEPENDS_ON", "to": "Billing Service"}, pack["path"])
        self.assertIn({"from": "Retry renewal webhooks inside the billing service", "rel": "BASED_ON", "to": "TKT-102"}, pack["path"])
        # MCP reads org_shared only: the private replacement never reaches an agent.
        self.assertNotIn("private queue", pack["answer"])
        missed = mcp_tools.call_tool("reading_list", {"name": "the payments gateway"})
        self.assertFalse(missed["found"])
        self.assertEqual(missed["candidates"], ["Billing Service", "API Gateway"])

    # --- in Slack ---------------------------------------------------------------------------------

    def test_in_slack_the_model_picks_it_and_the_reply_cites_the_path(self):
        with mock.patch.object(agent, "generate_object", return_value={"tool": "reading_list", "args": '{"name": "invoice worker"}'}):
            outcome = agent_turn.answer_turn(_turn("what should I read before I change the invoice worker"))
        self.assertEqual(outcome.status, "done")
        self.assertTrue(outcome.reply.startswith(INVOICE_WORKER), outcome.reply)
        for line in (
            "• Lena Fischer -[AUTHORED]-> Invoice format spec",
            "• Invoice Worker -[DEPENDS_ON]-> Billing Service",
            "• Retry renewal webhooks inside the billing service -[BASED_ON]-> Billing runbook",
            "• Sara Lindqvist -[OWNS]-> Postgres",
        ):
            self.assertIn(line, outcome.reply)
        self.assertIn("Tool `reading_list`, picked by the model", outcome.reply)

    def test_in_slack_without_a_model_the_keywords_pick_it(self):
        with mock.patch.object(agent, "generate_object", side_effect=ModelUnavailable("no key")):
            outcome = agent_turn.answer_turn(_turn("reading list for the memory layer"))
        self.assertIn("Before touching Memory Layer, read 1 document: RFC: memory layer on a graph store by Kofi Mensah", outcome.reply)
        self.assertIn("Tool `reading_list`, picked by keywords", outcome.reply)

    def test_in_a_thread_it_reads_up_on_what_the_thread_is_about(self):
        with mock.patch.object(agent, "generate_object", side_effect=ModelUnavailable("no key")):
            first = agent_turn.answer_turn(_turn("what breaks if billing goes down"))
            thread = [
                {"user": "ULENA", "text": "<@UBOT> what breaks if billing goes down", "ts": "1"},
                {"user": "UBOT", "bot_id": "B1", "text": first.reply, "ts": "2"},
                {"user": "ULENA", "text": "<@UBOT> what should I read before touching it", "ts": "3"},
            ]
            follow_up = agent_turn.answer_turn(_turn("what should I read before touching it", thread=thread))
        self.assertTrue(follow_up.reply.startswith("Before touching Billing Service, read 2 documents, nearest first:"), follow_up.reply)
        self.assertIn("Tool `reading_list`, picked by keywords. Subject from this thread.", follow_up.reply)
