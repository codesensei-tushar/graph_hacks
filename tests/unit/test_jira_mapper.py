"""Jira JSON to connector records: pure, from synthetic fixtures in the API's own shape."""

import unittest

from tests.fixtures.jira import CONFIG, SITE, load

from src.connectors.base import Edge, Install
from src.connectors.jira import mapper

INSTALL = Install(source="jira", config=CONFIG)
ISSUES = {issue["key"]: issue for issue in load("issues.json")}
PROJECTS = {project["key"]: project for project in load("projects.json")["values"]}


def _pay():
    return mapper.container(INSTALL, PROJECTS["PAY"], SITE)


class ContainerTest(unittest.TestCase):
    def test_project_becomes_a_container_with_install_ids(self):
        project = _pay()
        self.assertEqual(project.id, "jira:default:container:10000")
        self.assertEqual((project.name, project.external_id, project.kind), ("Quillfern Payments", "10000", "project"))
        self.assertEqual(project.url, f"{SITE}/browse/PAY")
        self.assertFalse(project.restricted)


class IssueTest(unittest.TestCase):
    def test_epic_maps_to_an_open_ticket_named_by_its_key(self):
        mapped = mapper.issue(INSTALL, ISSUES["PAY-1"], _pay(), SITE)
        item = mapped.item
        self.assertEqual((item.label, item.id, item.external_id, item.name), ("Ticket", "jira:default:10101", "10101", "PAY-1"))
        self.assertEqual(item.title, "Renewal retries without double charges")
        self.assertEqual((item.state, item.status), ("open", "In Progress"))
        self.assertEqual(item.url, f"{SITE}/browse/PAY-1")
        self.assertEqual(item.container, "jira:default:container:10000")
        # 09:00 in India is 03:30 UTC.
        self.assertEqual(item.updated_at, "2026-10-18T03:30:00.000Z")
        self.assertEqual(item.summary, "Retried renewal webhooks must never charge a customer twice.")
        self.assertEqual(item.about, ("Billing Service",))
        self.assertEqual(item.props["issue_type"], "Epic")
        self.assertEqual(item.props["priority"], "High")
        self.assertEqual(item.props["parent_key"], "")
        self.assertEqual(item.props["labels"], ["renewals"])
        self.assertNotIn("number", item.props)

    def test_reporter_opened_and_assignee_assigned(self):
        mapped = mapper.issue(INSTALL, ISSUES["PAY-1"], _pay(), SITE)
        users = {user.name: user for user in mapped.users}
        self.assertEqual(users["Maya Ortiz"].id, "jira:default:user:712020:0b1c-maya")
        self.assertEqual(users["Maya Ortiz"].email, "maya@quillfern.example.com")
        # Jira hid Arjun's email: no email, so no link to a Person by anything else.
        self.assertEqual(users["Arjun Mehta"].email, "")
        self.assertIn(Edge("OPENED", users["Maya Ortiz"].id, "jira:default:10101"), mapped.edges)
        self.assertIn(Edge("ASSIGNED", users["Arjun Mehta"].id, "jira:default:10101"), mapped.edges)

    def test_done_is_closed_and_the_parent_key_is_kept(self):
        item = mapper.issue(INSTALL, ISSUES["PAY-2"], _pay(), SITE).item
        self.assertEqual((item.state, item.status), ("closed", "Done"))
        self.assertEqual(item.props["parent_key"], "PAY-1")
        self.assertEqual(item.summary, "")

    def test_blocks_links_point_from_the_blocker_either_way_round(self):
        # PAY-3 blocks PAY-2 (outward), PLAT-7 blocks PAY-3 (inward); "Relates" is not a path edge.
        blocks = [edge for edge in mapper.issue(INSTALL, ISSUES["PAY-3"], _pay(), SITE).edges if edge.rel == "BLOCKS"]
        self.assertEqual(
            blocks,
            [Edge("BLOCKS", "jira:default:10103", "jira:default:10102"), Edge("BLOCKS", "jira:default:10207", "jira:default:10103")],
        )
        # PAY-2 reads the same link from the other side.
        self.assertEqual(mapper.blocks(ISSUES["PAY-2"]), [("10103", "10102")])

    def test_no_component_falls_back_to_the_title(self):
        mapped = mapper.issue(INSTALL, ISSUES["PAY-5"], _pay(), SITE)
        self.assertEqual(mapped.item.about, ("Document refund retries in the billing runbook",))
        # An app account can open an issue; it is an account like any other, with no email.
        self.assertEqual([(user.name, user.email) for user in mapped.users], [("Automation for Jira", "")])

    def test_description_is_flattened_to_text(self):
        summary = mapper.issue(INSTALL, ISSUES["PAY-3"], _pay(), SITE).item.summary
        self.assertEqual(
            summary,
            "Waiting on @Maya Ortiz for the Postgres upgrade.\nSee https://quillfern.atlassian.net/browse/PLAT-7\nrenewal_id column",
        )

    def test_security_level_and_other_projects_are_skipped(self):
        self.assertEqual(mapper.skip_reason(ISSUES["PAY-4"], _pay()), mapper.SECURITY)
        self.assertEqual(mapper.skip_reason(ISSUES["PLAT-7"], _pay()), mapper.OTHER_PROJECT)
        self.assertIsNone(mapper.skip_reason(ISSUES["PAY-1"], _pay()))


class PeopleTest(unittest.TestCase):
    ARJUN = {"accountId": "712020:7f3e-arjun", "displayName": "Arjun Mehta"}

    def test_an_admin_mapped_email_fills_in_a_hidden_one(self):
        mapped = mapper.user(INSTALL, self.ARJUN, {"712020:7f3e-arjun": "arjun@quillfern.example.com"})
        self.assertEqual(mapped.email, "arjun@quillfern.example.com")

    def test_a_visible_email_wins_over_the_mapping(self):
        visible = {**self.ARJUN, "emailAddress": "arjun@quillfern.example.com"}
        self.assertEqual(mapper.user(INSTALL, visible, {"712020:7f3e-arjun": "someone@else.example.com"}).email, "arjun@quillfern.example.com")

    def test_no_account_id_is_no_user(self):
        self.assertIsNone(mapper.user(INSTALL, {"displayName": "Former user"}))
        self.assertIsNone(mapper.user(INSTALL, None))


class TimeTest(unittest.TestCase):
    def test_offsets_normalise_to_utc(self):
        self.assertEqual(mapper.format_time(mapper.parse_time("2026-10-17T12:00:00.000-0700")), "2026-10-17T19:00:00.000Z")
        self.assertIsNone(mapper.parse_time("not a time"))
        self.assertEqual(mapper.updated_at({"fields": {}}), "")


if __name__ == "__main__":
    unittest.main()
