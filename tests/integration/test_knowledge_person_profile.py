"""``person_profile`` on a real FalkorDB: plain Quillfern, then with its scripted GitHub history.

It is a tool of its own, and ``profile_if_person`` is what answers instead of a refusal when a
question names a person ("who is arjun"), wherever who_knows, who_owns, onboarding_brief or
decision_trace would refuse it.
"""

import dataclasses
import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import ask_api, ask_service, bus_factor, catalogue, fallback, mcp_tools, person_profile  # noqa: E402
from src.domain.knowledge.resolve import Entity  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402
from src.seed import quillfern_workspace as script  # noqa: E402
from scripts.replay.driver import load_github_history  # noqa: E402

GRAPH = f"company_person_profile_test{tests.TEST_GRAPH_SUFFIX}"
GITHUB_GRAPH = f"company_person_profile_github_test{tests.TEST_GRAPH_SUFFIX}"
ORG = ["org_shared"]
PRIVATE = "slack_channel_C9"
BILLING_INTERNAL = "github_repo_9002"

ARJUN = (
    "Arjun Mehta is the Billing engineer on Payments. "
    "Owns Billing Service (2 things depend on it) and knows Auth Service (5 depend on it). "
    "Wrote the Billing runbook. "
    "Proposed *Retry renewal webhooks inside the billing service* (20 Sep 2026, still standing, approved by Tomas Reyes). "
    "Works most with Sara Lindqvist (Billing Service, Auth Service) and Tomas Reyes (Billing Service, the billing retries decision). "
    "1 thing rests on Arjun alone; ask “what if Arjun leaves”."
)


def _ask(question: str, scopes=None):
    return ask_service.ask(question, scopes or ORG)


def _profile(question: str, scopes=None):
    return catalogue.get_tool("person_profile").run({"name": question}, scopes or ORG)


def _cites(result) -> list[str]:
    return [step.cite() for step in result.path]


class _Graph(unittest.TestCase):
    graph_name = GRAPH

    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=cls.graph_name),
            mock.patch.object(seed, "graph_name", return_value=cls.graph_name),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        cls.seed_more()
        ask_api.ANSWERS.clear()

    @classmethod
    def seed_more(cls):
        pass

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(cls.graph_name).delete()
        finally:
            ask_api.ANSWERS.clear()
            for patch in cls._patches:
                patch.stop()


