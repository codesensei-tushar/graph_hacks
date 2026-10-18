"""The team page's pure parts: grouping by kind, the edges it draws. No database."""

import unittest

import tests  # noqa: F401  pins the test settings before src loads

from src.domain.knowledge import team_page
from src.domain.knowledge.team_page import KindGroup, Member, OpenTicket, OwnedThing, TeamLink, TeamPage


def _thing(thing_id: str, kind: str, owners=(), dependents: int = 0) -> OwnedThing:
    return OwnedThing(thing_id, thing_id.title(), kind, "", list(owners), dependents)


class GroupByKindTest(unittest.TestCase):
    def test_kinds_follow_the_fixed_order_and_unknown_ones_come_last_alphabetically(self):
        groups = team_page.group_by_kind(
            [_thing("hubspot", "system"), _thing("renewals", "process"), _thing("billing", "service"), _thing("zeta", "zine"), _thing("acme", "alpha")]
        )
        self.assertEqual([group.kind for group in groups], ["service", "system", "process", "alpha", "zine"])
        self.assertEqual([group.title for group in groups], ["Services", "Systems", "Processes", "Alpha", "Zine"])

    def test_every_listed_kind_has_a_plural_title(self):
        self.assertEqual(set(team_page.KIND_TITLES), set(team_page.KINDS))
        self.assertEqual(team_page.KINDS, ("service", "system", "process", "product", "account", "policy", "playbook"))

    def test_things_keep_their_order_inside_a_group(self):
        groups = team_page.group_by_kind([_thing("b", "process"), _thing("a", "process")])
        self.assertEqual([thing.id for thing in groups[0].items], ["b", "a"])


class TeamPathTest(unittest.TestCase):
    def test_membership_then_what_the_team_runs_then_who_owns_it(self):
        page = TeamPage(
            "sales",
            "Sales",
            "",
            "Quillfern",
            members=[Member("priya", "Priya Nair", "Account executive", ["Renewals"])],
            owns=[KindGroup("process", "Processes", [_thing("renewals", "process", ["Priya Nair"])]), KindGroup("system", "Systems", [_thing("hubspot", "system", ["Dev Rao"])])],
        )
        steps = team_page.team_path(page)
        self.assertEqual(
            [step.cite() for step in steps],
            [
                "Priya Nair -[MEMBER_OF]-> Sales",
                "Sales -[RUNS]-> Renewals",
                "Sales -[RUNS]-> Hubspot",
                "Priya Nair -[OWNS]-> Renewals",
                "Dev Rao -[OWNS]-> Hubspot",
            ],
        )
        # A member keeps one node across MEMBER_OF and OWNS; an owner outside the team is keyed by name.
        self.assertEqual(steps[0].from_id, steps[3].from_id)
        self.assertIsNone(steps[4].from_id)
        page.path = steps
        drawn = page.to_dict()
        self.assertEqual(len(drawn["edges"]), 5)
        self.assertEqual({node["label"] for node in drawn["nodes"]}, {"Person", "Team", "Service"})


class QuestionsTest(unittest.TestCase):
    def test_questions_are_worded_by_kind_and_skip_accounts_for_outages(self):
        page = TeamPage(
            "sales",
            "Sales",
            "",
            None,
            members=[Member("priya", "Priya Nair", "", [])],
            owns=team_page.group_by_kind(
                [_thing("acme", "account", ["Priya Nair"], 3), _thing("renewals", "process", [], 1), _thing("pricing", "playbook", ["Priya Nair"], 2)]
            ),
        )
        self.assertEqual([group.owner_label for group in page.owns], ["Run by", "Owned by", "Owned by"])
        # The owner question skips Renewals, which has no owner; the outage question skips the account
        # and picks the playbook, which more things depend on than Renewals.
        self.assertEqual(
            team_page.team_questions(page),
            ["who owns Acme, and who else has worked on it", "what breaks if Pricing no longer applies", "how is Priya Nair connected to Pricing"],
        )

    def test_no_outage_question_when_nothing_depends_on_what_the_team_runs(self):
        page = TeamPage("ops", "Ops", "", None, owns=team_page.group_by_kind([_thing("runbook", "playbook", ["Dev Rao"])]))
        self.assertEqual(team_page.team_questions(page), ["who owns Runbook, and who else has worked on it"])


def _service(thing_id: str, name: str) -> OwnedThing:
    return OwnedThing(thing_id, name, "service", "", [], 0)


class EmptySectionsTest(unittest.TestCase):
    def test_a_team_with_nothing_names_every_optional_section_in_page_order(self):
        page = TeamPage("ops", "Ops", "", "Quillfern")
        expected = ["links to other teams", "decisions", "open tickets", "chat"]
        self.assertEqual(page.empty_sections(), expected)
        self.assertEqual(page.to_dict()["empty"], expected)

    def test_a_section_with_rows_is_not_named(self):
        page = TeamPage(
            "ops",
            "Ops",
            "",
            "Quillfern",
            tickets=[OpenTicket("TKT-1", "Pager is down", "Pager")],
            depended_on_by=[TeamLink("data", "Data", ((("search", "Search Service"), ("pager", "Pager")),))],
        )
        self.assertEqual(page.empty_sections(), ["decisions", "chat"])


