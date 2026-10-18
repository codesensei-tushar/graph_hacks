"""``/console/api/people``: an admin links a Jira or HubSpot account to the person it is.

Accounts are written through graph.py, as a sync writes them, into a fresh org. The listing
suggests people by name or address domain and never shows an email; the link is an
admin-asserted ``IS``, audited, and the account's OWNS edges move onto the person. Another
org sees and links nothing of it.
Needs FalkorDB on localhost:6379 (``docker compose up -d``).
"""

import unittest

import tests  # noqa: F401  pins the test graph and owner before src loads

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api.v1 import console_people  # noqa: E402
from src.connectors import graph as connector_graph  # noqa: E402
from src.connectors.base import Container, Edge, ExternalUser, Install, Item, Page  # noqa: E402
from src.core import tenancy  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.db import falkordb_client  # noqa: E402
from src.db.falkordb_client import connect  # noqa: E402
from src.domain import orgs  # noqa: E402

ORIGIN = "https://console.test"
CSRF = "csrf-token-of-the-session"


def _rows(org, query: str, **params) -> list:
    with tenancy.org_context(org):
        _, graph = connect()
        return graph.query(query, params).result_set


class ConsolePeopleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        orgs.ensure_control_indexes()
        cls.x = orgs.create_org("People X", created_by="TX:UX")
        cls.y = orgs.create_org("People Y", created_by="TY:UY")
        cls.saved = {name: getattr(settings, name) for name in ("console_enabled", "public_url")}
        for name, value in (("console_enabled", True), ("public_url", ORIGIN)):
            object.__setattr__(settings, name, value)
        cls.app = FastAPI()
        cls.app.include_router(console_people.router)

    @classmethod
    def tearDownClass(cls):
        for name, value in cls.saved.items():
            object.__setattr__(settings, name, value)
        for org in (cls.x, cls.y):
            try:
                falkordb_client.client().select_graph(org.graph).delete()
            except Exception:
                pass
        tenancy.control_graph().query("MATCH (o:Org) WHERE o.id IN $ids DETACH DELETE o", {"ids": [cls.x.id, cls.y.id]})

    def setUp(self):
        for org in (self.x, self.y):
            _rows(org.ref(), "MATCH (n) WHERE NOT n:Company DETACH DELETE n RETURN count(*)")
        _rows(
            self.x.ref(),
            "UNWIND $people AS p CREATE (:Person {id: p[0], name: p[1], email: p[2]}) RETURN count(*)",
            people=[["paula-ortiz", "Paula Ortiz", "paula@dayone.example"], ["sam-okafor", "Sam Okafor", "Sam@DayOne.example"], ["gil", "Gil", "gil@gmail.com"]],
        )
        self.jira = Install(source="jira", id="site1", org=self.x.id, auth="oauth")
        self.hubspot = Install(source="hubspot", id="hub1", org=self.x.id, auth="oauth")
        with tenancy.org_context(self.x.ref()):
            for install, user_id, name, email in (
                (self.jira, "a1", "Paula Ortiz", ""),  # Jira hides the email
                (self.jira, "a2", "Robin Vale", "robin@dayone.example"),
                (self.jira, "a3", "Gee", "gee@gmail.com"),
                (self.jira, "a4", "Sam O.", "sam@dayone.example"),  # linked by email
                (self.jira, "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077", "Automation for Jira", ""),  # an Atlassian app
                (self.jira, "app2", "Jira Automation", ""),
            ):
                connector_graph.upsert_user(install, ExternalUser(id=install.user_id(user_id), source=install.source, external_id=user_id, name=name, email=email))
            container = self.hubspot.container_id("crm")
            connector_graph.write_containers(self.hubspot, [Container(id=container, name="CRM", external_id="crm")])
            owner = ExternalUser(id=self.hubspot.user_id("o9"), source="hubspot", external_id="o9", name="Paula O", email="paula.o@other.example")
            acme = Item(label="Service", id=self.hubspot.item_id("company:1"), external_id="company:1", container=container, name="Acme Corp", kind="account")
            connector_graph.write_page(self.hubspot, Page(items=[acme], users=[owner], edges=[Edge("OWNS", owner.id, acme.id)]))
        self.paula_jira = self.jira.user_id("a1")
        self.owner_id = owner.id
        self.session = {"org": self.x.id, "person": {"id": "p-ada", "name": "Ada"}, "role": "admin", "csrf": CSRF, "slack_user": "UADA", "team": "TX"}
        self.app.dependency_overrides[console_people.console_admin] = self._admin
        self.client = TestClient(self.app)
        self.headers = {"x-csrf-token": CSRF, "origin": ORIGIN}

    def _admin(self):
        if self.session["role"] not in ("owner", "admin"):
            raise HTTPException(status_code=403, detail="admins only")
        return self.session

    def _link(self, user_id, person_id):
        return self.client.post("/console/api/people/link", json={"external_user_id": user_id, "person_id": person_id}, headers=self.headers)

    def test_unlinked_accounts_come_with_suggestions_and_no_emails(self):
        body = self.client.get("/console/api/people/unlinked").json()
        self.assertNotIn("@", str(body), "no email, not even a domain, is returned")
        rows = {row["name"]: row for row in body["unlinked"]}
        self.assertEqual(sorted(rows), ["Gee", "Paula O", "Paula Ortiz", "Robin Vale"], "Sam is linked by email already; apps are not people")
        self.assertEqual(rows["Paula Ortiz"]["suggestions"], [{"person_id": "paula-ortiz", "name": "Paula Ortiz", "reason": "same_name"}])
        self.assertEqual(
            rows["Robin Vale"]["suggestions"],
            [{"person_id": "paula-ortiz", "name": "Paula Ortiz", "reason": "same_email_domain"}, {"person_id": "sam-okafor", "name": "Sam Okafor", "reason": "same_email_domain"}],
        )
        self.assertEqual(rows["Gee"]["suggestions"], [], "a public mail domain suggests nobody")
        self.assertEqual((rows["Paula O"]["source"], rows["Paula O"]["items"]), ("hubspot", 1))
        self.assertEqual(rows["Paula O"]["id"], self.owner_id)
        self.assertEqual(body["people"], [{"id": "gil", "name": "Gil"}, {"id": "paula-ortiz", "name": "Paula Ortiz"}, {"id": "sam-okafor", "name": "Sam Okafor"}])

    def test_an_admin_links_an_account_and_its_edges_move_to_the_person(self):
        response = self._link(self.owner_id, "paula-ortiz")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"ok": True, "external_user_id": self.owner_id, "person": {"id": "paula-ortiz", "name": "Paula Ortiz"}, "changed": True})
        self.assertEqual(_rows(self.x.ref(), "MATCH (:ExternalUser {id: $id})-[r:IS]->(p:Person) RETURN r.via, r.by, p.id", id=self.owner_id), [["admin", "p-ada", "paula-ortiz"]])
        self.assertEqual(_rows(self.x.ref(), "MATCH (p:Person)-[:OWNS]->(s:Service) RETURN p.id, s.name"), [["paula-ortiz", "Acme Corp"]])
        self.assertEqual(_rows(self.x.ref(), "MATCH (:ExternalUser)-[r:OWNS]->() RETURN count(r)"), [[0]])
        names = [row["name"] for row in self.client.get("/console/api/people/unlinked").json()["unlinked"]]
        self.assertNotIn("Paula O", names)
        audit = tenancy.control_graph().query(
            "MATCH (a:AuditEvent {org: $org, action: 'people.link'}) RETURN a.target, a.actor", {"org": self.x.id}
        ).result_set
        self.assertIn([f"{self.owner_id} -> paula-ortiz", "p-ada"], audit)

        again = self._link(self.owner_id, "paula-ortiz").json()
        self.assertFalse(again["changed"], "linking twice changes nothing")
        moved = self._link(self.owner_id, "sam-okafor")
        self.assertTrue(moved.json()["changed"], "an admin link may be corrected")
        self.assertEqual(_rows(self.x.ref(), "MATCH (:ExternalUser {id: $id})-[:IS]->(p:Person) RETURN p.id", id=self.owner_id), [["sam-okafor"]])
        self.assertEqual(_rows(self.x.ref(), "MATCH (p:Person)-[:OWNS]->(:Service) RETURN p.id"), [["sam-okafor"]])

    def test_refusals(self):
        self.assertEqual(self._link(self.jira.user_id("a4"), "paula-ortiz").status_code, 409, "an email link is not overridden")
        self.assertEqual(self._link("jira:nope:user:x", "paula-ortiz").status_code, 404)
        self.assertEqual(self._link(self.paula_jira, "nobody").status_code, 404)
        app = self.jira.user_id("557058:f58131cb-b67d-43c7-b30d-6b58d40bd077")
        self.assertEqual(self._link(app, "paula-ortiz").status_code, 404, "an app is never linked")
        _rows(self.x.ref(), "MATCH (u:ExternalUser {id: $id}) SET u.is_bot = true RETURN count(u)", id=self.jira.user_id("a3"))
        unlinked = [row["name"] for row in self.client.get("/console/api/people/unlinked").json()["unlinked"]]
        self.assertNotIn("Gee", unlinked, "is_bot hides it")
        self.assertEqual(self._link(self.paula_jira, "").status_code, 400)
        self.assertEqual(self.client.post("/console/api/people/link", content="x", headers={**self.headers, "content-type": "text/plain"}).status_code, 415)

        self.session = {**self.session, "org": self.y.id}
        self.assertEqual(self.client.get("/console/api/people/unlinked").json(), {"unlinked": [], "people": []}, "another org sees nothing")
        self.assertEqual(self._link(self.paula_jira, "paula-ortiz").status_code, 404, "nor links across orgs")
        self.assertEqual(_rows(self.x.ref(), "MATCH (:ExternalUser {id: $id})-[r:IS]->() RETURN count(r)", id=self.paula_jira), [[0]])

        self.session = {**self.session, "org": self.x.id, "role": "member"}
        self.assertEqual(self.client.get("/console/api/people/unlinked").status_code, 403)
        self.assertEqual(self._link(self.paula_jira, "paula-ortiz").status_code, 403)


if __name__ == "__main__":
    unittest.main()
