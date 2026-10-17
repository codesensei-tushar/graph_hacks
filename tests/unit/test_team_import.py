"""A team file reads into the seed's company shape, or is refused whole with one message per problem."""

import tomllib
import unittest
from pathlib import Path

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.domain.onboarding.team_import import (  # noqa: E402
    MAX_FILE_BYTES,
    Found,
    TeamFileError,
    describe,
    parse_team,
    parse_team_data,
    parse_team_file,
    slug,
)

ROOT = Path(__file__).resolve().parents[2]
SALES = ROOT / "examples" / "teams" / "quillfern-sales.toml"
TEMPLATE = ROOT / "docs" / "templates" / "team.example.toml"

MINIMAL = """
[team]
name = "Sales"

[[members]]
name = "Priya Shah"

[[runs]]
name = "Renewals"
kind = "process"
owner = "Priya Shah"
"""


class FakeLookup:
    """Stands in for the graph: what a reference outside the file finds there."""

    def __init__(self, things=(), run_by=None, slack=None, companies=("quillfern",), emails=None, people=(), scopes=None):
        self.things = list(things)
        self.runs = run_by or {}
        self.slack = slack or {}
        self.companies = companies
        self.emails = emails or {}
        self.scopes = scopes or {}
        # Everyone already here: named outright, by an email, by a Slack link, or found by name.
        self.people = {*people, *self.slack.values(), *(p for ids in self.emails.values() for p in ids)}
        self.people |= {item.id for item in self.things if item.label == "Person"}

    def has_company(self, company_id):
        return company_id in self.companies

    def people_with_email(self, email):
        return list(self.emails.get(email.lower(), []))

    def find(self, labels, text):
        return [item for item in self.things if item.label in labels and (item.id == slug(text) or item.name.lower() == text.strip().lower())]

    def run_by(self, service_id):
        return self.runs.get(service_id, [])

    def slack_person(self, slack_user):
        return self.slack.get(slack_user)

    def linked_person(self, slack_user):
        return (self.slack[slack_user], self.slack[slack_user]) if slack_user in self.slack else None

    def person_exists(self, person_id):
        return person_id in self.people

    def slack_users_of(self, person_id):
        return [user for user, person in self.slack.items() if person == person_id]

    def scope_of(self, label, ident):
        return self.scopes.get((label, ident))


# What the Sales file names outside itself: Tomas, Quillfern's engineering manager.
QUILLFERN = FakeLookup([Found("Person", "tomas", "Tomas Reyes")])


def parse_team_data_in_room(text: str, lookup, scope: str):
    return parse_team_data(tomllib.loads(text), "quillfern", lookup, scope)


def problems(text: str | bytes, lookup=None) -> list[str]:
    try:
        parse_team(text, "quillfern", lookup)
    except TeamFileError as error:
        return error.problems
    raise AssertionError("the file was not refused")


class SalesExampleTest(unittest.TestCase):
    def test_the_example_reads_into_the_seed_shape(self):
        team = parse_team_file(SALES, "quillfern", QUILLFERN)
        self.assertEqual(team.COMPANY, {"id": "quillfern"})
        self.assertEqual(team.TEAMS[0]["id"], "sales")
        self.assertEqual(team.TEAMS[0]["members"], ["priya", "dev", "noor"])
        self.assertEqual({s["id"]: s["kind"] for s in team.SERVICES}, {"renewals": "process", "hubspot": "system", "pricing-playbook": "playbook", "enterprise-accounts": "account"})
        self.assertTrue(all(s["team"] == "sales" for s in team.SERVICES))
        self.assertEqual(team.DEPENDS_ON, [("renewals", "hubspot"), ("pricing-playbook", "renewals")])
        self.assertIn(("priya", "OWNS", "renewals"), team.WORK)
        self.assertIn(("dev", "WORKED_ON", "renewals"), team.WORK)
        self.assertEqual(team.SLACK_USERS, [("UPRIYA", "priya"), ("UDEV", "dev"), ("UNOOR", "noor")])
        self.assertEqual((team.DOCUMENTS, team.TICKETS, team.CHATS), ([], [], []))
        self.assertEqual(describe(team), "Sales: 3 people, 1 system, 1 process, 1 account, 1 playbook, 2 decisions")

    def test_an_engineer_already_in_the_graph_links_the_two_teams(self):
        team = parse_team_file(SALES, "quillfern", QUILLFERN)
        self.assertIn(("tomas", "WORKED_ON", "pricing-playbook"), team.WORK)
        self.assertNotIn("tomas", team.TEAMS[0]["members"])
        # Read without the graph, the file must be self-contained, and Tomas is not in it.
        self.assertEqual(problems(SALES.read_bytes()), ["worked_on_by in [[runs]] 'Pricing playbook': 'Tomas Reyes' is not a member in this file"])

    def test_decisions_keep_their_dates_and_what_they_replace(self):
        older, newer = parse_team_file(SALES, "quillfern", QUILLFERN).DECISIONS
        self.assertEqual(newer["supersedes"], older["id"])
        self.assertEqual(older["decided_at"], 1_772_409_600_000)  # 2 Mar 2026, UTC midnight
        self.assertEqual(newer["decided_at"], 1_788_220_800_000)  # 1 Sep 2026
        self.assertEqual(newer["about"], ["renewals", "pricing-playbook"])
        self.assertEqual(newer["based_on"], ["pricing-playbook"])
        self.assertEqual((newer["proposer"], newer["approver"]), ("noor", "priya"))

    def test_the_blank_template_reads_as_it_stands(self):
        team = parse_team_file(TEMPLATE, "quillfern")
        self.assertEqual(describe(team), "Support: 2 people, 1 system, 1 process, 1 account, 1 playbook, 1 decision")

    def test_rows_carry_only_the_importers_own_field_names(self):
        team = parse_team_file(SALES, "quillfern", QUILLFERN)
        allowed = {"id", "name", "role", "email", "summary", "members", "kind", "team", "aliases", "about", "based_on", "proposer", "approver", "supersedes", "decided_at"}
        for row in [*team.PEOPLE, *team.TEAMS, *team.SERVICES, *team.DECISIONS]:
            self.assertLessEqual(set(row), allowed, row)


