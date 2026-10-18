"""pr_explain over this repo's own pull requests, backfilled from a fake GitHub, next to Quillfern.

The graph is production's legacy graph in miniature: Quillfern, then the Company Brain self-seed
merged next to it, then a GitHub backfill of four real pull requests from codesensei-tushar/graph_hacks
(``tests/fixtures/graph_hacks_prs.py``), then the self-seed again so its decisions link the pull
requests they cite. The real pull requests had no formal reviews and closed no issue, so the
reviews, the issue and the pull request that closes it below are made up for the test and say so.
Needs FalkorDB on localhost:6379 (``docker compose up -d``).
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.github import backfill, ingest, repos  # noqa: E402
from src.domain.knowledge import ask_api, ask_service, catalogue, mcp_tools, pr_explain  # noqa: E402
from src.seed import company_brain, quillfern, seed  # noqa: E402
from tests.fixtures import graph_hacks_prs as fx  # noqa: E402

GRAPH = f"company_pr_explain_test{tests.TEST_GRAPH_SUFFIX}"
PUBLIC = ["org_shared"]
TUSHAR, YASH, OM = fx.USERS["codesensei-tushar"], fx.USERS["noobsurd11"], fx.USERS["omsherikar"]
# Made up for the test: the real pull requests had no formal review.
REVIEWS = {207: [{"user": TUSHAR, "state": "APPROVED"}, {"user": OM, "state": "COMMENTED"}]}
# Made up for the test: an issue, and a pull request that closes it.
ISSUE = {"number": 900, "title": "Scheduler runs a job twice after a restart", "body": "", "state": "closed", "user": TUSHAR, "assignees": [YASH]}
CLOSER = {"number": 901, "title": "Release a job's lease on restart", "body": "Fixes #900.", "state": "open", "user": YASH, "requested_reviewers": [TUSHAR]}
PRIVATE_REPO = {"id": 555, "full_name": "codesensei-tushar/brain-private", "private": True}


def _ask(question: str, scopes=None):
    tool, args = ask_service.route(question)
    return tool, catalogue.get_tool(tool).run(args, list(scopes or PUBLIC))


def _cites(result) -> list[str]:
    return [step.cite() for step in result.path]


class PrExplainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        seed.seed_company(company_brain)
        fake = fx.FakeGitHub(reviews=REVIEWS, issues=[ISSUE])
        with mock.patch.object(backfill, "client_for", return_value=fake):
            backfill.backfill_installation(7)
        ingest.upsert_pull_request(fx.REPO, CLOSER, files=[{"filename": "src/core/scheduler.py"}], reviews=[])
        # A private repo with its own #207, readable only through its scope.
        repos.upsert_repo(PRIVATE_REPO, 7)
        ingest.upsert_pull_request(PRIVATE_REPO, {"number": 207, "title": "Private work", "state": "open", "user": OM}, files=[{"filename": "src/connectors/sync.py"}])
        # A Jira ticket in org_internal that #207 references, as the Jira connector would link it.
        _, graph = falkordb_client.connect()
        graph.query(
            """
            MATCH (pr:PullRequest {id: $pr})
            CREATE (t:Ticket {id: 'jira:PAY-12', name: 'PAY-12', title: 'Billing exports time out', state: 'open', source: 'jira', scope: 'org_internal'})
            CREATE (pr)-[:REFERENCES]->(t)
            """,
            {"pr": ingest.pull_request_id(fx.REPO["id"], 207)},
        )
        seed.seed_company(company_brain)  # decisions link the pull requests they cite
        ask_api.ANSWERS.clear()

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            ask_api.ANSWERS.clear()
            for patch in cls._patches:
                patch.stop()

    def test_what_did_a_pull_request_do(self):
        tool, result = _ask("what did PR #207 do")
        self.assertEqual((tool, result.tool, result.found), ("pr_explain", "pr_explain", True))
        self.assertTrue(
            # merged_at from the fixture (2026-10-18T17:15:37Z), now that ingest stores it.
            result.answer.startswith("graph_hacks#207 “I1: batched connector writes, content-hash skip, one generation bump per page” is merged 18 Oct 2026, 17:15 UTC."),
            result.answer,
        )
        self.assertIn("Written by Yash Patil (@noobsurd11).", result.answer)
        self.assertIn("Reviewed by Om Sherikar (commented) and Tushar Umbarkar (approved).", result.answer)
        self.assertIn("What it does: The connector write path for every org's graph", result.answer)
        self.assertNotIn("Size", result.answer)
        self.assertNotIn("| page |", result.answer, "tables are not prose")
        self.assertIn("Decisions based on it: Connector writes are batched and skip unchanged items by content hash.", result.answer)
        cites = _cites(result)
        for cite in (
            "Yash Patil -[AUTHORED]-> graph_hacks#207",
            "Tushar Umbarkar -[REVIEWED]-> graph_hacks#207",
            "Om Sherikar -[REVIEWED]-> graph_hacks#207",
            "graph_hacks#207 -[TOUCHES]-> Connector framework",
            "graph_hacks#207 -[TOUCHES]-> Jira connector",
            "graph_hacks#207 -[TOUCHES]-> HubSpot connector",
            "Console -[DEPENDS_ON]-> Connector framework",
            "Connector writes are batched and skip unchanged items by content hash -[BASED_ON]-> graph_hacks#207",
        ):
            self.assertIn(cite, cites)
        # Tests, docs and nothing else map to a service; Quillfern's services are never touched.
        touched = {step.to_name for step in result.path if step.rel == "TOUCHES"}
        self.assertEqual(touched, {"Connector framework", "FalkorDB", "HubSpot connector", "Jira connector", "Knowledge tools"})

    def test_when_it_merged_is_said_once_ingest_keeps_it(self):
        _, graph = falkordb_client.connect()
        node = ingest.pull_request_id(fx.REPO["id"], 216)
        graph.query("MATCH (p:PullRequest {id: $id}) SET p.merged_at = '2026-10-18T17:29:13Z'", {"id": node})
        self.addCleanup(graph.query, "MATCH (p:PullRequest {id: $id}) REMOVE p.merged_at", {"id": node})
        _, result = _ask("explain PR 216")
        self.assertIn("is merged 18 Oct 2026, 17:29 UTC.", result.answer)

    def test_who_did_it_leads_with_the_author(self):
        for question in ("who did #216", "who wrote PR 216", "who authored graph_hacks#216"):
            with self.subTest(question=question):
                tool, result = _ask(question)
                self.assertEqual(tool, "pr_explain")
                self.assertTrue(result.answer.startswith("graph_hacks#216 was written by Yash Patil (@noobsurd11)."), result.answer)
                self.assertIn("No one has reviewed graph_hacks#216 on GitHub.", result.answer)

    def test_what_it_affects_leads_with_the_services_and_one_hop_out(self):
        tool, result = _ask("what does PR #216 affect")
        self.assertEqual(tool, "pr_explain")
        self.assertTrue(
            result.answer.startswith(
                "graph_hacks#216 touches Scheduler and Slack layer. One hop out, Connector framework (on Scheduler), "
                "Console (on Slack layer), GitHub ingest (on Scheduler) and Turn engine (on Scheduler) depend on them."
            ),
            result.answer,
        )
        self.assertIn("GitHub ingest -[DEPENDS_ON]-> Scheduler", _cites(result))

    def test_who_reviewed_it(self):
        _, result = _ask("who reviewed graph_hacks#207")
        self.assertTrue(result.answer.startswith("Reviewed by Om Sherikar (commented) and Tushar Umbarkar (approved)."), result.answer)
        _, waiting = _ask("who reviewed PR 901")
        self.assertTrue(waiting.answer.startswith("No one has reviewed graph_hacks#901 on GitHub. Review requested from Tushar Umbarkar (@codesensei-tushar)."), waiting.answer)
        self.assertIn("Tushar Umbarkar -[REVIEW_REQUESTED]-> graph_hacks#901", _cites(waiting))

    def test_what_it_closes_and_references_within_the_askers_scopes(self):
        _, closer = _ask("what tickets does PR 901 close")
        self.assertTrue(closer.answer.startswith("Closes graph_hacks#900 “Scheduler runs a job twice after a restart” (closed)."), closer.answer)
        self.assertIn("graph_hacks#901 -[CLOSES]-> graph_hacks#900", _cites(closer))
        self.assertIn("is open", closer.answer)
        # PAY-12 is a Jira ticket in org_internal: a Slack asker reads it, the public page does not.
        _, public = _ask("what does PR 207 close")
        self.assertTrue(public.answer.startswith("graph_hacks#207 closes and references no ticket."), public.answer)
        self.assertNotIn("PAY-12", public.answer + " ".join(_cites(public)))
        _, internal = _ask("what does PR 207 close", PUBLIC + ["org_internal"])
        self.assertTrue(internal.answer.startswith("References PAY-12 “Billing exports time out” (open)."), internal.answer)

    def test_a_private_repo_is_seen_only_through_its_scope(self):
        _, public = _ask("what did PR 207 do")
        self.assertNotIn("brain-private", public.answer)
        _, both = _ask("what did PR 207 do", PUBLIC + [repos.repo_scope(PRIVATE_REPO)])
        self.assertFalse(both.found)
        self.assertIn("More than one pull request is #207", both.answer)
        _, named = _ask("what did graph_hacks#207 do", PUBLIC + [repos.repo_scope(PRIVATE_REPO)])
        self.assertTrue(named.found)
        self.assertTrue(named.answer.startswith("graph_hacks#207 “I1"), named.answer)
        _, private = _ask("what did brain-private#207 do", PUBLIC + [repos.repo_scope(PRIVATE_REPO)])
        self.assertTrue(private.answer.startswith("brain-private#207 “Private work” is open."), private.answer)
        _, hidden = _ask("what did brain-private#207 do")
        self.assertFalse(hidden.found)
        self.assertNotIn("Private work", hidden.answer)

    def test_a_missing_pull_request_or_an_issue_number(self):
        _, missing = _ask("what did PR #4040 do")
        self.assertFalse(missing.found)
        self.assertTrue(missing.answer.startswith("I can't find pull request #4040 in the graph you can read."), missing.answer)
        self.assertEqual(missing.path, [])
        # graph_hacks#900 is an issue: answered as the ticket it is.
        _, issue = _ask("what is graph_hacks#900")
        self.assertEqual(issue.tool, "ticket_status")
        self.assertIn("Scheduler runs a job twice after a restart", issue.answer)

    def test_who_should_know_and_what_changed_keep_their_tools(self):
        tool, result = _ask("who should know about PR #216")
        self.assertEqual(tool, "who_should_know")
        self.assertTrue(result.found, result.answer)
        tool, changed = _ask("what changed in the scheduler this week")
        self.assertEqual(tool, "what_changed")
        self.assertIn("graph_hacks#216", changed.answer)

    def test_the_public_page_and_mcp_answer_it(self):
        answer = ask_api.answer_question("what did PR #207 do")
        self.assertEqual((answer.tool, answer.found), ("pr_explain", True))
        self.assertTrue(answer.path)
        self.assertIn("pr_explain", mcp_tools.tool_names())
        called = mcp_tools.call_tool("pr_explain", {"name": "graph_hacks#216"})
        self.assertTrue(called["found"])
        self.assertTrue(called["answer"].startswith("graph_hacks#216 “I2: scheduler worker pool"), called["answer"])

    def test_the_owner_of_what_it_touches_is_one_question_away(self):
        _, owner = _ask("who owns the console")
        self.assertTrue(owner.answer.startswith("Console is owned by Yash Patil"), owner.answer)
        self.assertEqual(pr_explain.TOOL, "pr_explain")


if __name__ == "__main__":
    unittest.main()
