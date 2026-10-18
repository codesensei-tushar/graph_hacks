"""``onboard team:`` reads one Slack line into team-file data, mentions resolved through Slack links only."""

import unittest

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.domain.onboarding.onboard_command import command_args, for_slack, team_data  # noqa: E402
from src.domain.onboarding.team_import import TeamFileError  # noqa: E402
from tests.unit.test_team_import import FakeLookup  # noqa: E402

LINE = (
    "sales | members: <@UPRIYA|priya> <@UDEV> | owns: Renewals (process), HubSpot (system, <@UNOOR>), Pricing playbook (playbook) "
    "| decision: annual plans only, decided 2026-09-01 | decision: quarterly price review | summary: Renewals and pricing."
)


class CommandArgsTest(unittest.TestCase):
    def test_the_line_splits_into_the_tools_arguments(self):
        args = command_args(LINE)
        self.assertEqual(args["team"], "Sales")
        self.assertEqual(args["summary"], "Renewals and pricing.")
        # A mention that carries a name keeps its | inside the brackets.
        self.assertEqual(args["members"], ["<@UPRIYA>", "<@UDEV>"])
        self.assertEqual(args["runs"], ["Renewals (process)", "HubSpot (system, <@UNOOR>)", "Pricing playbook (playbook)"])
        self.assertEqual(args["decisions"], ["annual plans only, decided 2026-09-01", "quarterly price review"])

    def test_aliases_follow_aka_outside_or_inside_the_brackets(self):
        args = command_args(
            "Payments | members: <@UPAULA> | owns: Billing Service (service) aka billing, payments, Ledger (system, <@USAM>, aka books), Refunds"
        )
        self.assertEqual(args["runs"], ["Billing Service (service) aka billing, payments", "Ledger (system, <@USAM>, aka books)", "Refunds"])
        args |= {"profiles": {"UPAULA": "Paula Ortiz", "USAM": "Sam Okafor"}}
        runs = team_data(args, FakeLookup(slack={}))["runs"]
        self.assertEqual(
            runs,
            [
                {"name": "Billing Service", "kind": "service", "owner": "slack-upaula", "aliases": ["billing", "payments"]},
                {"name": "Ledger", "kind": "system", "owner": "slack-usam", "aliases": ["books"]},
                {"name": "Refunds", "kind": None, "owner": "slack-upaula"},
            ],
        )
        no_kind = command_args("Payments | members: <@UPAULA> | owns: Payouts aka payout, disbursements")["runs"]
        self.assertEqual(no_kind, ["Payouts aka payout, disbursements"])
        row = team_data({**command_args("Payments | members: <@UPAULA> | owns: Payouts aka payout, disbursements"), "profiles": {"UPAULA": "Paula Ortiz"}}, FakeLookup(slack={}))["runs"][0]
        self.assertEqual((row["name"], row["aliases"]), ("Payouts", ["payout", "disbursements"]))

    def test_what_cannot_be_read_is_refused_with_the_shape_to_use(self):
        for line, expected in (
            ("| members: <@U1>", "say the team's name first"),
            ("Sales | members: priya", "mention each member with @"),
            ("Sales | colour: blue | members: <@U1>", "I can't read `colour: blue`"),
            ("Sales", "name at least one member"),
        ):
            with self.subTest(line=line), self.assertRaises(TeamFileError) as caught:
                command_args(line)
            self.assertIn(expected, caught.exception.problems[0])


class TeamDataTest(unittest.TestCase):
    def setUp(self):
        self.args = command_args(LINE) | {"profiles": {"UDEV": "Dev Patel", "UNOOR": "Noor Haddad"}, "asker_slack_user_id": "UPRIYA", "decided_at": 1_790_000_000_000}
        # Priya is already linked; Dev and Noor are new to the graph.
        self.lookup = FakeLookup(slack={"UPRIYA": "priya"})

    def test_mentions_resolve_through_links_and_new_people_get_their_own_id(self):
        data = team_data(self.args, self.lookup)
        self.assertEqual(
            data["members"],
            [
                {"id": "priya", "name": "priya", "slack_id": "UPRIYA"},
                {"id": "slack-udev", "name": "Dev Patel", "slack_id": "UDEV"},
                {"id": "slack-unoor", "name": "Noor Haddad", "slack_id": "UNOOR"},
            ],
        )

    def test_only_a_stored_name_that_is_the_users_slack_handle_takes_the_slack_name(self):
        users = {
            "UAMY": (("amy", "amy"), "Amy Member", "amy"),  # stored as the handle: replaced
            "UPRIYA": (("priya", "Priya Shah"), "Priya S.", "priya"),
            "UWANG": (("wang", "王伟"), "Wang Wei", "wang.wei"),
            "UMADONNA": (("madonna", "madonna"), "Madonna Ciccone", "mciccone"),
            "UJOSE": (("jose", "josé"), "José Ruiz", "jruiz"),
        }
        args = command_args("Sales | members: " + " ".join(f"<@{user}>" for user in users)) | {
            "profiles": {user: row[1] for user, row in users.items()},
            "handles": {user: row[2] for user, row in users.items()},
        }
        lookup = FakeLookup(slack={})
        lookup.linked_person = lambda user: users[user][0] if user in users else None
        self.assertEqual(
            [member["name"] for member in team_data(args, lookup)["members"]],
            ["Amy Member", "Priya Shah", "王伟", "madonna", "josé"],
        )

    def test_owners_default_to_the_first_member_and_a_named_owner_joins_the_team(self):
        runs = team_data(self.args, self.lookup)["runs"]
        self.assertEqual([(row["name"], row["kind"], row["owner"]) for row in runs], [("Renewals", "process", "priya"), ("HubSpot", "system", "slack-unoor"), ("Pricing playbook", "playbook", "priya")])

    def test_decisions_are_about_everything_listed_and_dated_by_the_message_without_a_day(self):
        first, second = team_data(self.args, self.lookup)["decisions"]
        self.assertEqual(first, {"name": "Annual plans only", "about": ["Renewals", "HubSpot", "Pricing playbook"], "decided_on": "2026-09-01", "proposer": "priya"})
        self.assertEqual(second["decided_on"], "2026-09-21")

    def test_a_mention_with_no_person_and_no_profile_or_a_decision_with_nothing_to_be_about_is_refused(self):
        with self.assertRaises(TeamFileError) as caught:
            team_data(command_args("Sales | members: <@UGHOST> | decision: annual plans only"), self.lookup)
        self.assertEqual(
            caught.exception.problems,
            ["<@UGHOST> is not in Company Brain yet and I couldn't read their Slack profile", "a decision is about what the team runs, so add `owns:` with at least one thing"],
        )

    def test_messages_drop_the_team_files_section_names(self):
        self.assertEqual(for_slack(["kind in [[runs]] 'Renewals' is 'x'", "[team] needs a name"]), "kind in 'Renewals' is 'x'; the team needs a name")


if __name__ == "__main__":
    unittest.main()