class DefaultsTest(unittest.TestCase):
    def test_ids_are_slugged_from_names_and_decisions_are_prefixed(self):
        team = parse_team(MINIMAL + '\n[[decisions]]\nname = "Annual plans only!"\nabout = ["Renewals"]\n', "quillfern")
        self.assertEqual(team.TEAM_ID, "sales")
        self.assertEqual(team.PEOPLE, [{"id": "priya-shah", "name": "Priya Shah"}])
        self.assertEqual(team.DECISIONS[0]["id"], "decision-annual-plans-only")
        self.assertEqual(slug("  HubSpot / CRM (EU) "), "hubspot-crm-eu")

    def test_a_reference_finds_an_entry_by_id_or_by_name(self):
        text = MINIMAL.replace('owner = "Priya Shah"', 'owner = "priya-shah"') + '\n[[runs]]\nname = "HubSpot"\nkind = "system"\n'
        text = text.replace('kind = "process"', 'kind = "process"\ndepends_on = ["hubspot"]')
        team = parse_team(text, "quillfern")
        self.assertEqual(team.DEPENDS_ON, [("renewals", "hubspot")])
        self.assertEqual(team.WORK, [("priya-shah", "OWNS", "renewals")])

    def test_a_blank_kind_reads_as_a_service_and_a_toml_date_works(self):
        text = MINIMAL.replace('kind = "process"\n', "") + '\n[[decisions]]\nname = "X"\nabout = "Renewals"\ndecided_on = 2026-09-01\n'
        team = parse_team(text, "quillfern")
        self.assertEqual(team.SERVICES[0]["kind"], "service")
        self.assertEqual(team.DECISIONS[0]["decided_at"], 1_788_220_800_000)


