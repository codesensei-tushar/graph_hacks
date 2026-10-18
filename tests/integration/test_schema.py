"""``schema.ensure`` on a real FalkorDB: a fresh graph and an old one end at the same version
with the same indexes, and running it again changes nothing."""

import unittest

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client, schema  # noqa: E402
from src.domain.knowledge.search import INDEXED  # noqa: E402

FRESH = f"company_schema_fresh_test{tests.TEST_GRAPH_SUFFIX}"
OLD = f"company_schema_old_test{tests.TEST_GRAPH_SUFFIX}"


def _graph(name):
    """An empty throwaway graph behind the proxy (untracked: it leaves no generation key)."""
    graph = falkordb_client.scratch_graph(name)
    try:
        graph.delete()
    except Exception:
        pass
    return graph


def _expected() -> dict[str, dict[str, set[str]]]:
    out: dict[str, dict[str, set[str]]] = {}
    for label, prop in schema.INDEXES:
        out.setdefault(label, {}).setdefault(prop, set()).add("RANGE")
    for label, prop in (*schema.FULLTEXT, *((label, field) for label, fields in INDEXED.items() for field in fields)):
        out.setdefault(label, {}).setdefault(prop, set()).add("FULLTEXT")
    return out


class SchemaTest(unittest.TestCase):
    def tearDown(self):
        for name in (FRESH, OLD):
            try:
                falkordb_client.client().select_graph(name).delete()
            except Exception:
                pass

    def test_the_indexes_every_anchor_needs_are_listed(self):
        for wanted in (("Person", "id"), ("Team", "id"), ("Decision", "id"), ("PullRequest", "id"), ("GitHubUser", "id"), ("Repo", "id"), ("Chat", "id"), ("Company", "id"), ("ExternalUser", "email"), ("Container", "install"), ("Service", "id"), ("Ticket", "id")):
            self.assertIn(wanted, schema.INDEXES)
        self.assertEqual(len(set(schema.INDEXES)), len(schema.INDEXES))

    def test_a_fresh_graph_and_an_old_graph_end_at_the_latest_version_with_the_same_indexes(self):
        fresh = _graph(FRESH)
        fresh.query("CREATE (:Company {id: 'acme', name: 'Acme'})")  # a graph exists only once written
        self.assertEqual(schema.ensure(fresh), schema.LATEST)

        old = _graph(OLD)
        # As a graph made before schema.py: some of the indexes, nodes with no scope, no SchemaVersion.
        old.query("CREATE INDEX FOR (n:Service) ON (n.id)")
        old.query(
            """
            CREATE (:Service {id: 'billing', name: 'Billing'}), (:Person {id: 'ada', name: 'Ada'}),
                   (:Ticket {id: 't1', name: 'T-1', scope: 'slack_channel_C1'}), (:Memory {id: 'm1', text: 'kept as is'}),
                   (:Ticket {id: 'j1', name: 'PAY-1', scope: 'org_internal', source_updated_at: '2026-09-01T10:00:00.000+0000'}),
                   (:Ticket {id: 'j2', name: 'PAY-2', scope: 'org_internal', source_updated_at: 'not a time'}),
                   (:Ticket {name: 'no id', scope: 'org_internal', source_updated_at: '2026-09-01T10:00:00Z'})
            """
        )
        self.assertEqual(schema.version(old), 0)
        self.assertEqual(schema.ensure(old), schema.LATEST)

        self.assertEqual(schema.version(fresh), schema.version(old))
        self.assertEqual(schema.indexes(fresh), schema.indexes(old))
        self.assertEqual(schema.indexes(old), _expected())
        scopes = dict(old.ro_query("MATCH (n) WHERE n.id IN ['billing', 'ada', 't1', 'm1'] RETURN n.id, n.scope").result_set)
        self.assertEqual(scopes, {"billing": "org_shared", "ada": "org_shared", "t1": "slack_channel_C1", "m1": None})
        # Synced before changed_at_ms: it is filled from the source time, -1 when that cannot be read.
        changed = dict(old.ro_query("MATCH (t:Ticket) RETURN coalesce(t.id, t.name), t.changed_at_ms").result_set)
        self.assertEqual(changed, {"t1": None, "j1": 1788256800000, "j2": -1, "no id": 1788256800000})  # an id-less node too, and the loop ends

    def test_running_it_again_changes_nothing(self):
        graph = _graph(FRESH)
        graph.query("CREATE (:Company {id: 'acme', name: 'Acme'})")
        schema.ensure(graph)
        before = (schema.indexes(graph), graph.ro_query("MATCH (n) RETURN count(n)").result_set[0][0])
        self.assertEqual(schema.ensure(graph), schema.LATEST)
        self.assertEqual((schema.indexes(graph), graph.ro_query("MATCH (n) RETURN count(n)").result_set[0][0]), before)

    def test_a_new_org_starts_at_the_latest_version(self):
        from src.core import tenancy
        from src.domain import orgs

        orgs.ensure_control_indexes()
        org = orgs.create_org("Schema Probe", created_by="TS:US")
        try:
            graph = falkordb_client.scratch_graph(org.graph)
            self.assertEqual(schema.version(graph), schema.LATEST)
            self.assertEqual(schema.indexes(graph), _expected())
        finally:
            falkordb_client.scratch_graph(org.graph).delete()
            tenancy.control_graph().query("MATCH (o:Org {id: $id}) DETACH DELETE o", {"id": org.id})

    def test_background_migrations_run_and_a_failure_is_only_logged(self):
        from unittest import mock

        graph = _graph(FRESH)
        graph.query("CREATE (:Person {id: 'ada', name: 'Ada'})")
        self.assertEqual(schema.ensure(graph, migrate=False), 0)
        with mock.patch.object(falkordb_client, "graph_name", return_value=FRESH):
            schema.ensure_in_background().join(30)
        self.assertEqual(schema.version(graph), schema.LATEST)
        with mock.patch.object(schema, "ensure", side_effect=RuntimeError("boom")), self.assertLogs("src.db.schema", level="ERROR"):
            schema.ensure_in_background().join(30)

    def test_a_migration_runs_once(self):
        graph = _graph(FRESH)
        graph.query("CREATE (:Company {id: 'acme', name: 'Acme'})")
        schema.ensure(graph)
        graph.query("CREATE (:Person {id: 'late', name: 'Late'})")  # written after migration 1
        schema.ensure(graph)
        self.assertEqual(graph.ro_query("MATCH (p:Person {id: 'late'}) RETURN p.scope").result_set[0][0], None)


if __name__ == "__main__":
    unittest.main()
