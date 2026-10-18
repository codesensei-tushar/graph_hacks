"""explore, find and neighbors without a graph: routing, argument parsing, schemas, and refusals that never reach a query."""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.domain.knowledge import agent, ask_service, catalogue, explore, fallback, find  # noqa: E402
from src.domain.knowledge.kinds import SERVICE_KINDS  # noqa: E402


class RouteTest(unittest.TestCase):
    def test_explore_and_find_questions(self):
        cases = {
            "explore billing": ("explore", {"name": "explore billing"}),
            "what do we know about the sales team": ("explore", {"name": "what do we know about the sales team"}),
            "everything on TKT-102": ("explore", {"name": "everything on TKT-102"}),
            "what's around auth": ("explore", {"name": "what's around auth"}),
            "the neighbourhood of the invoice worker": ("explore", {"name": "the neighbourhood of the invoice worker"}),
            "explore billing two hops out": ("explore", {"name": "explore billing two hops out", "depth": 2}),
            "explore auth six hops out": ("explore", {"name": "explore auth six hops out", "depth": 2}),
            "which tickets are open": ("find", {"label": "Ticket", "state": "open"}),
            "which tickets are open for billing?": ("find", {"label": "Ticket", "state": "open", "about": "billing"}),
            "what are the open pull requests": ("find", {"label": "PullRequest", "state": "open"}),
            "what decisions have we made": ("find", {"label": "Decision"}),
            "what decisions did we make?": ("find", {"label": "Decision"}),
            "list the services in payments": ("find", {"label": "Service", "team": "payments"}),
            "show me all teams": ("find", {"label": "Team"}),
            "list the processes in the sales team": ("find", {"label": "Service", "kind": "process", "team": "sales"}),
            "list the standing decisions in sales": ("find", {"label": "Decision", "standing": True, "team": "sales"}),
            "show me merged pull requests": ("find", {"label": "PullRequest", "state": "merged"}),
            "list people in the platform team": ("find", {"label": "Person", "team": "platform"}),
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question), expected)

    def test_questions_other_tools_answer_keep_their_tool(self):
        cases = {
            "what decisions have we made about billing": "decision_trace",
            "list the decisions about billing": "decision_trace",
            "why did we move the memory layer off postgres": "decision_trace",
            "who owns billing": "who_owns",
            "who knows the payments gateway": "who_knows",
            "what breaks if auth goes down": "blast_radius",
            "what breaks if the sales team goes down": "blast_radius",
            "what does the payments team own": "onboarding_brief",
            "who is on the platform team": "onboarding_brief",
            "what should I read before touching the invoice worker": "reading_list",
            "what docs cover billing": "reading_list",
            "search for webhooks": "search",
            "anything about PDF layout": "search",
            "what have we said about billing": "related_memories",
            "is the billing retries decision still the last word": "decision_pressure",
            "what are our riskiest services": "risk_map",
            "which services are load-bearing": "load_bearing",
            "how is maya connected to the invoice worker": "path_between",
            "what does the invoice worker depend on": "blast_radius",
            "who should know if postgres changes": "who_should_know",
        }
        for question, tool in cases.items():
            with self.subTest(question=question):
                self.assertEqual(ask_service.route(question)[0], tool)


class ParseTest(unittest.TestCase):
    def test_the_label_comes_from_the_first_list_noun(self):
        self.assertEqual(find.parse("which tickets are open about the invoice worker"), {"label": "Ticket", "state": "open", "about": "the invoice worker"})
        self.assertEqual(find.parse("list the documents describing billing"), {"label": "Document", "describes": "billing"})
        self.assertEqual(find.parse("show pull requests touching auth"), {"label": "PullRequest", "touches": "auth"})
        self.assertEqual(find.parse("list people on billing"), {"label": "Person", "holds": "billing"})
        self.assertEqual(find.parse("list the systems"), {"label": "Service", "kind": "system"})
        self.assertEqual(find.parse("hello there"), {})

    def test_a_state_a_label_cannot_take_is_dropped(self):
        self.assertEqual(find.parse("show merged tickets"), {"label": "Ticket"})
        self.assertEqual(find.parse("list open services"), {"label": "Service"})


