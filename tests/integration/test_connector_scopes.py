"""What connected tools write (scope ``org_internal``) reaches Slack askers and nobody else.

Hand-made nodes on the Quillfern seed, shaped as ``src/connectors/graph.py`` writes them:
a hidden service on a dependency route, a Jira ticket with a linked and an unlinked
account, and a backlog of tickets about one service. Every tool that walks past them must
filter each node it shows, and who_knows counts a connector's accounts like GitHub's.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import algorithms, ask_service, decisions, resolve  # noqa: E402
from src.domain.memory.read_scope import resolve_brain_read_container_tags  # noqa: E402
from src.domain.memory.writeback import SlackMemoryScope  # noqa: E402
from src.domain.slack import token_crypto  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_connector_scopes_test{tests.TEST_GRAPH_SUFFIX}"
ORG = ["org_shared"]
SLACK = ["org_shared", "org_internal"]


def _cites(result) -> list[str]:
    return [step.cite() for step in result.path]


class ConnectorScopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        _, graph = falkordb_client.connect()
        graph.query(
            """
            MATCH (arjun:Person {id: 'arjun'}), (billing:Service {id: 'billing-service'}), (postgres:Service {id: 'postgres'})
            CREATE (ledger:Service {id: 'ledger', name: 'Ledger', scope: 'org_internal'})-[:DEPENDS_ON]->(postgres),
                   (:Service {id: 'reports', name: 'Reports Service'})-[:DEPENDS_ON]->(ledger),
                   (pay:Ticket {id: 'jira:default:10012', name: 'PAY-12', title: 'Billing exports time out', external_id: '10012',
                                source: 'jira', state: 'open', scope: 'org_internal'})-[:ABOUT]->(billing),
                   (linked:ExternalUser {id: 'jira:default:user:a1', name: 'arjun.m', scope: 'org_internal'})-[:IS]->(arjun),
                   (contractor:ExternalUser {id: 'jira:default:user:z9', name: 'Zed Contractor', scope: 'org_internal'}),
                   (linked)-[:ASSIGNED]->(pay), (contractor)-[:OPENED]->(pay),
                   (:Ticket {id: 'TKT-900', name: 'TKT-900', title: 'Ledger drift'})-[:ABOUT]->(ledger),
                   (:Ticket {id: 'github:1:issue:5000', name: 'app#5000', title: 'An issue', source: 'github'}),
                   (:Decision {id: 'decision-ledger', name: 'Close the books nightly', scope: 'org_shared'})-[:ABOUT]->(ledger)
            """
        )
        # A backlog about the invoice worker: ten open tickets and a closed one, all from Jira.
        for number in range(1, 12):
            graph.query(
                """
                MATCH (worker:Service {id: 'invoice-worker'})
                CREATE (:Ticket {id: $id, name: $name, title: 'Invoice backlog item', external_id: $ext, source: 'jira',
                                 state: $state, source_updated_at: $updated, scope: 'org_internal'})-[:ABOUT]->(worker)
                """,
                {
                    "id": f"jira:default:2{number:04d}", "name": f"INV-{number}", "ext": f"2{number:04d}",
                    "state": "closed" if number == 11 else "open", "updated": f"2026-10-{number:02d}T00:00:00Z",
                },
            )

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in reversed(cls._patches):
                patch.stop()

    # --- the read set ----------------------------------------------------------------------

    def test_every_slack_conversation_reads_org_internal(self):
        for scope in (
            SlackMemoryScope(kind="shared", channel_id="CPUB"),
            SlackMemoryScope(kind="private_channel", channel_id="CPRIV"),
            SlackMemoryScope(kind="dm", user_id="U1", slack_user_id="U1"),
        ):
            tags = resolve_brain_read_container_tags(scope)
            self.assertEqual(tags[:2], ["org_shared", "org_internal"], scope.kind)
        # An explicit surface is taken as given.
        self.assertEqual(resolve_brain_read_container_tags(None, ["org_shared"]), ["org_shared"])

    # --- blast radius and load bearing -------------------------------------------------------

    def test_blast_radius_never_walks_through_what_the_asker_cannot_read(self):
        public = ask_service.blast_radius("what breaks if postgres goes down", ORG)
        self.assertNotIn("Ledger", public.answer)
        self.assertNotIn("Reports Service", public.answer)
        self.assertFalse([cite for cite in _cites(public) if "Ledger" in cite or "Reports" in cite])

        inside = ask_service.blast_radius("what breaks if postgres goes down", SLACK)
        self.assertIn("Ledger", inside.answer)
        self.assertIn("Reports Service -[DEPENDS_ON]-> Ledger", _cites(inside))

    def test_dependencies_walk_is_scoped_too(self):
        result = algorithms.blast_radius("what does the reports service depend on", ORG)
        self.assertNotIn("Ledger", result.answer)
        self.assertNotIn("Postgres", result.answer)

    def test_load_bearing_ranks_and_counts_only_readable_services(self):
        public = algorithms.load_bearing(10, ORG)
        self.assertNotIn("Ledger", public.answer)
        self.assertFalse([cite for cite in _cites(public) if "Ledger" in cite or "Reports" in cite])
        inside = algorithms.load_bearing(10, SLACK)
        self.assertIn("Ledger", inside.answer)
        # Without scopes, the company-wide scope only.
        self.assertEqual(algorithms.load_bearing(10).answer, public.answer)

    def test_load_bearing_through_the_catalogue_gets_the_askers_scopes(self):
        from src.domain.knowledge import catalogue

        spec = catalogue.get_tool("load_bearing")
        self.assertNotIn("Ledger", spec.run({"limit": 10}, ORG).answer)
        self.assertIn("Ledger", spec.run({"limit": 10}, SLACK).answer)

    # --- what a node is about ----------------------------------------------------------------

    def test_a_public_node_about_a_hidden_service_does_not_name_it(self):
        for result in (ask_service.who_owns("who owns TKT-900", ORG), ask_service.who_knows("who knows TKT-900", ORG)):
            self.assertTrue(result.found)
            self.assertNotIn("Ledger", result.answer)
            self.assertFalse([cite for cite in _cites(result) if "Ledger" in cite])
        trace = decisions.decision_trace("why did we close the books nightly", ORG)
        self.assertNotIn("Ledger", trace.answer)
        self.assertFalse([cite for cite in _cites(trace) if "Ledger" in cite])

    # --- who_knows ---------------------------------------------------------------------------

    def test_connector_ticket_is_invisible_to_the_public_scope(self):
        self.assertFalse(ask_service.who_knows("who knows PAY-12", ORG).found)
        result = ask_service.who_knows("who knows billing", ORG)
        self.assertNotIn("PAY-12", result.answer)
        self.assertFalse([cite for cite in _cites(result) if "PAY-12" in cite])

    def test_connector_accounts_count_like_github_accounts(self):
        result = ask_service.who_knows("who knows PAY-12", SLACK)
        self.assertTrue(result.found)
        cites = _cites(result)
        self.assertIn("Arjun Mehta -[ASSIGNED]-> PAY-12", cites)
        self.assertIn("Zed Contractor -[OPENED]-> PAY-12", cites)
        self.assertIn("Arjun Mehta (is assigned it", result.answer.replace("is assigned PAY-12", "is assigned it"))
        self.assertIn("Zed Contractor (opened", result.answer)

    def test_jira_assignees_of_tickets_about_a_service_know_it(self):
        result = ask_service.who_knows("who knows billing", SLACK)
        cites = _cites(result)
        self.assertIn("PAY-12 -[ABOUT]-> Billing Service", cites)
        self.assertIn("Arjun Mehta -[ASSIGNED]-> PAY-12", cites)
        self.assertIn("Open tickets: PAY-12 and TKT-102.", result.answer)

    def test_ticket_listing_is_capped_and_prefers_open_tickets(self):
        result = ask_service.who_knows("who knows the invoice worker", SLACK)
        about = [cite for cite in _cites(result) if cite.endswith("-[ABOUT]-> Invoice Worker")]
        self.assertEqual(len(about), ask_service.MAX_TICKETS)
        self.assertNotIn("INV-11 -[ABOUT]-> Invoice Worker", about)  # the closed one
        # The most recently updated open tickets are the ones listed, and the rest are counted.
        self.assertNotIn("INV-1 -[ABOUT]-> Invoice Worker", about)
        self.assertIn("INV-10 -[ABOUT]-> Invoice Worker", about)
        self.assertIn("and 2 more.", result.answer)

    def test_github_ticket_people_are_unchanged(self):
        _, graph = falkordb_client.connect()
        graph.query(
            """
            MATCH (lena:Person {id: 'lena'}), (t:Ticket {id: 'github:1:issue:5000'})
            MERGE (gl:GitHubUser {id: 'gh-lena-scopes', login: 'lena-f', name: 'lena-f'})-[:IS]->(lena)
            MERGE (gx:GitHubUser {id: 'gh-ext-scopes', login: 'outsider', name: 'outsider'})
            MERGE (gl)-[:ASSIGNED]->(t) MERGE (gx)-[:OPENED]->(t)
            """
        )
        result = ask_service.who_knows("who knows app#5000", ORG)
        self.assertIn("Lena Fischer -[ASSIGNED]-> app#5000", _cites(result))
        self.assertIn("@outsider -[OPENED]-> app#5000", _cites(result))

    # --- name resolution ---------------------------------------------------------------------

    def test_connector_titles_do_not_resolve_names(self):
        resolution = resolve.resolve_name("who knows billing exports time out", ask_service.ANSWER_LABELS, SLACK)
        self.assertNotEqual(resolution.entity.id if resolution.entity else None, "jira:default:10012")
        self.assertNotIn("PAY-12", resolution.candidate_names())
        # A handle still does, and a seeded ticket's title still does.
        self.assertEqual(resolve.resolve_name("PAY-12", ask_service.ANSWER_LABELS, SLACK).entity.id, "jira:default:10012")
        self.assertEqual(resolve.resolve_name("annual plan renewals charge twice on retry", ask_service.ANSWER_LABELS, ORG).entity.id, "TKT-102")

    # --- writes from Slack -------------------------------------------------------------------

    def test_next_ticket_id_counts_only_tkt_ids(self):
        _, graph = falkordb_client.connect()
        self.assertEqual(ask_service._next_ticket_id(graph), "TKT-901")
        graph.query("CREATE (:Ticket {id: 'TKT-2a', name: 'odd'})")
        self.assertEqual(ask_service._next_ticket_id(graph), "TKT-901")


class TokenCryptoTest(unittest.TestCase):
    def test_connector_credentials_never_fall_back_to_plaintext(self):
        with mock.patch.object(token_crypto, "_fernet", return_value=None):
            self.assertTrue(token_crypto.encrypt_token("t").startswith("plain:"))
            with self.assertRaises(RuntimeError):
                token_crypto.encrypt_token("t", allow_plaintext=False)
            with self.assertRaises(ValueError):
                token_crypto.decrypt_token("plain:t", allow_plaintext=False)


if __name__ == "__main__":
    unittest.main()
