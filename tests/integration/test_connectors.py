"""The connector framework end to end, with a fake tracker on a real FalkorDB.

``FakeConnector`` serves two projects from memory, two issues a page, and can be told to
fail. The tests drive ``sync.sync_step`` the way the scheduler does and read the graph:
backfill, cursor resume after a restart, Retry-After, idempotent re-sync, deletions, the
per-container cap, account linking by email, restricted containers, pull request
references, the webhook and admin routes, and who may read the result: every Slack asker,
never the public ask page, the team pages or MCP.
"""

import dataclasses
import hashlib
import hmac
import json
import time
import unittest
import uuid
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api.v1 import connectors as connectors_route  # noqa: E402
from src.api.v1 import slack_install  # noqa: E402
from src.connectors import graph, registry, sync  # noqa: E402
from src.connectors.base import (  # noqa: E402
    Connector,
    Container,
    CredentialsRejected,
    Edge,
    ExternalUser,
    Install,
    InstallInfo,
    Item,
    Page,
    RetryLater,
)
from src.core import scheduler  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import algorithms, ask_api, catalogue, graph_overview, mcp_tools, team_page  # noqa: E402
from src.domain.memory.read_scope import resolve_brain_read_container_tags  # noqa: E402
from src.domain.memory.writeback import SlackMemoryScope  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_connectors_test{tests.TEST_GRAPH_SUFFIX}"
TOKEN = "fake-api-token-never-shown"
WEBHOOK_SECRET = "fake-webhook-secret-never-shown"
ADMIN = "connector-admin-token"
PAGE_SIZE = 2

ARJUN = {"id": "u-arjun", "name": "arjun.m", "email": "Arjun@Quillfern.example.com"}
ZED = {"id": "u-zed", "name": "Zed Contractor", "email": "zed@contractor.example"}


def _issues() -> dict[str, list[dict]]:
    return {
        "OPS": [
            {"id": "1001", "key": "OPS-1", "title": "Billing exports time out", "components": ["billing"], "reporter": ZED, "assignee": ARJUN,
             "updated": "2026-10-05T10:00:00Z", "blocks": ["1002"]},
            {"id": "1002", "key": "OPS-2", "title": "Token rotation logs people out", "components": ["auth"], "assignee": ZED},
            {"id": "1003", "key": "OPS-3", "title": "Old cleanup", "state": "closed", "status": "Done", "components": ["nothing we know"]},
            {"id": "company:1004", "key": "Acme Corp", "title": "Acme Corporation", "label": "Service", "kind": "account", "owner": ARJUN},
            {"id": "company:1005", "key": "Globex", "title": "Globex Inc", "label": "Service", "kind": "account", "owner": ZED},
        ],
        "SEC": [{"id": "2001", "key": "SEC-1", "title": "Rotate the signing keys", "components": ["auth"], "assignee": ARJUN}],
    }


class FakeConnector(Connector):
    source = "fake"
    required = ("token",)
    poll_seconds = 60
    accepts_webhooks = True

    def __init__(self):
        self.reset()

    def reset(self):
        self.projects = _issues()
        self.restricted = {"SEC"}
        self.readers_of = {"SEC": [ARJUN]}
        self.fail_next: Exception | None = None
        self.reject = False
        self.retry_after: float | None = None
        # Set: backfill pages say where polling should resume, and a poll from there finds nothing new.
        self.poll_from: str | None = None
        self.calls: list[tuple[str, str | None]] = []

    def check_credentials(self, install):
        if self.reject:
            raise CredentialsRejected("GET /myself answered 401")
        return InstallInfo(account="bot@fake.example", site="https://fake.example")

    def _container(self, install, key):
        return Container(id=install.container_id(key), name=f"{key} project", external_id=key, url=f"https://fake.example/{key}", kind="project", restricted=key in self.restricted)

    def containers(self, install):
        return [self._container(install, key) for key in self.projects]

    def fetch(self, install, container, cursor):
        self.calls.append((container.external_id, cursor))
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error
        if cursor and cursor.startswith("poll:"):
            return Page(next_cursor=cursor)
        issues = self.projects[container.external_id]
        start = int(cursor or 0)
        chunk = issues[start : start + PAGE_SIZE]
        page = Page(next_cursor=str(start + len(chunk)), has_more=start + len(chunk) < len(issues), retry_after=self.retry_after)
        if self.poll_from is not None:
            page.poll_cursor = self.poll_from
        for issue in chunk:
            self._emit(install, container, issue, page)
        return page

    def readers(self, install, container):
        users = self.readers_of.get(container.external_id)
        return None if users is None else [self._user(install, user) for user in users]

    def _user(self, install, user):
        return ExternalUser(id=install.user_id(user["id"]), source=self.source, external_id=user["id"], name=user["name"], email=user["email"])

    def _emit(self, install, container, issue, page):
        item_id = install.item_id(issue["id"])
        page.items.append(
            Item(
                label=issue.get("label", "Ticket"), id=item_id, external_id=issue["id"], container=container.id, name=issue["key"],
                title=issue["title"], url=f"https://fake.example/browse/{issue['key']}", state=issue.get("state", "open"),
                status=issue.get("status", "To Do"), updated_at=issue.get("updated", "2026-10-01T00:00:00Z"), summary="x" * 3000,
                kind=issue.get("kind"), about=tuple(issue.get("components", ())), props={"priority": "High", "labels": ["a", "b"]},
            )
        )
        for role, rel in (("reporter", "OPENED"), ("assignee", "ASSIGNED"), ("owner", "OWNS")):
            if issue.get(role):
                page.users.append(self._user(install, issue[role]))
                page.edges.append(Edge(rel, install.user_id(issue[role]["id"]), item_id))
        for target in issue.get("blocks", ()):
            page.edges.append(Edge("BLOCKS", item_id, install.item_id(target)))

    def verify_webhook(self, install, request):
        signature = request.headers.get("x-fake-signature", "")
        expected = "sha256=" + hmac.new(install.get("webhook_secret").encode(), request.body, hashlib.sha256).hexdigest()
        return bool(install.get("webhook_secret")) and hmac.compare_digest(expected, signature)

    def parse_webhook(self, install, request):
        payload = json.loads(request.body)
        page = Page(deleted_ids=[install.item_id(issue_id) for issue_id in payload.get("deleted", [])])
        for issue in payload.get("issues", []):
            self._emit(install, self._container(install, payload["project"]), issue, page)
        return page


