"""The production recording script (docs/STRATEGY.md) against the Company Brain self-seed, no model.

Seeds ``src/seed/company_brain.py`` into a throwaway graph, adds one public and one private
pull request like the GitHub App would, then plays the steps through the keyword router
and the catalogue exactly as a no-key deployment answers them. Every step must answer
with the expected tool and cite a path, or refuse with the nearest names.
"""

import re
import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import agent, ask_service, catalogue, proof  # noqa: E402
from src.domain.knowledge.dto import ToolResult  # noqa: E402
from src.domain.slack import compute  # noqa: E402
from src.seed import company_brain, seed  # noqa: E402

GRAPH = f"company_production_demo_test{tests.TEST_GRAPH_SUFFIX}"
ORG = ["org_shared"]
PRIVATE = "github_repo_9100"
BOT = frozenset({"UBOT"})

STEPS = {
    1: "who owns the memory layer, and who else has worked on it",
    2: "why do we store memory in FalkorDB instead of a hosted memory API",
    3: "why did we cut vector recall",
    4: "what breaks if the claude api goes down, one hop",
    6: "who owns the linear integration",
    7: "who knows github ingest",
}


def _ask(question: str, scopes=None, **extra) -> tuple[str, ToolResult]:
    tool, args = ask_service.route(question)
    args.update(extra)
    return tool, catalogue.get_tool(tool).run(args, list(scopes or ORG))


def _cites(result: ToolResult) -> list[str]:
    return [step.cite() for step in result.path]


class ProductionDemoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(company_brain)
        _, graph = falkordb_client.connect()
        graph.query(
            """
            MATCH (tushar:Person {id: 'tushar'}), (memory:Service {id: 'memory-layer'}), (github:Service {id: 'github-ingest'})
            CREATE (me:GitHubUser {id: 'gh-tushar', login: 'codesensei-tushar', name: 'codesensei-tushar'})-[:IS]->(tushar),
                   (ext:GitHubUser {id: 'gh-ext', login: 'ext-dev', name: 'ext-dev'}),
                   (public:PullRequest {id: 'pr-public-1', name: 'graph_hacks#1', scope: 'org_shared'})-[:TOUCHES]->(memory),
                   (private:PullRequest {id: 'pr-private-3', name: 'brain-private#3', scope: $private})-[:TOUCHES]->(github),
                   (me)-[:AUTHORED]->(public), (ext)-[:REVIEWED]->(public),
                   (me)-[:AUTHORED]->(private)
            """,
            {"private": PRIVATE},
        )

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def test_every_step_answers_with_its_tool_and_a_path_or_refuses(self):
        expected = {1: "who_knows", 2: "decision_trace", 3: "decision_trace", 4: "blast_radius", 6: "who_owns", 7: "who_knows"}
        for step, question in STEPS.items():
            with self.subTest(step=step):
                tool, result = _ask(question)
                self.assertEqual(tool, expected[step])
                self.assertEqual(result.tool, expected[step])
                if step == 6:
                    self.assertFalse(result.found)
                    self.assertEqual(result.path, [])
                else:
                    self.assertTrue(result.found, result.answer)
                    self.assertTrue(result.path, result.answer)

    def test_1_owner_then_the_pull_requests_that_touched_the_code(self):
        _, result = _ask(STEPS[1])
        self.assertTrue(result.answer.startswith("Memory layer is owned by Tushar. Ask first: Tushar (owns it, wrote graph_hacks#1)"), result.answer)
        cites = _cites(result)
        self.assertEqual(cites[0], "Tushar -[OWNS]-> Memory layer")
        self.assertIn("Tushar -[AUTHORED]-> graph_hacks#1", cites)
        self.assertIn("graph_hacks#1 -[TOUCHES]-> Memory layer", cites)
        self.assertIn("@ext-dev -[REVIEWED]-> graph_hacks#1", cites)
        self.assertEqual(result.candidates, [], "a question about the memory layer names nothing else")

    def test_2_the_decision_its_author_sources_and_the_conversation(self):
        _, result = _ask(STEPS[2])
        decision = "Store memory in FalkorDB instead of a hosted memory API"
        self.assertTrue(result.answer.startswith(decision), result.answer)
        self.assertIn("Decided 15 Oct 2026.", result.answer)
        self.assertIn("Proposed by Tushar.", result.answer)
        cites = _cites(result)
        for cite in (
            f"Tushar -[PROPOSED]-> {decision}",
            f"{decision} -[BASED_ON]-> Problem statement",
            f"{decision} -[BASED_ON]-> Slack guide",
            f"{decision} -[ABOUT]-> Memory layer",
        ):
            self.assertIn(cite, cites)
        self.assertTrue(any(cite.endswith(f"-[ABOUT]-> {decision}") and "chat" in cite.lower() for cite in cites), cites)

    def test_3_the_superseded_decision_pair_traces_with_both_dates(self):
        _, result = _ask(STEPS[3])
        current, replaced = "Recall follows MENTIONS edges, vector search cut", "Hybrid recall with a vector index on Memory.embedding"
        self.assertIn(current, result.answer)
        self.assertIn(f"This replaced *{replaced}* (decided 17 Oct 2026)", result.answer)
        self.assertIn("Decided 17 Oct 2026.", result.answer)
        self.assertGreaterEqual(result.answer.count("17 Oct 2026"), 2)
        self.assertIn(f"{current} -[SUPERSEDES]-> {replaced}", _cites(result))
        self.assertIn(f"{current} -[BASED_ON]-> docs/STRATEGY.md", _cites(result))

    def test_4_depth_one_differs_from_depth_three_and_the_thread_follow_up_reaches_three(self):
        _, one = _ask(STEPS[4])
        self.assertEqual(one.depth, 1)
        self.assertEqual(_cites(one), ["LLM gateway -[DEPENDS_ON]-> Claude API"])
        # The exact opening of the reply, so the recording script and the code cannot drift apart again.
        self.assertTrue(
            one.answer.startswith(
                "If Claude API goes down, these stop with it: LLM gateway (1 hop). "
                "4 more break further out (Memory layer at depth 2, Slack workspace at depth 2, Turn engine at depth 2 "
                "and Slack events API at depth 3); ask with more hops."
            ),
            one.answer,
        )

        _, three = _ask("what breaks if the claude api goes down", depth=3)
        self.assertEqual(three.depth, 3)
        affected_one = {step.from_name for step in one.path}
        affected_three = {step.from_name for step in three.path}
        self.assertLess(affected_one, affected_three)
        for cite in (
            "Slack workspace -[DEPENDS_ON]-> LLM gateway",
            "Turn engine -[DEPENDS_ON]-> LLM gateway",
            "Memory layer -[DEPENDS_ON]-> LLM gateway",
            "Slack events API -[DEPENDS_ON]-> Slack workspace",
        ):
            self.assertIn(cite, _cites(three))
        self.assertIn("Depth 3 reached 5 services; nothing new past depth 3.", three.answer)

        # "and three hops out" in the thread, with no model, takes the subject from the first question.
        thread = [
            {"user": "USARA", "text": f"<@UBOT> {STEPS[4]}", "ts": "1"},
            {"user": "UBOT", "bot_id": "B1", "text": one.answer + "\n\n*Path* (`blast_radius`, depth 1, 1 edge)\n\n_Tool `blast_radius`, picked by keywords. Read from: public channels_", "ts": "2"},
        ]
        result, choice = agent.run_choice(agent.choose_with_keywords("and three hops out"), "and three hops out", ORG, thread, BOT)
        self.assertEqual((choice.tool, choice.via, result.depth), ("blast_radius", "thread", 3))
        self.assertIn("Slack events API -[DEPENDS_ON]-> Slack workspace", _cites(result))

    def test_5_the_card_names_the_service_the_write_lands_and_the_follow_up_finds_the_owner(self):
        title = "scheduler jobs run twice after a restart"
        preview = catalogue.get_tool("ingest_ticket").preview({"title": title}, ORG)
        self.assertEqual(preview, f"File a ticket titled *{title}* in the company graph, linked to *Scheduler*")

        text = compute.execute_approved_action(compute.TICKET_TOOL_NAME, {"title": title}, "org_shared")
        ticket = re.search(r"Filed \*(TKT-\d+)\* about Scheduler", text).group(1)
        self.assertIn(f"• {ticket} -[ABOUT]-> Scheduler", text)
        self.assertIn("• Tushar -[OWNS]-> Scheduler", text)

        thread = [
            {"user": "UKOFI", "text": f"<@UBOT> ticket: {title}", "ts": "1"},
            {"user": "UBOT", "bot_id": "B1", "text": text + "\n\n_Tool `ingest_ticket`, run after your approval. Read from: public channels_", "ts": "2"},
        ]
        result, choice = agent.run_choice(agent.choose_with_keywords("who should look at it"), "who should look at it", ORG, thread, BOT)
        self.assertEqual((choice.tool, choice.via), ("who_knows", "thread"))
        # A ticket leads with what its tracker shows: title, status and assignee.
        self.assertEqual(result.answer, f"{ticket} is “{title}”: open, assigned to no one. About Scheduler. Ask first: Tushar (owns Scheduler).")
        self.assertIn(f"{ticket} -[ABOUT]-> Scheduler", _cites(result))
        self.assertIn("Tushar -[OWNS]-> Scheduler", _cites(result))

    def test_6_something_not_in_the_graph_refuses_with_the_nearest_names(self):
        _, result = _ask(STEPS[6])
        self.assertFalse(result.found)
        self.assertTrue(result.candidates, result.answer)
        self.assertTrue({"TKT-105", "GitHub ingest"} & set(result.candidates), result.candidates)
        self.assertIn("Nothing in the graph matches “linear integration”", result.answer)
        self.assertIn("Nearest:", result.answer)

    def test_7_a_private_repo_shows_only_to_its_collaborators(self):
        _, outsider = _ask(STEPS[7])
        _, collaborator = _ask(STEPS[7], scopes=ORG + [PRIVATE])
        self.assertTrue(outsider.found and collaborator.found)
        self.assertNotIn("brain-private#3", " ".join(_cites(outsider)) + outsider.answer)
        self.assertIn("brain-private#3 -[TOUCHES]-> GitHub ingest", _cites(collaborator))
        self.assertIn("Tushar -[AUTHORED]-> brain-private#3", _cites(collaborator))
        self.assertIn("wrote brain-private#3", collaborator.answer)

    def test_8_the_proof_report_on_the_production_seed(self):
        report = proof.run_proof(company="company-brain")
        self.assertEqual((report.collapsed, report.total), (5, 6))
        self.assertEqual(report.rows[-1].verdict, "refused both ways")
        self.assertIn("payments gateway", report.rows[-1].with_graph.answer)
        for row in report.rows[:-1]:
            self.assertEqual(row.verdict, "collapsed", row.question)