class PersonProfileTest(_Graph):
    def test_who_is_arjun_is_told_from_edges(self):
        result = _profile("who is arjun")
        self.assertEqual((result.tool, result.found), ("person_profile", True))
        self.assertEqual(result.answer, ARJUN)
        self.assertEqual(
            _cites(result),
            [
                "Arjun Mehta -[MEMBER_OF]-> Payments",
                "Arjun Mehta -[OWNS]-> Billing Service",
                "Arjun Mehta -[KNOWS]-> Auth Service",
                "Arjun Mehta -[AUTHORED]-> Billing runbook",
                "Arjun Mehta -[PROPOSED]-> Retry renewal webhooks inside the billing service",
                "Tomas Reyes -[APPROVED]-> Retry renewal webhooks inside the billing service",
                "Sara Lindqvist -[KNOWS]-> Billing Service",
                "Tomas Reyes -[WORKED_ON]-> Billing Service",
            ],
        )
        self.assertTrue(all(step.from_id and step.to_id and step.from_label and step.to_label for step in result.path))
        # What Arjun said, through his Slack account.
        self.assertEqual(
            [(memory.learned_at, memory.author, memory.text) for memory in result.memories],
            [("2026-09-20", "Arjun Mehta", "billing now retries a failed renewal webhook once, idempotently, so TKT-102 stops charging twice")],
        )

    def test_a_question_that_names_a_person_gets_the_profile_instead_of_a_refusal(self):
        for question, name in (
            ("who is arjun", "Arjun Mehta"),
            ("who is Arjun Mehta", "Arjun Mehta"),
            ("what does sara own", "Sara Lindqvist"),
            ("what is kofi working on", "Kofi Mensah"),
            ("what does maya know about", "Maya Ortiz"),
            ("who works with lena", "Lena Fischer"),
            ("what has maya decided", "Maya Ortiz"),
            ("what does lena fischer work on", "Lena Fischer"),
        ):
            with self.subTest(question=question):
                # End to end: the keyword router picks who_knows, who_owns, onboarding_brief or
                # decision_trace, which would refuse, and fallback.before_refusing answers with the profile.
                result = _ask(question)
                self.assertEqual((result.tool, result.found), ("person_profile", True))
                self.assertTrue(result.answer.startswith(f"{name} is the "), result.answer)
        self.assertEqual(_ask("who is arjun").answer, ARJUN)
        # A refusal that names no person comes back as it was, candidates included.
        gateway = _ask("who knows the payments gateway")
        self.assertIs(person_profile.profile_if_person("who knows the payments gateway", ORG, gateway), gateway)
        self.assertIsNone(person_profile.profile_or_none("who knows the payments gateway", ORG))
        self.assertEqual(fallback.STEPS[0].__name__, "_person")

    def test_refusals_that_name_no_person_are_unchanged(self):
        gateway = _ask("who knows the payments gateway")
        self.assertEqual((gateway.tool, gateway.found, gateway.candidates), ("who_knows", False, ["Billing Service", "API Gateway"]))
        self.assertTrue(gateway.answer.startswith("Nothing in the graph matches “payments gateway” as one thing."))
        # A "why" asks for a reason the graph does not have, so naming a person does not turn it into a profile.
        kafka = _ask("why did arjun pick kafka")
        self.assertEqual((kafka.tool, kafka.found), ("decision_trace", False))
        self.assertEqual(kafka.answer, "Nothing in the graph matches “arjun pick kafka”. Name a decision that exists in the graph.")
        zed = _ask("what if zed leaves")
        self.assertEqual((zed.tool, zed.answer), ("if_person_leaves", "Nothing in the graph matches “zed”. Name a person that exists in the graph."))
        self.assertEqual(_ask("who is zed").answer, "Nothing in the graph matches “zed”. Name a service, document, or ticket that exists in the graph.")
        self.assertEqual(catalogue.get_tool("person_profile").run({"name": "zed"}, ORG).answer, "Nothing in the graph matches “zed”. Name a person that exists in the graph.")

    def test_a_replaced_decision_says_what_replaced_it(self):
        sara = _profile("who is sara").answer
        self.assertIn(
            "Proposed *Keep notebook links in Postgres join tables* (1 Jun 2026, replaced by *Move the memory layer off Postgres*, approved by Tomas Reyes).",
            sara,
        )
        self.assertIn("Owns Postgres (6 things depend on it) and Graph Store (2 depend on it), and knows Auth Service (5 depend on it) and Billing Service (2 depend on it).", sara)
        # Tomas approved all three; the two newest are told, the rest counted.
        tomas = _profile("who is tomas").answer
        self.assertIn(
            "Approved *Retry renewal webhooks inside the billing service* (20 Sep 2026, still standing, proposed by Arjun Mehta); "
            "approved *Move the memory layer off Postgres* (15 Sep 2026, still standing, proposed by Kofi Mensah); and 1 more.",
            tomas,
        )

    def test_what_rests_on_them_alone_agrees_with_if_person_leaves(self):
        _, graph = falkordb_client.connect()
        for person_id, name in (("arjun", "Arjun Mehta"), ("sara", "Sara Lindqvist"), ("kofi", "Kofi Mensah"), ("tomas", "Tomas Reyes")):
            with self.subTest(person=name):
                count = len(bus_factor.at_risk(graph, Entity(person_id, name, "Person"), ORG))
                first = name.split()[0]
                leaves = catalogue.get_tool("if_person_leaves").run({"name": name}, ORG).answer
                profile = _profile(f"who is {first}").answer
                if count:
                    things = "1 thing rests" if count == 1 else f"{count} things rest"
                    self.assertTrue(leaves.startswith(f"If {name} leaves, {things} on them alone"), leaves)
                    self.assertIn(f"{things} on {first} alone; ask “what if {first} leaves”.", profile)
                else:
                    self.assertIn("nothing rests on them alone", leaves)
                    self.assertTrue(profile.endswith(f"Nothing rests on {first} alone."), profile)

    def test_a_private_memory_is_told_only_with_its_scope(self):
        _, graph = falkordb_client.connect()
        graph.query(
            """
            MATCH (u:SlackUser {id: 'UARJUN'})
            CREATE (u)-[:SAID]->(:Memory {id: 'memory-private-refunds', text: 'refund ledger check ships friday', scope: $private,
                                          is_forgotten: false, created_at: 1791000000000})
            """,
            {"private": PRIVATE},
        )
        try:
            self.assertEqual(len(_profile("who is arjun").memories), 1)
            texts = [memory.text for memory in _profile("who is arjun", ORG + [PRIVATE]).memories]
            self.assertEqual(texts[0], "refund ledger check ships friday")
            self.assertEqual(len(texts), 2)
        finally:
            graph.query("MATCH (m:Memory {id: 'memory-private-refunds'}) DETACH DELETE m")

    def test_the_routing_fixes(self):
        approved = _ask("who approved moving the memory layer")
        self.assertEqual((approved.tool, approved.found), ("decision_trace", True))
        self.assertTrue(approved.answer.startswith("Move the memory layer off Postgres."), approved.answer)
        self.assertIn("Kofi Mensah -[PROPOSED]-> Move the memory layer off Postgres", _cites(approved))
        team = _ask("summarize the payments team")
        self.assertEqual((team.tool, team.found), ("onboarding_brief", True))
        self.assertEqual(_ask("tell me about the billing service").tool, "who_knows")

    def test_the_catalogue_mcp_and_the_chip(self):
        self.assertTrue(catalogue.get_tool("person_profile").run({"name": "arjun"}, ORG).found)
        self.assertIn("person_profile", mcp_tools.tool_names())
        called = mcp_tools.call_tool("person_profile", {"name": "arjun"})
        self.assertEqual((called["tool"], called["found"]), ("person_profile", True))
        self.assertIn("who is Arjun Mehta", ask_api.examples().questions)
        answer = ask_api.answer_question("who is Arjun Mehta").to_dict()
        self.assertEqual((answer["tool"], answer["found"], answer["grid"]["kind"]), ("person_profile", True, "person_profile"))
        self.assertTrue(answer["nodes"] and answer["edges"])

    def test_the_grid_rows_are_what_they_hold(self):
        grid = _profile("who is sara").grid
        self.assertEqual((grid["kind"], grid["title"]), ("person_profile", "What Sara Lindqvist holds"))
        self.assertEqual([column["key"] for column in grid["columns"]], ["held", "dependents", "people", "alone"])
        self.assertEqual(
            [(row["name"], row["held"], row["dependents"], row["people"], row["flags"]["alone"], row["score"]) for row in grid["rows"]],
            [
                ("Postgres", "owns", 6, [], True, 7),
                ("Graph Store", "owns", 2, [], True, 3),
                ("Auth Service", "knows", 5, ["Maya Ortiz", "Arjun Mehta"], False, 0),
                ("Billing Service", "knows", 2, ["Arjun Mehta", "Lena Fischer", "Tomas Reyes"], False, 0),
            ],
        )
        # "alone" is if_person_leaves' own list: Arjun is Billing Service's only owner, though others know it.
        billing = _profile("who is arjun").grid["rows"][0]
        self.assertEqual((billing["name"], billing["flags"]["alone"], billing["team"], billing["owners"]), ("Billing Service", True, "Payments", ["Arjun Mehta"]))
        self.assertIn("Billing Service: Arjun Mehta is its only owner", catalogue.get_tool("if_person_leaves").run({"name": "arjun"}, ORG).answer)


