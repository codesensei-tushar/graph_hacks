"""How a connector page is written: batched, skipped when nothing changed, and never half visible.

On a graph of its own: a few Services and People, one install, one container. Pages are written
with ``graph.write_page`` and the graph is read back. Query counts come from the proxy's hooks.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph before src loads

from src.connectors import graph  # noqa: E402
from src.connectors.base import Container, Edge, ExternalUser, Install, Item, Page  # noqa: E402
from src.db import falkordb_client  # noqa: E402

GRAPH = f"company_connector_writes_test{tests.TEST_GRAPH_SUFFIX}"
INSTALL = Install("jira")
PROJECT = Container(INSTALL.container_id("10"), "Ops", "10", kind="project")


def _user(n: int, email: str | None = None) -> ExternalUser:
    return ExternalUser(INSTALL.user_id(f"acc{n}"), "jira", f"acc{n}", f"Account {n}", email if email is not None else f"p{n}@x.example.com")


def _ticket(n: int, title: str = "", about=("Billing Service",), updated="2026-09-01T10:00:00.000+0000") -> Item:
    return Item("Ticket", INSTALL.item_id(str(n)), str(n), PROJECT.id, f"OPS-{n}", title or f"Ticket {n}", updated_at=updated, about=about)


class _Counted:
    """Every query through the proxy while the block runs: (method, text)."""

    def __enter__(self):
        self.calls = []
        self._hook = lambda call: self.calls.append((call.method, " ".join(call.query.split()), call.result))
        falkordb_client.add_query_hook(after=self._hook)
        return self

    def __exit__(self, *exc):
        falkordb_client.remove_query_hook(self._hook)

    @property
    def writes(self):
        """The queries that changed something in the graph."""
        return [query for method, query, result in self.calls if method == "query" and result is not None and falkordb_client._changed(result)]


class ConnectorWritesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patch = mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH)
        cls._patch.start()

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            cls._patch.stop()

    def setUp(self):
        _, g = falkordb_client.connect()
        g.query("MATCH (n) DETACH DELETE n")
        graph.ensure_indexes()
        for sid, name in (("billing-service", "Billing Service"), ("graph-store", "Graph Store"), ("auth-service", "Auth Service")):
            g.query("CREATE (:Service {id: $id, name: $name, scope: 'org_shared'})", {"id": sid, "name": name})
        for n in range(6):
            # Mixed case on purpose: accounts link on the lower-cased email.
            g.query("CREATE (:Person {id: $id, name: $name, email: $email})", {"id": f"p{n}", "name": f"Person {n}", "email": f"P{n}@X.example.com"})
        graph.write_containers(INSTALL, [PROJECT])
        self.g = g

    def _page(self, n_items: int = 20, edit: frozenset = frozenset(), assignee=lambda n: n % 5) -> Page:
        page = Page()
        users = {}
        for n in range(n_items):
            page.items.append(_ticket(n, title=f"Ticket {n}" + (" (edited)" if n in edit else "")))
            for rel, who in (("OPENED", 5), ("ASSIGNED", assignee(n))):
                user = _user(who)
                users[user.id] = user
                page.edges.append(Edge(rel, user.id, INSTALL.item_id(str(n))))
        page.users = list(users.values())
        return page

    def _one(self, query: str, params: dict | None = None):
        return self.g.query(query, params or {}).result_set

    def _generation(self) -> int:
        return falkordb_client.generation(GRAPH) or 0

    # --- batching and the unchanged poll ------------------------------------------------------

    def test_a_page_is_a_handful_of_queries_whatever_its_size(self):
        with _Counted() as small:
            graph.write_page(INSTALL, self._page(4))
        self.setUp()
        with _Counted() as large:
            graph.write_page(INSTALL, self._page(40))
        self.assertLessEqual(len(large.calls), 20, large.calls)
        self.assertEqual(len(large.calls), len(small.calls))
        self.assertEqual(self._one("MATCH (t:Ticket) RETURN count(t)")[0][0], 40)
        self.assertEqual(self._one("MATCH (:ExternalUser)-[r:ASSIGNED]->(:Ticket) RETURN count(r)")[0][0], 40)
        self.assertEqual(self._one("MATCH (:Ticket)-[r:ABOUT]->(:Service {id: 'billing-service'}) RETURN count(r)")[0][0], 40)

    def test_a_poll_that_found_nothing_new_writes_nothing_and_bumps_no_generation(self):
        graph.write_page(INSTALL, self._page())
        before = self._generation()
        with _Counted() as again:
            report = graph.write_page(INSTALL, self._page())
        self.assertEqual(again.writes, [], "an unchanged page writes nothing")
        self.assertEqual((report["items"], report["unchanged"], report["edges"]), (0, 20, 0))
        self.assertEqual(self._generation(), before)

    def test_a_changed_item_is_rewritten_alone_and_the_page_bumps_the_generation_once(self):
        graph.write_page(INSTALL, self._page())
        before = self._generation()
        report = graph.write_page(INSTALL, self._page(edit=frozenset({3, 4})))
        self.assertEqual((report["items"], report["unchanged"]), (2, 18))
        self.assertEqual(self._generation(), before + 1)
        self.assertEqual(self._one("MATCH (t:Ticket {name: 'OPS-3'}) RETURN t.title")[0][0], "Ticket 3 (edited)")

    def test_a_reassignment_replaces_the_edge_and_leaves_no_stale_one(self):
        graph.write_page(INSTALL, self._page())
        report = graph.write_page(INSTALL, self._page(assignee=lambda n: 4 if n == 0 else n % 5))
        self.assertEqual(report["items"], 1)
        rows = self._one("MATCH (u:ExternalUser)-[:ASSIGNED]->(:Ticket {name: 'OPS-0'}) RETURN u.external_id")
        self.assertEqual(rows, [["acc4"]])
        self.assertEqual(self._one("MATCH (:ExternalUser)-[r:OPENED]->(:Ticket {name: 'OPS-0'}) RETURN count(r)")[0][0], 1)

    def test_a_new_service_in_the_graph_reaches_tickets_whose_text_did_not_change(self):
        graph.write_page(INSTALL, Page(items=[_ticket(1, about=("Search Service",))]))
        self.assertEqual(self._one("MATCH (:Ticket)-[r:ABOUT]->() RETURN count(r)")[0][0], 0)
        self.g.query("CREATE (:Service {id: 'search-service', name: 'Search Service', scope: 'org_shared'})")
        graph.write_page(INSTALL, Page(items=[_ticket(1, about=("Search Service",))]))
        self.assertEqual(self._one("MATCH (:Ticket)-[:ABOUT]->(s:Service) RETURN s.id"), [["search-service"]])

    def test_an_about_hint_never_matches_part_of_a_name(self):
        graph.write_page(INSTALL, Page(items=[_ticket(1, about=("store",)), _ticket(2, about=("store migration stalls",))]))
        self.assertEqual(self._one("MATCH (:Ticket)-[:ABOUT]->(s:Service) RETURN s.id"), [])
        # The whole name, in a component or in the title's words, still finds it.
        graph.write_page(INSTALL, Page(items=[_ticket(3, about=("Graph Store",)), _ticket(4, about=("graph store latency spikes",))]))
        self.assertEqual(self._one("MATCH (t:Ticket)-[:ABOUT]->(s:Service) RETURN t.name, s.id ORDER BY t.name"), [["OPS-3", "graph-store"], ["OPS-4", "graph-store"]])

    def test_a_component_named_like_a_customer_account_is_not_about_it(self):
        self.g.query("CREATE (:Service {id: 'hubspot:default:acme', name: 'Acme Robotics', kind: 'account', scope: 'org_internal'})")
        graph.write_page(INSTALL, Page(items=[_ticket(1, about=("Acme Robotics",)), _ticket(2, about=("acme robotics export",))]))
        self.assertEqual(self._one("MATCH (:Ticket)-[:ABOUT]->(s:Service) RETURN s.id"), [])

    def test_changed_at_ms_is_the_vendors_time_in_milliseconds(self):
        graph.write_page(INSTALL, Page(items=[_ticket(1), _ticket(2, updated="2026-09-01T10:00:00Z"), _ticket(3, updated="")]))
        rows = dict(self._one("MATCH (t:Ticket) RETURN t.name, t.changed_at_ms"))
        self.assertEqual(rows["OPS-1"], 1_788_256_800_000)
        self.assertEqual(rows["OPS-2"], 1_788_256_800_000)
        self.assertIsNone(rows["OPS-3"])
        self.assertEqual(graph.changed_at_ms("2026-09-01T12:00:00.000+0200"), 1_788_256_800_000)
        self.assertEqual(graph.changed_at_ms("1788256800000"), 1_788_256_800_000)
        self.assertIsNone(graph.changed_at_ms("yesterday"))

    def test_a_new_ticket_arrives_with_its_reporter_and_assignee_in_one_query(self):
        with _Counted() as first:
            graph.write_page(INSTALL, self._page(3))
        item_queries = [query for query in first.writes if "MERGE (n:Ticket" in query]
        self.assertEqual(len(item_queries), 1)
        self.assertFalse([query for query in first.writes if query.startswith("UNWIND $rows AS row MATCH (a:ExternalUser") and "ASSIGNED" in query])
        self.assertEqual(self._one("MATCH (:ExternalUser)-[r:ASSIGNED|OPENED]->(:Ticket) WHERE r.item IS NOT NULL RETURN count(r)")[0][0], 6)

    def test_a_full_resync_repairs_edges_lost_outside_the_sync(self):
        from src.connectors import sync

        graph.write_page(INSTALL, self._page(2))
        # A re-seed deletes and re-creates the Service (same id): its ABOUT edges are gone, the tickets unchanged.
        self.g.query("MATCH (s:Service {id: 'billing-service'}) DETACH DELETE s")
        self.g.query("CREATE (:Service {id: 'billing-service', name: 'Billing Service', scope: 'org_shared'})")
        self.assertEqual(graph.write_page(INSTALL, self._page(2))["unchanged"], 2)  # an ordinary poll cannot tell
        with mock.patch.object(sync, "resolve", return_value=(None, INSTALL)), mock.patch.object(sync, "schedule"):
            sync.sync_now("jira", full=True)
        self.assertEqual(graph.write_page(INSTALL, self._page(2))["items"], 2)
        self.assertEqual(self._one("MATCH (:Ticket)-[r:ABOUT]->(:Service {id: 'billing-service'}) RETURN count(r)")[0][0], 2)

    def test_restricted_read_access_comes_only_from_an_email_link(self):
        restricted = Container(INSTALL.container_id("99"), "Secret", "99", kind="project", restricted=True)
        graph.write_containers(INSTALL, [PROJECT, restricted])
        reader, admin_linked = _user(1), _user(9, email="hidden@vendor.example.com")
        graph.write_readers(INSTALL, restricted.id, [reader, admin_linked])
        self.g.query("CREATE (:SlackUser {id: 'U1'})-[:IS]->(:Person {id: 'p-u1'})")
        self.g.query("MATCH (p:Person {id: 'p1'}), (s:SlackUser {id: 'U1'}) MATCH (s)-[r:IS]->() DELETE r MERGE (s)-[:IS]->(p)")
        self.assertEqual(len(graph.readable_container_scopes_for_slack_user("U1")), 1)  # acc1's email is p1's
        # An admin links the hidden-email reader to themselves (p3): attribution, never read access.
        self.g.query("CREATE (:SlackUser {id: 'U3'})")
        self.g.query("MATCH (s:SlackUser {id: 'U3'}), (p:Person {id: 'p3'}) MERGE (s)-[:IS]->(p)")
        self.g.query("MATCH (u:ExternalUser {id: $id}), (p:Person {id: 'p3'}) MERGE (u)-[r:IS]->(p) SET r.via = 'admin'", {"id": admin_linked.id})
        self.assertEqual(graph.readable_container_scopes_for_slack_user("U3"), [])

    # --- accounts and their people --------------------------------------------------------------

    def test_accounts_link_on_the_lower_cased_email_and_carry_where_it_came_from(self):
        user = ExternalUser(INSTALL.user_id("a"), "jira", "a", "A", "p1@x.example.com", email_from="admin", account_type="atlassian")
        graph.upsert_users(INSTALL, [user])
        rows = self._one("MATCH (u:ExternalUser {id: $id})-[r:IS]->(p:Person) RETURN r.via, p.id, u.email_lc, u.email_from, u.account_type", {"id": user.id})
        self.assertEqual(rows, [["email", "p1", "p1@x.example.com", "admin", "atlassian"]])
        self.assertEqual(self._one("MATCH (p:Person {id: 'p1'}) RETURN p.email_lc")[0][0], "p1@x.example.com")

    def test_a_person_whose_email_changed_is_relinked(self):
        user = _user(1)
        graph.upsert_users(INSTALL, [user])
        self.g.query("MATCH (p:Person {id: 'p1'}) SET p.email = 'someone.else@x.example.com'")
        self.g.query("MATCH (p:Person {id: 'p2'}) SET p.email = 'P1@x.example.com'")
        self.assertEqual(graph.upsert_users(INSTALL, [user]), {user.id: "p2"})
        self.assertEqual(self._one("MATCH (:ExternalUser {id: $id})-[:IS]->(p) RETURN p.id", {"id": user.id}), [["p2"]])

    def test_an_unchanged_account_is_not_written_again(self):
        graph.upsert_users(INSTALL, [_user(1), _user(2)])
        with _Counted() as again:
            graph.upsert_users(INSTALL, [_user(1), _user(2)])
        self.assertEqual(again.writes, [])

    def test_an_admin_link_survives_every_sync_and_takes_the_owner_edges(self):
        """The console's People screen links an account whose email the vendor hides or that differs:
        a later sync never undoes it, drops the email link, and OWNS follows the admin's Person."""
        user = _user(1)  # its email is p1's
        account = Item("Service", INSTALL.item_id("acme"), "acme", PROJECT.id, "Acme", kind="account")
        page = Page(items=[account], users=[user], edges=[Edge("OWNS", user.id, account.id)])
        graph.write_page(INSTALL, page)
        self.assertEqual(self._one("MATCH (p:Person)-[:OWNS]->(:Service {name: 'Acme'}) RETURN p.id"), [["p1"]])
        # An admin links the account to p3, alongside the email link the sync made.
        self.g.query("MATCH (u:ExternalUser {id: $id}), (p:Person {id: 'p3'}) MERGE (u)-[r:IS]->(p) SET r.via = 'admin'", {"id": user.id})
        for _ in range(2):
            graph.write_page(INSTALL, page)
            links = self._one("MATCH (:ExternalUser {id: $id})-[r:IS]->(p:Person) RETURN r.via, p.id", {"id": user.id})
            self.assertEqual(links, [["admin", "p3"]])
            self.assertEqual(self._one("MATCH (p:Person)-[:OWNS]->(:Service {name: 'Acme'}) RETURN p.id"), [["p3"]])

    def test_owner_edges_follow_a_link_that_appears_later(self):
        user = _user(9, email="nobody@x.example.com")
        account = Item("Service", INSTALL.item_id("acme"), "acme", PROJECT.id, "Acme", kind="account")
        page = Page(items=[account], users=[user], edges=[Edge("OWNS", user.id, account.id)])
        graph.write_page(INSTALL, page)
        self.assertEqual(self._one("MATCH (u:ExternalUser)-[:OWNS]->(:Service {name: 'Acme'}) RETURN u.id"), [[user.id]])
        self.g.query("MATCH (p:Person {id: 'p4'}) SET p.email = 'Nobody@x.example.com'")
        graph.write_page(INSTALL, page)
        self.assertEqual(self._one("MATCH (h)-[:OWNS]->(:Service {name: 'Acme'}) RETURN h.id"), [["p4"]])


class DeferredGenerationTest(unittest.TestCase):
    def test_writes_inside_bump_each_graph_once_at_the_end(self):
        bumped = []
        with mock.patch.object(falkordb_client.kv(), "incr", side_effect=lambda key: bumped.append(key)):
            with falkordb_client.deferred_generation():
                for _ in range(3):
                    falkordb_client.bump_generation("a")
                with falkordb_client.deferred_generation():
                    falkordb_client.bump_generation("b")
                self.assertEqual(bumped, [])
            self.assertEqual(sorted(bumped), ["brain:graph_generation:a", "brain:graph_generation:b"])
            bumped.clear()
            with self.assertRaises(RuntimeError), falkordb_client.deferred_generation():
                falkordb_client.bump_generation("c")
                raise RuntimeError("what was written stays written")
            self.assertEqual(bumped, ["brain:graph_generation:c"])
            bumped.clear()
            falkordb_client.bump_generation("d")
            self.assertEqual(bumped, ["brain:graph_generation:d"])


if __name__ == "__main__":
    unittest.main()
