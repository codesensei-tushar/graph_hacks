"""A question in two parts answers both, on a real FalkorDB with the Quillfern seed: through the
keyword router (``ask_service.ask``), the ask page (``ask_api.answer_question``), the Slack turn
with no model, and the agent with a model choosing each part.

A rest that names nothing ("who to contact", "what breaks if it goes down") is asked about what
the first part resolved; one the graph cannot answer is still offered as the next question; a
question in one part comes back exactly as before.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import agent, ask_api, ask_service, compound  # noqa: E402
from src.domain.memory.read_scope import resolve_brain_read_container_tags  # noqa: E402
from src.domain.memory.writeback import SlackMemoryScope  # noqa: E402
from src.domain.slack import agent_turn  # noqa: E402
from src.domain.slack.compute import TurnRequest  # noqa: E402
from src.integrations.llm.model import ModelUnavailable  # noqa: E402
from src.integrations.slack.events import SlackEventInner  # noqa: E402
from src.integrations.slack.format import markdown_reply_blocks, plain_text_fallback  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_compound_test{tests.TEST_GRAPH_SUFFIX}"
ORG = ["org_shared"]
AUTH_DOWN = "If Auth Service goes down, these stop with it: API Gateway, Billing Service, Notification Service and Search Service (1 hop)"
AUTH_PEOPLE = "Auth Service is owned by Maya Ortiz. Ask first: Maya Ortiz"
CHANNEL = "CCOMPOUND"


def _turn(question: str, thread: list[dict]) -> TurnRequest:
    scope = SlackMemoryScope(kind="shared", channel_id=CHANNEL, channel_type="channel")
    event = SlackEventInner(type="app_mention", user="UALICE", channel=CHANNEL, channel_type="channel", text=question, ts="1792324800.000100")
    return TurnRequest(
        workspace=SimpleNamespace(team_id="T1", bot_user_id="UBOT"),
        event=event,
        question=question,
        raw_text=question,
        asker_slack_user_id="UALICE",
        asker_name="Alice",
        memory_scope=scope,
        read_tags=resolve_brain_read_container_tags(scope),
        thread=list(thread),
        channel=CHANNEL,
        thread_ts=event.ts,
        is_dm=False,
    )


class CompoundAnswerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        ask_api.ANSWERS.clear()

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in reversed(cls._patches):
                patch.stop()

    def setUp(self):
        patch = mock.patch.object(agent, "generate_object", side_effect=ModelUnavailable("no key"))
        patch.start()
        self.addCleanup(patch.stop)

    def assert_both(self, result, tools: list[str], questions: list[str], leads: list[str]):
        self.assertTrue(result.found, result.answer)
        self.assertEqual(result.tool, " + ".join(tools))
        self.assertEqual([part["question"] for part in result.parts], questions)
        self.assertEqual([part["tool"] for part in result.parts], tools)
        paragraphs = result.answer.split(compound.SEPARATOR)
        self.assertEqual(len(paragraphs), 2, result.answer)
        for paragraph, part, lead in zip(paragraphs, result.parts, leads, strict=True):
            self.assertEqual(paragraph, part["answer"])
            self.assertTrue(paragraph.startswith(lead), paragraph)
        self.assertNotIn("You also asked", result.answer)

    # --- the three questions ---------------------------------------------------------------------

    def test_what_if_auth_breaks_and_who_to_contact(self):
        question = "what if auth breaks and who to contact"
        result = ask_service.ask(question, ORG)
        self.assert_both(result, ["blast_radius", "who_knows"], ["what if auth breaks", "who to contact about Auth Service"], [AUTH_DOWN, AUTH_PEOPLE])
        cited = {step.cite() for step in result.path}
        # The union of both walks: what depends on Auth Service, and the people who know it.
        self.assertIn("API Gateway -[DEPENDS_ON]-> Auth Service", cited)
        self.assertIn("Maya Ortiz -[OWNS]-> Auth Service", cited)
        self.assertIn("Arjun Mehta -[KNOWS]-> Auth Service", cited)
        blast = ask_service.ask("what if auth breaks", ORG)
        people = ask_service.ask("who knows Auth Service", ORG)
        self.assertEqual(result.path, blast.path + [step for step in people.path if step not in blast.path])
        self.assertEqual(result.depth, blast.depth)

    def test_who_owns_billing_and_what_breaks_if_it_goes_down(self):
        result = ask_service.ask("who owns billing and what breaks if it goes down", ORG)
        self.assert_both(
            result,
            ["who_owns", "blast_radius"],
            ["who owns billing", "what breaks if Billing Service goes down"],
            ["Billing Service is owned by Arjun Mehta.", "If Billing Service goes down, these stop with it: API Gateway and Invoice Worker (1 hop)."],
        )
        cited = {step.cite() for step in result.path}
        self.assertIn("Arjun Mehta -[OWNS]-> Billing Service", cited)
        self.assertIn("Invoice Worker -[DEPENDS_ON]-> Billing Service", cited)

    def test_what_breaks_if_postgres_goes_down_and_who_should_know(self):
        result = ask_service.ask("what breaks if postgres goes down and who should know", ORG)
        self.assert_both(
            result,
            ["blast_radius", "who_should_know"],
            ["what breaks if postgres goes down", "who should know about Postgres"],
            ["If Postgres goes down, these stop with it: Auth Service and Billing Service (1 hop)", "Postgres is owned by Sara Lindqvist."],
        )

    def test_who_to_ping_goes_to_the_people_not_the_blast_radius(self):
        for question, rest in (
            ("what if auth breaks? who should I ping", "who should I ping about Auth Service"),
            ("what breaks if auth goes down, also who do we talk to", "who do we talk to about Auth Service"),
            ("what if auth breaks and who to ping about the outage", "who to ping about the outage (Auth Service)"),
        ):
            with self.subTest(question=question):
                result = ask_service.ask(question, ORG)
                self.assert_both(result, ["blast_radius", "who_knows"], [ask_service.split_compound(question)[0], rest], [AUTH_DOWN, AUTH_PEOPLE])

    def test_a_rest_that_names_its_own_thing_is_asked_about_it(self):
        result = ask_service.ask("who owns billing and what breaks if auth goes down", ORG)
        self.assert_both(result, ["who_owns", "blast_radius"], ["who owns billing", "what breaks if auth goes down"], ["Billing Service is owned by Arjun Mehta.", AUTH_DOWN])

    def test_the_same_tool_twice_is_named_once(self):
        result = ask_service.ask("what does the invoice worker depend on and what depends on it", ORG)
        self.assertEqual(result.tool, "blast_radius")
        self.assertEqual([part["question"] for part in result.parts], ["what does the invoice worker depend on", "what depends on Invoice Worker"])
        self.assertTrue(result.answer.startswith("Invoice Worker depends on Billing Service and Notification Service (1 hop)"), result.answer)
        self.assertTrue(result.answer.endswith("Nothing in the graph depends on Invoice Worker."), result.answer)

    # --- a part that cannot be answered ------------------------------------------------------------

    def test_a_rest_the_graph_cannot_answer_is_offered_next(self):
        result = ask_service.ask("who owns billing and what breaks if the payments gateway goes down", ORG)
        self.assertEqual(result.tool, "who_owns")
        self.assertEqual(result.parts, [])
        self.assertEqual(
            result.answer,
            "Billing Service is owned by Arjun Mehta. Run by the Payments team. You also asked “what breaks if the payments gateway goes down”: ask it on its own for that answer.",
        )

    def test_a_first_part_that_names_nothing_leaves_the_rest_unasked(self):
        # Nothing to carry over: "who to contact" is never searched as words.
        alone = ask_service.ask("who owns the payments gateway", ORG)
        result = ask_service.ask("who owns the payments gateway and who to contact", ORG)
        self.assertFalse(result.found)
        self.assertEqual((result.tool, result.answer, result.parts), (alone.tool, alone.answer, []))

    def test_a_first_part_the_graph_lacks_still_answers_a_rest_that_names_its_thing(self):
        result = ask_service.ask("who owns the payments gateway and what breaks if auth goes down", ORG)
        self.assertTrue(result.found)
        self.assertEqual([part["found"] for part in result.parts], [False, True])
        self.assertTrue(result.answer.split(compound.SEPARATOR)[1].startswith(AUTH_DOWN), result.answer)

    # --- one part: exactly as before ---------------------------------------------------------------

    def test_a_question_in_one_part_is_unchanged(self):
        for question in ("what if auth breaks", "who owns billing", "who owns billing, and who else has worked on it", "billing is failing, what changed"):
            with self.subTest(question=question):
                tool, args = ask_service.route(question)
                result = ask_service.ask(question, ORG)
                self.assertEqual(result.tool, tool)
                self.assertEqual(result.parts, [])
                self.assertNotIn("parts", result.to_dict())
                ran, _choice = agent.run_choice(agent.ToolChoice(tool, args, "caller"), question, ORG)
                self.assertEqual(result.to_dict(), ran.to_dict())
                page = ask_api.answer_question(question).to_dict()
                self.assertNotIn("parts", page)
                self.assertEqual(page["answer"], result.answer)

    # --- the page and Slack -----------------------------------------------------------------------

    def test_the_page_gets_both_parts(self):
        page = ask_api.answer_question("what if auth breaks and who to contact").to_dict()
        self.assertEqual(page["tool"], "blast_radius + who_knows")
        self.assertTrue(page["found"])
        self.assertEqual([part["tool"] for part in page["parts"]], ["blast_radius", "who_knows"])
        self.assertEqual(page["answer"], compound.SEPARATOR.join(part["answer"] for part in page["parts"]))
        names = {node["name"] for node in page["nodes"]}
        self.assertTrue({"Auth Service", "API Gateway", "Maya Ortiz", "Arjun Mehta"} <= names, names)
        self.assertEqual(len(page["edges"]), len(page["path"]))

    def test_slack_answers_both_in_two_sections_and_a_follow_up_reads_the_footer(self):
        thread = [{"user": "UALICE", "text": "<@UBOT> what if auth breaks and who to contact", "ts": "1"}]
        reply = agent_turn.answer_turn(_turn("what if auth breaks and who to contact", thread)).reply or ""
        lines = reply.split("\n")
        self.assertEqual(lines[0], "*What if auth breaks?*")
        self.assertTrue(lines[1].startswith(AUTH_DOWN), reply)
        self.assertEqual(lines[2:4], ["", "*Who to contact about Auth Service?*"])
        self.assertTrue(lines[4].startswith(AUTH_PEOPLE), reply)
        self.assertIn("*Path* (`blast_radius + who_knows`, depth 2, ", reply)
        self.assertIn("• Maya Ortiz -[OWNS]-> Auth Service", reply)
        self.assertIn("• API Gateway -[DEPENDS_ON]-> Auth Service", reply)
        self.assertIn("_Tool `blast_radius + who_knows`, picked by keywords. Read from:", reply)
        self.assertNotIn("You also asked", reply)
        thread.append({"user": "UBOT", "bot_id": "B1", "text": plain_text_fallback(reply), "blocks": markdown_reply_blocks(reply), "ts": "2"})
        self.assertEqual(agent.reply_tool(reply), "who_knows")

    def test_the_agent_chooses_each_part_with_the_model(self):
        def choose(request):
            asked = request.prompt.split("\n\nQuestion: ", 1)[1].split("\n\n", 1)[0]
            if asked == "what if auth breaks":
                return {"tool": "blast_radius", "args": {"name": "auth"}}
            if asked == "who to contact":
                return {"tool": "who_knows", "args": {"name": "who to contact"}}
            # The whole question: the model answers only one part of it.
            return {"tool": "who_knows", "args": {"name": "auth"}}

        question = "what if auth breaks and who to contact"
        with mock.patch.object(agent, "generate_object", side_effect=choose):
            choice = agent.choose_tool(question, scopes=ORG)
            self.assertEqual(choice.via, "model")
            result, ran = agent.run_choice(choice, question, ORG)
        self.assertEqual(ran.tool, "blast_radius")
        self.assert_both(result, ["blast_radius", "who_knows"], ["what if auth breaks", "who to contact about Auth Service"], [AUTH_DOWN, AUTH_PEOPLE])

    def test_a_tool_named_outright_answers_the_whole_question(self):
        # A caller that names the tool (the page's tool picker, MCP) asks one tool at a time.
        question = "what if auth breaks and who to contact"
        result, _choice = agent.run_choice(agent.ToolChoice("who_knows", {"name": question}, "caller"), question, ORG)
        self.assertEqual(result.tool, "who_knows")
        self.assertEqual(result.parts, [])


if __name__ == "__main__":
    unittest.main()
