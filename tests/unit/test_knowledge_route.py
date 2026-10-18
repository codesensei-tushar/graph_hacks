"""The keyword router and the depth parser, pure."""

import unittest

from src.domain.knowledge import algorithms, ask_service, coordination


class RouteTest(unittest.TestCase):
    def test_each_tool_has_a_keyword_route(self):
        cases = {
            "who owns billing": ("who_owns", {"name": "who owns billing"}),
            "who owns billing, and who else has worked on it": ("who_knows", {"name": "who owns billing, and who else has worked on it"}),
            "why did we move the memory layer off postgres": ("decision_trace", {"topic": "why did we move the memory layer off postgres"}),
            "what breaks if auth goes down": ("blast_radius", {"name": "what breaks if auth goes down"}),
            "what breaks if auth goes down, two hops out": ("blast_radius", {"name": "what breaks if auth goes down, two hops out", "depth": 2}),
            "which services are load bearing": ("load_bearing", {"limit": algorithms.DEFAULT_LOAD_BEARING}),
            "how is maya connected to the invoice worker": ("path_between", {"source": "how is maya connected to the invoice worker", "target": ""}),
            "what have we said about billing": ("related_memories", {"name": "what have we said about billing"}),
            "who said the retries were flaky": ("recall", {"query": "who said the retries were flaky"}),
            "why did we cut vector recall": ("decision_trace", {"topic": "why did we cut vector recall"}),
            "is the billing retries decision still the last word": (
                "decision_pressure",
                {"topic": "is the billing retries decision still the last word"},
            ),
            "who actually works together": ("working_groups", {}),
            "what breaks if arjun leaves": ("if_person_leaves", {"name": "what breaks if arjun leaves"}),
            "who should know about notebook#45": ("who_should_know", {"name": "who should know about notebook#45"}),
            "": ("none", {}),
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question), expected)

    def test_a_person_leaving_is_not_a_service_going_down(self):
        for question in (
            "what breaks if Arjun leaves",
            "what happens if arjun leaves",
            "what if lena quits",
            "who covers for sara",
            "who takes over from kofi",
            "what if maya is on leave",
            "what is Arjun the only one who knows",
            "what does tomas know that nobody else does",
            "what's the bus factor on billing's owner",
        ):
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question)[0], "if_person_leaves")
        # A service-shaped "only one who knows" question still asks who knows the service.
        self.assertEqual(ask_service.route("who is the only one who knows billing")[0], "who_knows")
        self.assertEqual(ask_service.route("what breaks if the claude api goes down, one hop")[0], "blast_radius")
        self.assertEqual(ask_service.route("what is left uncited after the billing retries decision")[0], "decision_pressure")

    def test_joining_a_team_asks_for_its_brief(self):
        for question in (
            "I just joined Sales, what should I know",
            "brief me on the payments team",
            "I'm new to the data team",
            "i am new on platform",
            "onboard me to sales",
            "get me up to speed on payments",
            "what should I know about the payments team",
            "what should a new hire know about the sales team",
        ):
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question), ("onboarding_brief", {"name": question}))
        # Naming no team, it is still a who_knows question.
        self.assertEqual(ask_service.route("what should I know about billing")[0], "who_knows")

    def test_who_connects_teams_is_not_how_two_things_connect(self):
        for question in ("who connects Sales and Engineering", "who bridges payments and platform", "who links data to sales", "bridges between sales and platform"):
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question), ("connectors", {"source": question, "target": ""}))
        self.assertEqual(ask_service.route("how is maya connected to the invoice worker")[0], "path_between")

    def test_who_should_know_about_a_change_is_not_who_knows_it_or_what_breaks(self):
        for question in (
            "who should know about notebook#45",
            "who else should review notebook#45",
            "who needs to know if auth changes",
            "who else needs to know about PR 52",
            "who should be told about #45",
            "who should hear about the billing change",
            "who should we loop in on notebook#52",
            "who should I ping about billing-internal#7",
            "Who should know about notebook#45?",
        ):
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question), ("who_should_know", {"name": question}))
        self.assertEqual(ask_service.route("what breaks if auth goes down")[0], "blast_radius")
        self.assertEqual(ask_service.route("who owns billing")[0], "who_owns")
        self.assertEqual(ask_service.route("who knows billing")[0], "who_knows")
        # The demo's thread follow-up after a ticket still asks who knows the service.
        self.assertEqual(ask_service.route("who should look at it")[0], "who_knows")
        # A person leaving is still about the person.
        self.assertEqual(ask_service.route("who should know if arjun leaves")[0], "if_person_leaves")
        # A ticket is asked about, not changed; "why" is a decision question.
        self.assertEqual(ask_service.route("who should I ping about TKT-101")[0], "who_knows")
        self.assertEqual(ask_service.route("who needs to know why we moved off postgres")[0], "decision_trace")

    def test_someone_to_ask_is_who_knows_and_pr_dash_number_is_a_pull_request(self):
        # Someone to go to, not someone a change should reach; "outage" does not make it blast radius.
        for question in ("who should I ping about billing", "who should we ping about the auth outage", "who should I ask about invoices"):
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question), ("who_knows", {"name": question}))
        # "PR-45" looks like a ticket id but is a pull request.
        self.assertEqual(ask_service.route("who should know about PR-45"), ("who_should_know", {"name": "who should know about PR-45"}))
        self.assertEqual(coordination.pr_reference("who should know about PR-45"), (None, 45))
        self.assertEqual(ask_service.route("who should know about TKT-45")[0], "who_knows")
        # A document stays on who_should_know here; the tool answers it as who_knows (integration test).
        self.assertEqual(ask_service.route("who should review the billing runbook")[0], "who_should_know")

    def test_depth_words(self):
        self.assertEqual(algorithms.depth_in("what breaks if auth goes down, one hop"), 1)
        self.assertEqual(algorithms.depth_in("and 3 hops out"), 3)
        self.assertEqual(algorithms.depth_in("blast radius of postgres at depth 9"), 6)
        self.assertEqual(algorithms.depth_in("what depends on auth directly"), 1)
        self.assertIsNone(algorithms.depth_in("what breaks if auth goes down"))
        self.assertEqual(algorithms.depth_in("what breaks if the claude api goes down, one hop"), 1)
        self.assertEqual(algorithms.depth_in("and three hops out"), 3)
        self.assertIsNone(algorithms.depth_in("all the way"))

    def test_depth_is_clamped_to_an_integer_or_none(self):
        self.assertEqual(algorithms.clamp_depth(0), 1)
        self.assertEqual(algorithms.clamp_depth("4"), 4)
        self.assertEqual(algorithms.clamp_depth(99), algorithms.MAX_DEPTH)
        self.assertIsNone(algorithms.clamp_depth(None))
        self.assertIsNone(algorithms.clamp_depth("deep"))