class RefusalTest(unittest.TestCase):
    def test_unknown_fields_are_named_with_a_hint(self):
        found = problems(MINIMAL.replace("owner =", "ownr =") + '\n[[owns]]\nname = "Old"\n')
        self.assertIn("unknown field 'owns' in the file; did you mean 'runs'?", found)
        self.assertIn("unknown field 'ownr' in [[runs]] 'Renewals'; did you mean 'owner'?", found)

    def test_a_query_shaped_field_name_is_refused_and_never_copied(self):
        found = problems(MINIMAL + '"x} DETACH DELETE n //" = 1\n')
        self.assertTrue(any(p.startswith("unknown field 'x} DETACH DELETE n //' in [[runs]] 'Renewals'") for p in found), found)

    def test_every_problem_is_reported_at_once(self):
        found = problems(
            MINIMAL.replace('"process"', '"department"')
            + '\n[[runs]]\nname = "Acme"\nkind = "account"\ndepends_on = ["Renewals"]\n'
            + '\n[[decisions]]\nname = "Annual only"\ndecided_on = "1 Sep 2026"\n'
        )
        self.assertIn("kind in [[runs]] 'Renewals' is 'department'; use one of service, system, process, product, account, policy, playbook", found)
        self.assertIn("[[runs]] 'Acme' is an account, and an account depends on nothing; leave depends_on off it", found)
        self.assertIn("[[decisions]] 'Annual only' needs about: the things in [[runs]] it is about", found)
        self.assertIn('decided_on in [[decisions]] \'Annual only\' should be a date like "2026-09-01"', found)

    def test_nothing_depends_on_an_account(self):
        parse_team(MINIMAL + '\n[[runs]]\nname = "Acme"\nkind = "account"\n', "quillfern")  # an account on its own is fine
        found = problems(MINIMAL.replace('owner = "Priya Shah"', 'owner = "Priya Shah"\ndepends_on = ["Acme"]') + '\n[[runs]]\nname = "Acme"\nkind = "account"\n')
        self.assertIn("depends_on in [[runs]] 'Renewals': 'Acme' is an account, and nothing depends on an account", found)

    def test_a_reference_to_nothing_is_refused(self):
        found = problems(MINIMAL.replace('owner = "Priya Shah"', 'owner = "Nobody"'))
        self.assertEqual(found, ["owner in [[runs]] 'Renewals': 'Nobody' is not a member in this file"])

    def test_duplicates_bad_values_and_missing_sections(self):
        found = problems(MINIMAL + '\n[[members]]\nname = "Priya Shah"\nslack_id = "nope"\nemail = "priya"\n')
        self.assertIn("[[members]] 'Priya Shah': two entries have the id 'priya-shah'; give one of them its own id", found)
        found = problems('[team]\nname = "S"\n[[members]]\nname = "A"\nslack_id = "nope"\nemail = "a"\n')
        self.assertIn("slack_id in [[members]] 'A' should look like U012ABCDEF (Slack profile, ⋮, Copy member ID)", found)
        self.assertIn("email in [[members]] 'A' does not look like an email address", found)
        self.assertEqual(problems('[[members]]\nname = "A"\n'), ["the file needs a [team] section with the team's name"])
        self.assertEqual(problems('[team]\nname = 3\n'), ["name in [team] should be text in quotes"])

    def test_the_file_itself_is_checked_first(self):
        self.assertTrue(problems("[team\nname = 'x'")[0].startswith("this is not valid TOML"))
        self.assertEqual(problems(b"\xff\xfe"), ["the file is not UTF-8 text"])
        too_big = b"# " + b"x" * MAX_FILE_BYTES
        self.assertEqual(problems(too_big), [f"the file is {len(too_big)} bytes; the limit is {MAX_FILE_BYTES}"])