FAKE = FakeConnector()
INSTALL = Install(source="fake", config={"token": TOKEN, "webhook_secret": WEBHOOK_SECRET})


def _rows(query: str, params: dict | None = None) -> list:
    _, g = falkordb_client.connect()
    return g.query(query, params or {}).result_set


def _count(query: str, params: dict | None = None) -> int:
    return _rows(query, params)[0][0]


def _drain(limit: int = 30) -> list[dict]:
    reports = []
    for _ in range(limit):
        report = sync.sync_step("fake")
        reports.append(report)
        if report["status"] != "synced":
            break
    return reports


class ConnectorFrameworkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
            mock.patch.object(scheduler, "schedule"),
            mock.patch.object(sync, "MORE_DELAY_SECONDS", 0.0),
        ]
        for patch in cls._patches:
            patch.start()
        registry.register(FAKE)

    @classmethod
    def tearDownClass(cls):
        registry.unconfigure("fake")
        registry._connectors.pop("fake", None)
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in reversed(cls._patches):
                patch.stop()

    def setUp(self):
        seed.reset_and_seed(quillfern)
        FAKE.reset()
        sync._containers.clear()
        registry.configure(INSTALL)
        scheduler.schedule.reset_mock()

    # --- backfill ----------------------------------------------------------------------------

    def test_backfill_writes_items_users_and_edges(self):
        reports = _drain()
        self.assertEqual(reports[-1]["status"], "idle")
        props = _rows("MATCH (t:Ticket {id: 'fake:default:1001'}) RETURN properties(t)")[0][0]
        for name, value in {
            "name": "OPS-1", "title": "Billing exports time out", "url": "https://fake.example/browse/OPS-1", "state": "open", "status": "To Do",
            "source": "fake", "install": "fake:default", "external_id": "1001", "scope": "org_internal", "priority": "High",
            "source_updated_at": "2026-10-05T10:00:00Z",
        }.items():
            self.assertEqual(props[name], value, name)
        self.assertEqual(len(props["summary"]), graph.SUMMARY_MAX_CHARS)
        self.assertIsInstance(props["synced_at"], int)
        self.assertEqual(_rows("MATCH (t:Ticket {id: 'fake:default:1003'}) RETURN t.state, t.status")[0], ["closed", "Done"])
        # Every item is in its container, and the container lists the install's projects.
        self.assertEqual(_count("MATCH (n)-[:IN_CONTAINER]->(:Container {id: 'fake:default:container:OPS'}) RETURN count(n)"), 5)
        self.assertEqual(_count("MATCH (n)-[:IN_CONTAINER]->(:Container {id: 'fake:default:container:SEC'}) RETURN count(n)"), 1)
        # ABOUT from the hints: a component that names a service, and none for one that names nothing.
        self.assertEqual(_rows("MATCH (:Ticket {id: 'fake:default:1001'})-[:ABOUT]->(s:Service) RETURN s.id"), [["billing-service"]])
        self.assertEqual(_rows("MATCH (:Ticket {id: 'fake:default:1002'})-[:ABOUT]->(s:Service) RETURN s.id"), [["auth-service"]])
        self.assertEqual(_count("MATCH (:Ticket {id: 'fake:default:1003'})-[:ABOUT]->() RETURN count(*)"), 0)
        self.assertEqual(_count("MATCH (:Ticket {id: 'fake:default:1001'})-[:BLOCKS]->(:Ticket {id: 'fake:default:1002'}) RETURN count(*)"), 1)
        # An account is a Service of kind 'account', owned by the linked Person, or by the account when unlinked.
        self.assertEqual(_rows("MATCH (s:Service {id: 'fake:default:company:1004'}) RETURN s.kind, s.name"), [["account", "Acme Corp"]])
        self.assertEqual(_rows("MATCH (p:Person)-[:OWNS]->(:Service {id: 'fake:default:company:1004'}) RETURN p.id"), [["arjun"]])
        self.assertEqual(_rows("MATCH (u:ExternalUser)-[:OWNS]->(:Service {id: 'fake:default:company:1005'}) RETURN u.id"), [["fake:default:user:u-zed"]])
        # The cursor is stored per container, and the install records who it acts as.
        self.assertEqual(graph.get_state("fake:default:container:OPS")["cursor"], "5")
        self.assertEqual(graph.get_state("fake:default")["account"], "bot@fake.example")

    def test_accounts_link_to_exactly_one_person_by_email(self):
        _rows("MATCH (p:Person {id: 'sara'}) SET p.email = 'zed@contractor.example'")
        _rows("CREATE (:Person {id: 'zed-twin', name: 'Zed Twin', email: 'ZED@contractor.example'})")
        _drain()
        self.assertEqual(_rows("MATCH (:ExternalUser {id: 'fake:default:user:u-arjun'})-[:IS]->(p:Person) RETURN p.id"), [["arjun"]])
        # Two people share Zed's email, so nobody is linked.
        self.assertEqual(_count("MATCH (:ExternalUser {id: 'fake:default:user:u-zed'})-[:IS]->() RETURN count(*)"), 0)
        self.assertEqual(_count("MATCH (n:Person) RETURN count(n)"), len(quillfern.PEOPLE) + 1)  # no Person was created

    def test_a_connector_edge_prop_never_overrides_the_frameworks(self):
        _drain()
        edge = Edge("WORKED_ON", "fake:default:user:u-zed", "fake:default:company:1005", props={"via": "x", "item": "y", "source": "z", "install": "w", "stage": "won"})
        graph.write_page(INSTALL, Page(edges=[edge]))
        props = _rows("MATCH (:ExternalUser {id: 'fake:default:user:u-zed'})-[r:WORKED_ON]->(:Service {id: 'fake:default:company:1005'}) RETURN properties(r)")[0][0]
        self.assertEqual(
            {name: props[name] for name in ("via", "item", "source", "install", "stage")},
            {"via": "fake:default:user:u-zed", "item": "fake:default:company:1005", "source": "fake", "install": "fake:default", "stage": "won"},
        )

    def test_an_unlinked_owner_is_named_with_its_tool(self):
        _drain()
        slack_tags = resolve_brain_read_container_tags(SlackMemoryScope(kind="shared", channel_id="CPUB"))
        result = catalogue.get_tool("who_owns").run({"name": "who owns Globex"}, slack_tags)
        self.assertEqual(result.answer, "The Globex account is owned by Zed Contractor (Fake).")
        self.assertIn("Zed Contractor (Fake) -[OWNS]-> Globex", [step.cite() for step in result.path])
        linked = catalogue.get_tool("who_owns").run({"name": "who owns Acme Corp"}, slack_tags)
        self.assertEqual(linked.answer, "The Acme Corp account is owned by Arjun Mehta.")

    def test_a_team_brief_names_the_accounts_its_members_own(self):
        _drain()
        slack_tags = resolve_brain_read_container_tags(SlackMemoryScope(kind="shared", channel_id="CPUB"))
        page = team_page.team_page("payments", slack_tags)
        self.assertEqual(([account.name for account in page.accounts], page.accounts_total), (["Acme Corp"], 1))
        self.assertIn("Arjun Mehta -[OWNS]-> Acme Corp", [step.cite() for step in page.path])
        self.assertEqual(page.to_dict()["accounts_total"], 1)
        brief = catalogue.get_tool("onboarding_brief").run({"name": "brief me on the payments team"}, slack_tags)
        self.assertIn("Its members also own 1 account: Acme Corp (owned by Arjun Mehta).", brief.answer)
        # The public page never shows them, and no RUNS edge was made.
        self.assertEqual(team_page.team_page("payments").accounts, [])
        public = catalogue.get_tool("onboarding_brief").run({"name": "brief me on the payments team"}, ["org_shared"])
        self.assertNotIn("Acme", public.answer + repr([step.cite() for step in public.path]))
        self.assertEqual(_count("MATCH (:Team)-[:RUNS]->(:Service {id: 'fake:default:company:1004'}) RETURN count(*)"), 0)

    def test_ownership_moves_to_the_person_once_linked(self):
        _drain()
        _rows("CREATE (:Person {id: 'zed', name: 'Zed Contractor', email: 'zed@contractor.example'})")
        graph.upsert_user(INSTALL, FAKE._user(INSTALL, ZED))
        self.assertEqual(_rows("MATCH (p:Person)-[:OWNS]->(:Service {id: 'fake:default:company:1005'}) RETURN p.id"), [["zed"]])
        self.assertEqual(_count("MATCH (:ExternalUser)-[:OWNS]->(:Service {id: 'fake:default:company:1005'}) RETURN count(*)"), 0)

    # --- cursors, retries, failures ----------------------------------------------------------

    def test_cursor_resumes_after_a_restart(self):
        del FAKE.projects["SEC"]  # one container, so the next fetch is OPS again
        first = sync.sync_step("fake")
        self.assertEqual(first["status"], "synced")
        cursor = graph.get_state(first["container"])["cursor"]
        self.assertEqual(cursor, "2")
        sync._containers.clear()  # what a restart forgets
        sync.sync_step("fake")
        self.assertIn(("OPS", "2"), FAKE.calls)
        self.assertEqual(FAKE.calls.count(("OPS", None)), 1)

    def test_retry_after_waits_without_moving_the_cursor(self):
        del FAKE.projects["SEC"]  # one container, so its wait is the install's
        sync.sync_step("fake")
        FAKE.fail_next = RetryLater(30)
        report = sync.sync_step("fake")
        self.assertEqual(report["status"], "retry_later")
        self.assertEqual(graph.get_state("fake:default:container:OPS")["cursor"], "2")
        self.assertTrue(29 <= report["next_in"] <= 31, report)
        self.assertIn("retry in 30s", graph.get_state("fake:default:container:OPS")["last_error"])
        # Nothing is due before then; a sync now makes it due and fetches the same page again.
        self.assertEqual(sync.sync_step("fake")["status"], "idle")
        sync.sync_now("fake")
        sync.sync_step("fake")
        self.assertEqual(FAKE.calls[-1], ("OPS", "2"))

    def test_a_page_retry_after_spaces_the_next_fetch(self):
        FAKE.retry_after = 45
        report = sync.sync_step("fake")
        self.assertEqual(report["status"], "synced")
        state = graph.get_state(report["container"])
        self.assertTrue(state["has_more"])
        self.assertGreaterEqual(state["due_at"] - state["last_fetch_at"], 45_000)

    def test_rejected_credentials_back_off_and_show_without_secrets(self):
        FAKE.reject = True
        report = sync.sync_step("fake")
        self.assertEqual(report, {"status": "credentials_rejected", "next_in": sync.CREDENTIALS_BACKOFF_SECONDS})
        status = json.dumps(sync.status())
        self.assertIn("401", status)
        self.assertNotIn(TOKEN, status)

    def test_a_failing_fetch_backs_off_that_container_only(self):
        FAKE.fail_next = ConnectionError("boom")
        report = sync.sync_step("fake")
        self.assertEqual(report["status"], "failed")
        failed = report["container"]
        other = sync.sync_step("fake")
        self.assertEqual(other["status"], "synced")
        self.assertNotEqual(other["container"], failed)

    def test_a_page_the_writer_refuses_fails_like_a_fetch(self):
        stranger = Item(label="Ticket", id="TKT-101", external_id="x", container="fake:default:container:OPS", name="x")
        with mock.patch.object(FAKE, "fetch", return_value=Page(items=[stranger])):
            report = sync.sync_step("fake")
        self.assertEqual(report["status"], "failed")
        self.assertIn("not in install", graph.get_state(report["container"])["last_error"])
        self.assertEqual(sync.sync_step("fake")["status"], "synced")  # the other container goes on
        self.assertEqual(_rows("MATCH (t:Ticket {id: 'TKT-101'}) RETURN t.scope, t.source"), [[None, None]])

    def test_the_job_always_re_arms_while_configured(self):
        with mock.patch.object(sync, "sync_step", side_effect=RuntimeError("bug")):
            sync.run_sync({"source": "fake"})
        scheduler.schedule.assert_called_once()
        self.assertEqual(scheduler.schedule.call_args.kwargs["job_id"], "connector:sync:fake")
        scheduler.schedule.reset_mock()
        registry.unconfigure("fake")
        sync.run_sync({"source": "fake"})
        scheduler.schedule.assert_not_called()

    # --- idempotence, deletion, caps -----------------------------------------------------------

    def test_resync_is_idempotent(self):
        _drain()
        before = (_count("MATCH (n) RETURN count(n)"), _count("MATCH ()-[r]->() RETURN count(r)"))
        sync.sync_now("fake", full=True)
        _drain()
        self.assertEqual(FAKE.calls.count(("OPS", None)), 2)
        self.assertEqual((_count("MATCH (n) RETURN count(n)"), _count("MATCH ()-[r]->() RETURN count(r)")), before)

    def test_an_items_edges_are_replaced_on_resync(self):
        _drain()
        FAKE.projects["OPS"][0]["assignee"] = ZED
        sync.sync_now("fake", full=True)
        _drain()
        assigned = _rows("MATCH (u)-[:ASSIGNED]->(:Ticket {id: 'fake:default:1001'}) RETURN u.id")
        self.assertEqual(assigned, [["fake:default:user:u-zed"]])
        self.assertEqual(_rows("MATCH (:Ticket {id: 'fake:default:1001'})-[:ABOUT]->(s) RETURN s.id"), [["billing-service"]])

    def test_deleted_ids_and_dropped_containers_are_removed(self):
        _drain()
        report = graph.write_page(INSTALL, Page(deleted_ids=["fake:default:1002"]))
        self.assertEqual(report["deleted"], 1)
        self.assertEqual(_count("MATCH (t:Ticket {id: 'fake:default:1002'}) RETURN count(t)"), 0)
        self.assertEqual(_count("MATCH (:Ticket {id: 'fake:default:1001'})-[:BLOCKS]->() RETURN count(*)"), 0)
        del FAKE.projects["SEC"]
        sync.sync_now("fake")
        sync.sync_step("fake")
        self.assertEqual(_count("MATCH (n {id: 'fake:default:2001'}) RETURN count(n)"), 0)
        self.assertEqual(_count("MATCH (c:Container {id: 'fake:default:container:SEC'}) RETURN count(c)"), 0)

    def test_a_capped_backfill_ends_and_the_containers_take_turns(self):
        FAKE.projects["SEC"] += [
            {"id": "2002", "key": "SEC-2", "title": "Audit logins"},
            {"id": "2003", "key": "SEC-3", "title": "Pin TLS"},
        ]
        _drain()
        # Two pages each way, alternating: a long backfill never holds up another container.
        self.assertEqual(FAKE.calls[:4], [("OPS", None), ("SEC", None), ("OPS", "2"), ("SEC", "2")])
        FAKE.calls.clear()
        sync.sync_now("fake", full=True)
        with mock.patch.object(sync, "MAX_ITEMS_PER_CONTAINER", 2):
            _drain()
        # At the cap the backfill stops: one page each, the cursor kept for the next poll.
        self.assertEqual(sorted(FAKE.calls), [("OPS", None), ("SEC", None)])
        state = graph.get_state("fake:default:container:OPS")
        self.assertEqual((state["has_more"], state["capped"], state["cursor"]), (False, True, "2"))
        self.assertGreaterEqual(state["due_at"] - state["last_fetch_at"], FAKE.poll_seconds * 1000)

    def test_at_the_cap_polling_resumes_from_the_poll_cursor(self):
        FAKE.poll_from = "poll:2026-10-05T10:00:00Z"
        with mock.patch.object(sync, "MAX_ITEMS_PER_CONTAINER", 2):
            _drain()
            state = graph.get_state("fake:default:container:OPS")
            self.assertEqual((state["cursor"], state["has_more"], state["backfilled"]), ("poll:2026-10-05T10:00:00Z", False, True))
            self.assertNotIn("poll_cursor", state)
            FAKE.calls.clear()
            sync.sync_now("fake")
            _drain()
        # The next poll asks for what changed since the backfill's newest, not for older pages.
        self.assertIn(("OPS", "poll:2026-10-05T10:00:00Z"), FAKE.calls)

    def test_a_container_parked_too_far_out_is_due_now(self):
        _drain()
        now = int(time.time() * 1000)
        graph.set_state("fake:default:container:OPS", INSTALL, due_at=now + 60_000)
        graph.set_state("fake:default:container:SEC", INSTALL, due_at=now + 10**12)
        report = sync.sync_step("fake")
        self.assertEqual((report["status"], report["container"]), ("synced", "fake:default:container:SEC"))

    def test_huge_waits_are_clamped_and_a_parked_job_is_re_armed(self):
        FAKE.retry_after = 999_999_999
        report = sync.sync_step("fake")
        state = graph.get_state(report["container"])
        self.assertLessEqual(state["due_at"] - state["last_fetch_at"], sync.MAX_RETRY_AFTER_SECONDS * 1000)
        with mock.patch.object(FAKE, "check_credentials", side_effect=RetryLater(999_999_999)):
            sync._containers.clear()
            scheduler.schedule.reset_mock()
            sync.run_sync({"source": "fake"})
        self.assertLessEqual(scheduler.schedule.call_args.args[0], sync.max_delay("fake"))
        self.assertEqual(sync.max_delay("fake"), 3600.0)
        for due, rearmed in ((None, True), (10.0, False), (999_999_999.0, True)):
            scheduler.schedule.reset_mock()
            with mock.patch.object(scheduler, "due_at", return_value=None if due is None else time.time() + due):
                self.assertEqual(sync.schedule_all(), ["fake"] if rearmed else [], due)
            self.assertEqual(scheduler.schedule.called, rearmed)

    def test_a_hidden_shortcut_never_hides_a_readable_route(self):
        public = ["org_shared"]
        before = algorithms.path_between("Kofi Mensah", "Lena Fischer", public).answer
        tickets = algorithms.path_between("TKT-101", "TKT-102", public).answer
        self.assertIn("reaches Lena Fischer in 4 hops", before)
        _drain()
        kofi = ExternalUser(id=INSTALL.user_id("k"), source="fake", external_id="k", name="kofi", email="kofi@quillfern.example.com")
        item = Item(label="Ticket", id=INSTALL.item_id("77"), external_id="77", container="fake:default:container:OPS", name="OPS-77",
                    title="Notify retries", about=("Notification Service",))
        blocker = Item(label="Ticket", id=INSTALL.item_id("78"), external_id="78", container="fake:default:container:OPS", name="OPS-78")
        graph.write_page(INSTALL, Page(items=[item, blocker], users=[kofi], edges=[Edge("WORKED_ON", kofi.id, item.id)]))
        _rows("MATCH (b:Ticket {id: 'fake:default:78'}), (one:Ticket {id: 'TKT-101'}), (two:Ticket {id: 'TKT-102'}) CREATE (one)-[:BLOCKS]->(b)-[:BLOCKS]->(two)")
        # Shorter routes now run through Jira tickets; the public reader still gets the route it may read.
        self.assertEqual(algorithms.path_between("Kofi Mensah", "Lena Fischer", public).answer, before)
        self.assertEqual(algorithms.path_between("TKT-101", "TKT-102", public).answer, tickets)
        inside = ["org_shared", "org_internal"]
        self.assertIn("Kofi Mensah reaches Lena Fischer in 3 hops: Kofi Mensah → OPS-77", algorithms.path_between("Kofi Mensah", "Lena Fischer", inside).answer)
        self.assertIn("TKT-101 -[BLOCKS]-> OPS-78", [step.cite() for step in algorithms.path_between("TKT-101", "TKT-102", inside).path])
        # The search over readable nodes is bounded, looks nodes up by label, and says how far it got.
        queries = []
        real = type(falkordb_client.connect()[1]).ro_query

        def counting(graph_self, query, params=None, *args, **kwargs):
            queries.append(query)
            return real(graph_self, query, params, *args, **kwargs)

        with mock.patch.object(type(falkordb_client.connect()[1]), "ro_query", counting), mock.patch.object(algorithms, "MAX_SEARCHED", 3):
            capped = algorithms.path_between("Kofi Mensah", "Lena Fischer", public)
        self.assertEqual(capped.answer, "No path between Kofi Mensah and Lena Fischer within 1 hop in what you can read.")
        self.assertFalse([query for query in queries if "MATCH (n)-[r]-(m)" in query or "MATCH (a {id" in query])

    def test_an_unconnected_pair_costs_no_search(self):
        _rows("CREATE (:Service {id: 'island', name: 'Island Service'})")
        queries = []
        real = type(falkordb_client.connect()[1]).ro_query

        def counting(graph_self, query, params=None, *args, **kwargs):
            queries.append(query)
            return real(graph_self, query, params, *args, **kwargs)

        with mock.patch.object(type(falkordb_client.connect()[1]), "ro_query", counting):
            result = algorithms.path_between("Kofi Mensah", "Island Service", ["org_shared"])
        self.assertEqual(result.answer, "No path between Kofi Mensah and Island Service within 6 hops in what you can read.")
        self.assertFalse([query for query in queries if "-[r]-(m)" in query])

    def test_a_container_keeps_at_most_the_cap(self):
        with mock.patch.object(sync, "MAX_ITEMS_PER_CONTAINER", 3):
            _drain()
            self.assertEqual(_count("MATCH (n)-[:IN_CONTAINER]->(:Container {id: 'fake:default:container:OPS'}) RETURN count(n)"), 3)
            self.assertTrue(graph.get_state("fake:default:container:OPS")["capped"])
            # Items already in still update.
            FAKE.projects["OPS"][0]["title"] = "Billing exports time out again"
            sync.sync_now("fake", full=True)
            _drain()
        self.assertEqual(_rows("MATCH (t:Ticket {id: 'fake:default:1001'}) RETURN t.title"), [["Billing exports time out again"]])

    def test_ids_outside_the_install_are_refused(self):
        _drain()
        stranger = Item(label="Ticket", id="billing-service", external_id="x", container="fake:default:container:OPS", name="x")
        with self.assertRaises(ValueError):
            graph.write_page(INSTALL, Page(items=[stranger]))
        with self.assertRaises(ValueError):
            graph.write_page(INSTALL, Page(edges=[Edge("OWNS", "fake:default:user:u-zed", "billing-service")]))
        with self.assertRaises(ValueError):
            graph.write_page(INSTALL, Page(deleted_ids=["TKT-101"]))
        self.assertEqual(_count("MATCH (t:Ticket {id: 'TKT-101'}) RETURN count(t)"), 1)
        self.assertEqual(_rows("MATCH (s:Service {id: 'billing-service'}) RETURN s.name, s.scope"), [["Billing Service", None]])

    def test_an_item_outside_the_allow_list_is_dropped(self):
        _drain()
        loose = Item(label="Ticket", id="fake:default:9999", external_id="9999", container="fake:default:container:NOPE", name="NOPE-1")
        self.assertEqual(graph.write_page(INSTALL, Page(items=[loose]))["skipped"], 1)
        self.assertEqual(_count("MATCH (t:Ticket {id: 'fake:default:9999'}) RETURN count(t)"), 0)

    # --- who may read it -----------------------------------------------------------------------

    def test_slack_reads_connector_data_and_the_public_surfaces_do_not(self):
        public_stats = graph_overview.graph_stats().labels.get("Ticket")
        _drain()
        slack_tags = resolve_brain_read_container_tags(SlackMemoryScope(kind="shared", channel_id="CPUB"))
        who = catalogue.get_tool("who_knows")
        inside = who.run({"name": "OPS-1"}, slack_tags)
        self.assertTrue(inside.found)
        self.assertIn("Arjun Mehta -[ASSIGNED]-> OPS-1", [step.cite() for step in inside.path])
        self.assertIn("Zed Contractor (Fake) -[OPENED]-> OPS-1", [step.cite() for step in inside.path])
        self.assertTrue(who.run({"name": "who owns the Acme Corp account"}, slack_tags).found)

        self.assertFalse(ask_api.answer_question("who knows OPS-1").found)
        self.assertNotIn("OPS-1", ask_api.answer_question("who knows billing").answer)
        self.assertFalse(mcp_tools.call_tool("who_knows", {"name": "OPS-1"})["found"])
        self.assertFalse(mcp_tools.call_tool("who_owns", {"name": "Acme Corp"})["found"])
        page = team_page.team_page("payments")
        self.assertFalse([ticket for ticket in page.tickets if ticket.id.startswith("fake:")])
        self.assertEqual(graph_overview.graph_stats().labels.get("Ticket"), public_stats)

    def test_restricted_container_is_read_only_in_a_readers_dm(self):
        _rows("MATCH (p:Person {id: 'arjun'}) CREATE (:SlackUser {id: 'UARJUN'})-[:IS]->(p)")
        _rows("MATCH (p:Person {id: 'lena'}) CREATE (:SlackUser {id: 'ULENA'})-[:IS]->(p)")
        _drain()
        scope = _rows("MATCH (t:Ticket {id: 'fake:default:2001'}) RETURN t.scope")[0][0]
        self.assertEqual(scope, graph.restricted_scope("fake:default:container:SEC"))
        reader_dm = resolve_brain_read_container_tags(SlackMemoryScope(kind="dm", user_id="UARJUN", slack_user_id="UARJUN"))
        other_dm = resolve_brain_read_container_tags(SlackMemoryScope(kind="dm", user_id="ULENA", slack_user_id="ULENA"))
        channel = resolve_brain_read_container_tags(SlackMemoryScope(kind="shared", channel_id="CPUB"))
        self.assertIn(scope, reader_dm)
        self.assertNotIn(scope, other_dm)
        self.assertNotIn(scope, channel)
        self.assertTrue(catalogue.get_tool("who_knows").run({"name": "SEC-1"}, reader_dm).found)
        self.assertFalse(catalogue.get_tool("who_knows").run({"name": "SEC-1"}, channel).found)
        # Readers unknown: nobody keeps access.
        FAKE.readers_of = {}
        sync._containers.clear()
        sync.sync_step("fake")
        self.assertNotIn(scope, resolve_brain_read_container_tags(SlackMemoryScope(kind="dm", user_id="UARJUN", slack_user_id="UARJUN")))

    # --- pull requests ---------------------------------------------------------------------------

    def test_pull_request_references_link_now_or_when_the_ticket_arrives(self):
        _rows("CREATE (:PullRequest {id: 'github:1:pr:7', name: 'app#7', scope: 'org_shared'})")
        self.assertEqual(graph.link_references("github:1:pr:7", ["ops-1"], "fake"), 0)
        _drain()
        self.assertEqual(_rows("MATCH (:PullRequest {id: 'github:1:pr:7'})-[:REFERENCES]->(t) RETURN t.name"), [["OPS-1"]])
        self.assertEqual(graph.link_references("github:1:pr:7", ["OPS-2"], "fake", rel="CLOSES"), 1)
        self.assertEqual(_rows("MATCH (:PullRequest {id: 'github:1:pr:7'})-[:CLOSES]->(t) RETURN t.name"), [["OPS-2"]])
        graph.link_references("github:1:pr:7", [], "fake")
        self.assertEqual(_count("MATCH (:PullRequest {id: 'github:1:pr:7'})-[:REFERENCES]->() RETURN count(*)"), 0)
        self.assertEqual(_count("MATCH (:PullRequest {id: 'github:1:pr:7'})-[:CLOSES]->() RETURN count(*)"), 1)
        with self.assertRaises(ValueError):
            graph.link_references("github:1:pr:7", ["OPS-1"], "fake", rel="OWNS")

    # --- routes ----------------------------------------------------------------------------------

    def _client(self) -> TestClient:
        app = FastAPI()
        app.include_router(connectors_route.router)
        return TestClient(app)

    def _signed(self, payload: dict) -> tuple[bytes, dict]:
        body = json.dumps(payload).encode()
        return body, {"x-fake-signature": "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()}

    def test_webhook_verifies_dedupes_and_writes(self):
        _drain()
        client = self._client()
        issue = {"id": "1006", "key": "OPS-6", "title": "Webhook issue", "components": ["search"], "assignee": ARJUN, "nonce": uuid.uuid4().hex}
        body, headers = self._signed({"project": "OPS", "issues": [issue], "deleted": ["1003"]})
        self.assertEqual(client.post("/connectors/fake/webhook", content=body, headers={"x-fake-signature": "sha256=00"}).status_code, 401)
        self.assertEqual(client.post("/connectors/nope/webhook", content=body, headers=headers).status_code, 404)
        response = client.post("/connectors/fake/webhook", content=body, headers=headers)
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(_rows("MATCH (t:Ticket {id: 'fake:default:1006'})-[:ABOUT]->(s) RETURN t.scope, s.id"), [["org_internal", "search-service"]])
        self.assertEqual(_count("MATCH (t:Ticket {id: 'fake:default:1003'}) RETURN count(t)"), 0)
        self.assertEqual(client.post("/connectors/fake/webhook", content=body, headers=headers).json(), {"ok": True, "duplicate": True})
        # A delivery never moves a cursor.
        self.assertEqual(graph.get_state("fake:default:container:OPS")["cursor"], "5")

    def test_admin_routes_need_the_token_and_show_no_secrets(self):
        _drain()
        client = self._client()
        armed = dataclasses.replace(settings, brain_admin_token=ADMIN)
        with mock.patch.object(slack_install, "settings", armed):
            self.assertEqual(client.get("/ops/connectors").status_code, 403)
            self.assertEqual(client.post("/ops/connectors/fake/sync").status_code, 403)
            response = client.get("/ops/connectors", headers={"Authorization": f"Bearer {ADMIN}"})
            self.assertEqual(client.post("/ops/connectors/nope/sync", headers={"Authorization": f"Bearer {ADMIN}"}).status_code, 404)
            synced = client.post("/ops/connectors/fake/sync?full=true", headers={"Authorization": f"Bearer {ADMIN}"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(TOKEN, response.text)
        self.assertNotIn(WEBHOOK_SECRET, response.text)
        row = response.json()["connectors"][0]
        self.assertEqual((row["source"], row["account"]), ("fake", "bot@fake.example"))
        self.assertEqual({container["id"]: container["items"] for container in row["containers"]}, {"fake:default:container:OPS": 5, "fake:default:container:SEC": 1})
        self.assertEqual(synced.json(), {"ok": True, "source": "fake", "full": True})
        self.assertIsNone(graph.get_state("fake:default:container:OPS").get("cursor"))
        scheduler.schedule.assert_called()


if __name__ == "__main__":
    unittest.main()
