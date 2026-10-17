"""The team page's pure parts: grouping by kind, the edges it draws. No database."""

import unittest

import tests  # noqa: F401  pins the test settings before src loads

from src.domain.knowledge import team_page
from src.domain.knowledge.team_page import KindGroup, Member, OwnedThing, TeamPage


def _thing(thing_id: str, kind: str, owners=()) -> OwnedThing:
    return OwnedThing(thing_id, thing_id.title(), kind, "", list(owners))


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
            owns=team_page.group_by_kind([_thing("acme", "account"), _thing("renewals", "process"), _thing("pricing", "playbook")]),
        )
        self.assertEqual([group.owner_label for group in page.owns], ["Run by", "Owned by", "Owned by"])
        self.assertEqual(
            team_page.team_questions(page),
            ["who owns Renewals, and who else has worked on it", "what breaks if Renewals stalls", "how is Priya Nair connected to Pricing"],
        )