class GraphReferenceTest(unittest.TestCase):
    """With a lookup, a reference may name something already in the graph."""

    def test_a_reference_outside_the_file_resolves_to_what_the_graph_holds(self):
        lookup = FakeLookup([Found("Service", "billing-service", "Billing Service", "service"), Found("Person", "tomas", "Tomas Reyes")])
        text = MINIMAL.replace('owner = "Priya Shah"', 'owner = "Priya Shah"\nworked_on_by = ["Tomas Reyes"]\ndepends_on = ["Billing Service"]')
        team = parse_team(text, "quillfern", lookup)
        self.assertEqual(team.DEPENDS_ON, [("renewals", "billing-service")])
        self.assertIn(("tomas", "WORKED_ON", "renewals"), team.WORK)
        self.assertNotIn("tomas", team.TEAMS[0]["members"])

    def test_missing_ambiguous_and_account_references_are_refused(self):
        lookup = FakeLookup([Found("Service", "a", "Twin", "service"), Found("Service", "b", "Twin", "service"), Found("Service", "acme", "Acme", "account")])
        found = problems(MINIMAL.replace('owner = "Priya Shah"', 'owner = "Priya Shah"\ndepends_on = ["Twin", "Ghost", "Acme"]'), lookup)
        self.assertIn("depends_on in [[runs]] 'Renewals': 'Twin' matches more than one thing (a, b); use its id", found)
        self.assertIn("depends_on in [[runs]] 'Renewals': no thing named 'Ghost' in this file or in Company Brain", found)
        self.assertIn("depends_on in [[runs]] 'Renewals': 'Acme' is an account, and nothing depends on an account", found)

    def test_a_thing_another_team_runs_and_a_taken_slack_id_are_refused(self):
        lookup = FakeLookup(run_by={"renewals": [("payments", "Payments")]}, slack={"UPRIYA": "maya"})
        found = problems(MINIMAL.replace('name = "Priya Shah"', 'name = "Priya Shah"\nslack_id = "UPRIYA"'), lookup)
        self.assertIn(
            "[[runs]] 'Renewals': 'renewals' is already run by Payments. To lean on it, list it under depends_on; to add a separate one, give this entry its own id",
            found,
        )
        self.assertIn("slack_id in [[members]] 'Priya Shah' already belongs to 'maya' in Company Brain; if that is this member, add their email or set id = \"maya\"", found)

    def test_a_member_whose_email_is_already_here_is_that_person(self):
        # Slack provisions a placeholder Person per member; the file fills it in instead of adding a second one.
        text = MINIMAL.replace('name = "Priya Shah"', 'name = "Priya Shah"\nemail = "Priya@Example.com"\nslack_id = "UPRIYA"')
        lookup = FakeLookup(emails={"priya@example.com": ["person-5238dd"]}, slack={"UPRIYA": "person-5238dd"})
        team = parse_team(text, "quillfern", lookup)
        self.assertEqual(team.PEOPLE, [{"id": "person-5238dd", "name": "Priya Shah", "email": "Priya@Example.com"}])
        self.assertEqual(team.TEAMS[0]["members"], ["person-5238dd"])
        self.assertEqual(team.WORK, [("person-5238dd", "OWNS", "renewals")])
        self.assertEqual(team.SLACK_USERS, [("UPRIYA", "person-5238dd")])

    def test_an_import_links_a_slack_user_only_to_a_person_it_creates(self):
        # A DM reads the private repos of the person its Slack user IS, so pairing a Slack user
        # with someone already here would hand over their access.
        text = MINIMAL.replace('name = "Priya Shah"', 'name = "Priya Shah"\nemail = "priya@example.com"\nslack_id = "UPRIYA"')
        found = problems(text, FakeLookup(emails={"priya@example.com": ["priya"]}, slack={"UPRIYA": "maya"}))
        self.assertIn(
            "slack_id for Priya Shah: they are already in Company Brain with no Slack account linked, and an import cannot link one; "
            "leave slack_id out, and they can send `link account` to the bot",
            found,
        )
        tomas = '[team]\nname = "Ops"\n[[members]]\nid = "tomas"\nname = "Tomas Reyes"\nslack_id = "UNOTTOMAS"\n'
        found = problems(tomas, FakeLookup(people=["tomas"], slack={"UTOMAS": "tomas"}))
        self.assertEqual(found, ["slack_id for Tomas Reyes does not match the Slack account already linked to them"])
        self.assertEqual(parse_team(tomas.replace("UNOTTOMAS", "UTOMAS"), "quillfern", FakeLookup(slack={"UTOMAS": "tomas"})).SLACK_USERS, [("UTOMAS", "tomas")])
        self.assertEqual(parse_team(tomas.replace('id = "tomas"\n', ""), "quillfern", FakeLookup(people=["tomas"])).SLACK_USERS, [("UNOTTOMAS", "tomas-reyes")])

    def test_rows_carry_a_private_scope_and_nothing_changes_who_can_see_it(self):
        team = parse_team_data_in_room(MINIMAL, FakeLookup(), "slack_channel_CPRIVATE")
        self.assertEqual((team.TEAMS[0]["scope"], team.SERVICES[0]["scope"]), ("slack_channel_CPRIVATE", "slack_channel_CPRIVATE"))
        self.assertNotIn("scope", team.PEOPLE[0])
        self.assertNotIn("scope", parse_team(MINIMAL, "quillfern", FakeLookup()).TEAMS[0])
        public = FakeLookup(scopes={("Team", "sales"): "org_shared"})
        with self.assertRaises(TeamFileError) as caught:
            parse_team_data_in_room(MINIMAL, public, "slack_channel_CPRIVATE")
        self.assertEqual(caught.exception.problems, ["'Sales' is already in Company Brain for public channels; onboard it from there"])

    def test_an_email_two_people_share_is_refused_unless_the_id_names_one(self):
        text = MINIMAL.replace('name = "Priya Shah"', 'name = "Priya Shah"\nemail = "priya@example.com"\nslack_id = "UPRIYA"')
        shared = FakeLookup(emails={"priya@example.com": ["priya-a", "priya-b"]})
        found = problems(text.replace('slack_id = "UPRIYA"\n', ""), shared)
        self.assertEqual(found, ["email in [[members]] 'Priya Shah' belongs to more than one person in Company Brain (priya-a, priya-b); set id to the right one"])
        chosen = parse_team(text.replace('slack_id = "UPRIYA"\n', 'id = "priya-b"\n'), "quillfern", shared)
        self.assertEqual(chosen.PEOPLE[0]["id"], "priya-b")

    def test_a_company_the_graph_lacks_is_refused(self):
        self.assertEqual(
            problems(MINIMAL, FakeLookup(companies=())),
            ["the graph has no company 'quillfern' to add the team to; check SEED_COMPANY"],
        )

    def test_reimporting_a_team_over_its_own_things_is_allowed(self):
        lookup = FakeLookup(run_by={"renewals": [("sales", "Sales")]}, slack={})
        self.assertEqual(parse_team(MINIMAL, "quillfern", lookup).SERVICES[0]["id"], "renewals")


if __name__ == "__main__":
    unittest.main()