class RefusalTest(unittest.TestCase):
    """Arguments outside an allowlist are refused before any query runs."""

    def setUp(self):
        patcher = mock.patch.object(explore, "connect", side_effect=AssertionError("no query may run"))
        self.addCleanup(patcher.stop)
        patcher.start()
        find_patcher = mock.patch.object(find, "connect", side_effect=AssertionError("no query may run"))
        self.addCleanup(find_patcher.stop)
        find_patcher.start()

    def test_neighbors_refuses_a_relation_or_direction_off_the_list(self):
        planted = explore.neighbors("billing", "OWNS]->(x) DETACH DELETE x //", ["org_shared"])
        self.assertFalse(planted.found)
        self.assertIn("is not a relationship neighbors follows", planted.answer)
        self.assertFalse(explore.neighbors("billing", "", ["org_shared"]).found)
        self.assertEqual(explore.neighbors("billing", "OWNS", ["org_shared"], "sideways").answer, "“sideways” is not a direction. Use out, in or both.")

    def test_find_refuses_a_label_or_filter_off_the_list(self):
        self.assertFalse(find.find("Memory", {}, ["org_shared"]).found)
        self.assertEqual(find.find("Team", {"kind": "process"}, ["org_shared"]).answer, "Teams take no filters; not by kind.")
        self.assertEqual(
            find.find("Ticket", {"owner": "maya"}, ["org_shared"]).answer,
            "Tickets can be filtered by state, about, team, person, blocked and project; not by owner.",
        )
        self.assertEqual(
            find.find("Service", {"kind": "spaceship"}, ["org_shared"]).answer,
            "“spaceship” is not a kind. A service's kind is one of service, system, process, product, account, policy and playbook.",
        )
        self.assertFalse(find.find("Decision", {"standing": "maybe"}, ["org_shared"]).found)


class SchemaTest(unittest.TestCase):
    def test_find_shows_the_model_every_label_and_filter(self):
        schema = catalogue.get_tool("find").args_schema
        self.assertEqual(schema["required"], ["label"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["label"]["enum"], list(find.LABELS))
        self.assertEqual(set(schema["properties"]) - {"label"}, set(find.FILTER_NAMES))
        self.assertEqual(schema["properties"]["kind"]["enum"], list(SERVICE_KINDS))
        self.assertEqual(schema["properties"]["standing"]["type"], "boolean")
        self.assertIn("Ticket: state, about, team, person, blocked, project", schema["properties"]["label"]["description"])

    def test_neighbors_and_explore_take_bounded_arguments(self):
        neighbors = catalogue.get_tool("neighbors").args_schema["properties"]
        self.assertEqual(neighbors["relation"]["enum"], list(explore.RELATIONS))
        self.assertEqual(neighbors["direction"]["enum"], ["out", "in", "both"])
        self.assertEqual(neighbors["depth"]["maximum"], explore.MAX_NEIGHBOR_DEPTH)
        self.assertEqual(catalogue.get_tool("explore").args_schema["properties"]["depth"]["maximum"], explore.MAX_EXPLORE_DEPTH)
        described = agent.describe_catalogue([catalogue.get_tool("neighbors")])
        self.assertIn("Between 1 and 3.", described)
        self.assertEqual(agent.validate_args(catalogue.get_tool("neighbors"), {"name": "auth", "relation": "DEPENDS_ON", "depth": 9})["depth"], 3)

    def test_each_description_says_when_a_specific_tool_fits_better(self):
        self.assertIn("for owners use who_owns or who_knows, for outages blast_radius, for a person person_profile", catalogue.get_tool("explore").description)
        self.assertIn("For what changed recently use what_changed", catalogue.get_tool("find").description)
        self.assertIn("For dependencies prefer blast_radius", catalogue.get_tool("neighbors").description)

    def test_queries_take_the_relation_and_direction_as_parameters(self):
        for query in (explore._DIRECT, explore._THROUGH_ACCOUNT, explore._OWN_ACCOUNTS):
            self.assertIn("type(r) = $rel", query)
            self.assertIn("$dir = 'out'", query)
            self.assertNotIn("{", query.replace("{id: $id}", ""))
        for filters in find.FILTERS.values():
            for spec in filters.values():
                if spec.query:
                    self.assertIn("$value", spec.query.replace("$value_name", ""))
                    self.assertNotIn("{scoped", spec.query)


class WordingTest(unittest.TestCase):
    def test_groups_read_in_plain_words(self):
        self.assertEqual(explore.phrase("ABOUT", False, "Ticket"), "tickets about it:")
        self.assertEqual(explore.phrase("DEPENDS_ON", False, "Service"), "depended on by")
        self.assertEqual(explore.phrase("SUPERSEDES", False, "Decision"), "replaced by")
        self.assertEqual(explore.phrase("SOMETHING_NEW", True, "Service"), "something new")
        self.assertEqual(explore._listing([f"n{index}" for index in range(10)], 8), "n0, n1, n2, n3, n4, n5, n6, n7 and 2 more")


class FallbackOrderTest(unittest.TestCase):
    def test_explore_sits_between_person_and_search(self):
        self.assertEqual(fallback.STEPS, (fallback._person, fallback._explore, fallback._search))