class PersonProfileWithGitHubTest(_Graph):
    graph_name = GITHUB_GRAPH

    @classmethod
    def seed_more(cls):
        notebook = script.REPOS[0]
        open_pr = script.RepoPullRequest(58, "Expire idle sessions after a day", "Idle sessions end after 24 hours.", "maya-ortiz", ("services/auth/session.py",), ("arjun-mehta",), merged=False)
        load_github_history((dataclasses.replace(notebook, pull_requests=notebook.pull_requests + (open_pr,)), *script.REPOS[1:]))

    def test_an_open_pull_request_comes_first(self):
        result = _profile("who is maya")
        self.assertIn("Pull requests: authored notebook#58 (open) and notebook#45; reviewed notebook#41.", result.answer)
        cited = _cites(result)
        self.assertIn("Maya Ortiz -[AUTHORED]-> notebook#58", cited)
        self.assertIn("notebook#58 -[TOUCHES]-> Auth Service", cited)

    def test_review_pairs_count_toward_who_they_work_with(self):
        # Arjun reviewed Maya's notebook#45 and #58 and Lena's notebook#52: Maya now shares three things, Lena two.
        arjun = _profile("who is arjun").answer
        self.assertIn("Pull requests: reviewed notebook#58 (open), notebook#45 and notebook#52.", arjun)
        self.assertIn("Works most with Maya Ortiz (Auth Service, notebook#45, notebook#58) and Lena Fischer (Billing Service, notebook#52).", arjun)

    def test_a_private_repo_s_pull_request_is_told_only_with_its_scope(self):
        self.assertNotIn("billing-internal#7", _profile("who is arjun").answer)
        self.assertIn("billing-internal#7", _profile("who is arjun", ORG + [BILLING_INTERNAL]).answer)


if __name__ == "__main__":
    unittest.main()