class TeamLinksTest(unittest.TestCase):
    def test_rows_group_by_team_in_row_order_and_a_repeated_pair_counts_once(self):
        links = team_page.group_links(
            [
                ("platform", "Platform", "billing", "Billing Service", "auth", "Auth Service"),
                ("platform", "Platform", "billing", "Billing Service", "postgres", "Postgres"),
                ("platform", "Platform", "billing", "Billing Service", "auth", "Auth Service"),
                ("data", "Data", "invoice", "Invoice Worker", "search", "Search Service"),
            ]
        )
        self.assertEqual(
            [link.to_dict() for link in links],
            [
                {"id": "platform", "name": "Platform", "pairs": [["Billing Service", "Auth Service"], ["Billing Service", "Postgres"]]},
                {"id": "data", "name": "Data", "pairs": [["Invoice Worker", "Search Service"]]},
            ],
        )

    def test_the_drawing_cites_each_tie_and_the_other_team_running_its_end_once(self):
        page = TeamPage(
            "payments",
            "Payments",
            "",
            "Quillfern",
            owns=[KindGroup("service", "Services", [_service("billing", "Billing Service"), _service("invoice", "Invoice Worker")])],
            depends_on=team_page.group_links(
                [
                    ("platform", "Platform", "billing", "Billing Service", "auth", "Auth Service"),
                    ("platform", "Platform", "invoice", "Invoice Worker", "auth", "Auth Service"),
                ]
            ),
            depended_on_by=team_page.group_links([("platform", "Platform", "gateway", "API Gateway", "billing", "Billing Service")]),
        )
        steps = team_page.team_path(page)
        self.assertEqual(
            [step.cite() for step in steps],
            [
                "Payments -[RUNS]-> Billing Service",
                "Payments -[RUNS]-> Invoice Worker",
                "Billing Service -[DEPENDS_ON]-> Auth Service",
                "Platform -[RUNS]-> Auth Service",
                "Invoice Worker -[DEPENDS_ON]-> Auth Service",
                "API Gateway -[DEPENDS_ON]-> Billing Service",
                "Platform -[RUNS]-> API Gateway",
            ],
        )
        # Every end carries its id, so a thing the team runs is one node across RUNS and DEPENDS_ON.
        self.assertTrue(all(step.to_id for step in steps))
        page.path = steps
        drawn = page.to_dict()
        self.assertEqual(sorted(node["name"] for node in drawn["nodes"]), ["API Gateway", "Auth Service", "Billing Service", "Invoice Worker", "Payments", "Platform"])


def _row(name: str, one_person: bool, owners=("Lena Fischer",)) -> dict:
    return {"name": name, "owners": list(owners), "flags": {"one_person": one_person, "no_document": False, "owner_elsewhere": False}}


class GlanceTest(unittest.TestCase):
    def test_the_line_counts_ties_both_ways_and_what_rests_on_one_person(self):
        page = TeamPage(
            "payments",
            "Payments",
            "",
            "Quillfern",
            depends_on=team_page.group_links(
                [
                    ("platform", "Platform", "billing", "Billing Service", "auth", "Auth Service"),
                    ("platform", "Platform", "billing", "Billing Service", "postgres", "Postgres"),
                ]
            ),
            depended_on_by=team_page.group_links([("platform", "Platform", "gateway", "API Gateway", "billing", "Billing Service")]),
            risk={"rows": [_row("Invoice Worker", True), _row("Billing Service", False)]},
        )
        self.assertEqual(
            team_page.team_glance(page),
            "Payments depends on Platform (2 edges). Platform (1 edge) depends on Payments. 1 of the 2 things it runs rests on one person: Invoice Worker.",
        )

    def test_several_teams_agree_in_number_and_a_team_without_gaps_says_so(self):
        page = TeamPage(
            "platform",
            "Platform",
            "",
            "Quillfern",
            depended_on_by=team_page.group_links(
                [
                    ("data", "Data", "search", "Search Service", "auth", "Auth Service"),
                    ("payments", "Payments", "billing", "Billing Service", "auth", "Auth Service"),
                ]
            ),
            risk={"rows": [_row("Auth Service", False), _row("Postgres", False)]},
        )
        self.assertEqual(
            team_page.team_glance(page),
            "Data (1 edge) and Payments (1 edge) depend on Platform. None of the 2 things it runs rests on one person.",
        )

    def test_a_team_with_nothing_has_no_line(self):
        self.assertEqual(team_page.team_glance(TeamPage("ops", "Ops", "", "Quillfern")), "")

    def test_the_risky_owner_and_a_dependency_elsewhere_become_questions(self):
        page = TeamPage(
            "payments",
            "Payments",
            "",
            "Quillfern",
            depends_on=team_page.group_links([("platform", "Platform", "billing", "Billing Service", "auth", "Auth Service")]),
            risk={"rows": [_row("Invoice Worker", True, ("Lena Fischer",))]},
        )
        questions = team_page.team_questions(page)
        self.assertIn("what if Lena Fischer leaves", questions)
        self.assertIn("what does Billing Service depend on", questions)

