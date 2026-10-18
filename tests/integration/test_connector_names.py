"""No answer shows an email address from a connector: a HubSpot owner with no name is named by
the email's local part, and an account synced before that, still named by its address, is shown
the same way.

Quillfern, with a CRM container holding two accounts: one owned by a HubSpot user with no name
(written through ``mapping.owner_user``, as the connector does), and one owned by an account an
older sync named by its whole email.
"""

import json
import re
import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.connectors import graph
from src.connectors.base import Container, Edge, ExternalUser, Install, Item, Page
from src.connectors.hubspot import mapping
from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import ask_api, ask_service, catalogue, explore, find, search  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_connector_names_test{tests.TEST_GRAPH_SUFFIX}"
SLACK = ["org_shared", "org_internal"]
INSTALL = Install(source="hubspot", config={})
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
QUESTIONS = (
    "who owns Bluefin Labs",
    "who knows Bluefin Labs",
    "who owns Copperleaf",
    "explore Copperleaf",
    "everything about Bluefin Labs",
    "list the services",
    "search for Bluefin",
    "what changed this week",
    "who is om",
    "what if om leaves",
)


def _text(result) -> str:
    return json.dumps(result.to_dict(), default=str) + result.answer


class ConnectorNamesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        search.ensure_search_indexes()
        ask_api.ANSWERS.clear()
        crm = Container(id=INSTALL.container_id("crm"), name="HubSpot CRM", external_id="crm", kind="crm")
        graph.write_containers(INSTALL, [crm])
        nameless = mapping.owner_user(INSTALL, {"id": "301", "email": "om@refactron.example", "firstName": "", "lastName": ""})
        legacy = ExternalUser(id=INSTALL.user_id("302"), source="hubspot", external_id="302", name="kim@refactron.example", email="kim@refactron.example")
        bluefin = Item(label="Service", kind="account", id=INSTALL.item_id("company:1"), external_id="company:1", container=crm.id, name="Bluefin Labs", state="open", status="customer")
        copper = Item(label="Service", kind="account", id=INSTALL.item_id("company:2"), external_id="company:2", container=crm.id, name="Copperleaf", state="open", status="opportunity")
        graph.write_page(
            INSTALL,
            Page(
                items=[bluefin, copper],
                users=[nameless, legacy],
                edges=[Edge("OWNS", nameless.id, bluefin.id), Edge("OWNS", legacy.id, copper.id), Edge("WORKED_ON", legacy.id, bluefin.id, {"evidence": "deal", "stage": "Contract sent"})],
            ),
        )

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in reversed(cls._patches):
                patch.stop()

    def test_the_nameless_owner_is_named_by_the_local_part_with_its_tool(self):
        answer = ask_service.ask("who owns Bluefin Labs", SLACK)
        self.assertIn("om (HubSpot)", answer.answer)

    def test_no_answer_ever_contains_an_email_from_a_connector_name(self):
        for question in QUESTIONS:
            with self.subTest(question=question):
                tool, args = ask_service.route(question)
                result = catalogue.get_tool(tool).run(args, SLACK)
                self.assertIsNone(EMAIL.search(_text(result)), f"{question!r} showed an email: {_text(result)[:400]}")
        for result in (
            explore.explore("Copperleaf", SLACK),
            explore.neighbors("Bluefin Labs", "WORKED_ON", SLACK),
            find.find("Service", {"kind": "account"}, SLACK),
        ):
            with self.subTest(tool=result.tool):
                self.assertIsNone(EMAIL.search(_text(result)), _text(result)[:400])

    def test_an_account_still_named_by_its_email_is_shown_by_the_local_part(self):
        self.assertEqual(ask_service.account_name("kim@refactron.example", "ExternalUser", "hubspot"), "kim (HubSpot)")
        self.assertIn("kim (HubSpot)", ask_service.ask("who owns Copperleaf", SLACK).answer)


if __name__ == "__main__":
    unittest.main()
