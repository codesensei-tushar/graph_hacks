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
            "what should I read before touching the invoice worker": ("reading_list", {"name": "what should I read before touching the invoice worker"}),
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

    def test_questions_judges_type_route_to_the_tool_that_answers_them(self):
        # A team, not Billing Service, which the graph also knows as "payments".
        for question in ("what does the payments team own", "who is on the platform team", "who's in the data team", "members of the payments team"):
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question), ("onboarding_brief", {"name": question}))
        self.assertEqual(ask_service.route("what connects sara and the invoice worker")[0], "path_between")
        # The bus factor for a service is who knows it; on a person it is that person leaving.
        self.assertEqual(ask_service.route("who is the bus factor for billing")[0], "who_knows")
        self.assertEqual(ask_service.route("what's the bus factor on arjun")[0], "if_person_leaves")
        # "who owns billing" and "who connects payments and platform" are unchanged.
        self.assertEqual(ask_service.route("who owns billing")[0], "who_owns")
        self.assertEqual(ask_service.route("who connects payments and platform")[0], "connectors")

    def test_what_a_service_depends_on_is_blast_radius_the_other_way(self):
        for question in (
            "what does the invoice worker depend on",
            "What does Billing depend on?",
            "the invoice worker depends on what",
            "what are billing's dependencies",
            "dependencies of the api gateway",
            "what does search need",
            "what does auth rely on",
            "what does the invoice worker need to run",
            "what does billing require",
            "what is needed to run search",
            "the invoice worker requires what",
        ):
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question), ("blast_radius", {"name": question, "direction": "dependencies"}))
        self.assertEqual(
            ask_service.route("what does the invoice worker depend on, one hop"),
            ("blast_radius", {"name": "what does the invoice worker depend on, one hop", "direction": "dependencies", "depth": 1}),
        )
        # What depends on a service, and questions that only share the words, are unchanged.
        for question in ("what depends on billing", "what breaks if auth goes down", "who are the dependents of auth", "what relies on billing"):
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question), ("blast_radius", {"name": question}))
        self.assertEqual(ask_service.route("what do i need to know about billing")[0], "who_knows")
        self.assertEqual(ask_service.route("which service is most depended on")[0], "load_bearing")
        self.assertFalse(algorithms.asks_for_dependencies("what does billing need to know"))
        self.assertEqual(ask_service.route("what does billing need to know")[0], "who_knows")

    def test_reworded_chips_route_like_the_chips(self):
        for question, tool in (
            ("how are maya and the invoice worker related", "path_between"),
            ("path from maya to the invoice worker", "path_between"),
            ("which service matters most", "load_bearing"),
            ("who works with who", "working_groups"),
            ("which people work together?", "working_groups"),
            ("who collaborates with whom", "working_groups"),
            ("is the billing retries decision still valid?", "decision_pressure"),
            ("what does sales own", "onboarding_brief"),
        ):
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question)[0], tool)
        # Unchanged neighbours.
        self.assertEqual(ask_service.route("how is maya connected to the invoice worker")[0], "path_between")
        self.assertEqual(ask_service.route("who actually works together")[0], "working_groups")
        self.assertEqual(ask_service.route("who owns billing")[0], "who_owns")

    def test_who_signed_off_is_a_decision_and_summarising_a_team_is_its_brief(self):
        for question in ("who approved moving the memory layer", "who proposed the billing retries", "who signed off on the graph store", "who decided to leave postgres"):
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question), ("decision_trace", {"topic": question}))
        for question in ("summarize the payments team", "summarise the sales team", "overview of the platform team", "tell me about the data team", "describe the payments team"):
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question), ("onboarding_brief", {"name": question}))
        # A service is not a team, and "who knows" is still who_knows.
        self.assertEqual(ask_service.route("tell me about the billing service")[0], "who_knows")
        self.assertEqual(ask_service.route("who knows the memory layer")[0], "who_knows")

    def test_risk_across_the_company_is_the_risk_map_and_one_person_or_service_is_not(self):
        for question in (
            "what are our riskiest services",
            "what's our biggest bus factor risk",
            "bus factor across the company",
            "where are we one person away from an outage",
            "which services have no runbook",
            "what is undocumented",
            "what are our knowledge gaps",
            "risk map",
        ):
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question), ("risk_map", {}))
        for question, tool in (
            ("the bus factor on arjun", "if_person_leaves"),
            ("what's the bus factor on billing's owner", "if_person_leaves"),
            ("who is the bus factor for billing", "who_knows"),
            ("what is most at risk if arjun leaves", "if_person_leaves"),
            ("which services are load bearing", "load_bearing"),
            ("what breaks if auth goes down", "blast_radius"),
        ):
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question)[0], tool)

    def test_what_to_read_before_a_change_is_a_reading_list(self):
        for question in (
            "what should I read before touching the invoice worker",
            "reading list for billing",
            "what should I read to understand auth",
            "what docs cover the invoice worker",
            "which docs describe billing",
            "what should a new engineer read about billing",
            "docs before touching postgres",
            "read up on the memory layer",
            "which documents should I read before changing billing",
        ):
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question), ("reading_list", {"name": question}))
        for question, tool in (
            ("what should I know about billing", "who_knows"),
            ("what should I know about the payments team", "onboarding_brief"),
            ("who should review the billing runbook", "who_should_know"),
            ("who should know if postgres changes", "who_should_know"),
            ("what does the invoice worker depend on", "blast_radius"),
            ("what have we said about billing", "related_memories"),
            ("why did we move the memory layer off postgres", "decision_trace"),
        ):
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question)[0], tool)

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
