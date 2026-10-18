"""The Company Brain self-seed merged next to Quillfern in one graph, as production's live demo runs it.

``POST /ops/seed?company=company_brain`` merges this repo's own teams, people, services and
decisions into the legacy graph that serves Quillfern. It must not change a single Quillfern
node or edge, must be idempotent, and its service paths must be the repo's real layout.
Needs FalkorDB on localhost:6379 (``docker compose up -d``).
"""

import dataclasses
import unittest
from pathlib import Path
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api import ops  # noqa: E402
from src.api.v1 import slack_install  # noqa: E402
from src.core import tenancy  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.db import falkordb_client  # noqa: E402
from src.domain.github import ingest  # noqa: E402
from src.seed import company_brain, quillfern, seed  # noqa: E402

GRAPH = f"company_demo_seed_test{tests.TEST_GRAPH_SUFFIX}"
TOKEN = "demo-seed-admin-token"
ROOT = Path(__file__).resolve().parents[2]
LOGINS = {"codesensei-tushar", "noobsurd11", "omsherikar"}


def _ids(module) -> set[str]:
    rows = [module.COMPANY, *module.PEOPLE, *module.TEAMS, *module.SERVICES, *module.DOCUMENTS, *module.TICKETS, *module.DECISIONS, *module.CHATS]
    rows += list(getattr(module, "MEMORIES", ()))
    return {row["id"] for row in rows}


def _words(module) -> set[str]:
    """Every name and alias a resolver could match, lower-cased."""
    found = set()
    for row in [*module.SERVICES, *module.TEAMS, *module.PEOPLE]:
        found.add(row["name"].lower())
        found.update(alias.strip().lower() for alias in (row.get("aliases") or "").split(",") if alias.strip())
    return found


def _snapshot(graph) -> list:
    """Every Quillfern-seeded node with its properties, and every edge between them."""
    ids = sorted(_ids(quillfern))
    nodes = graph.query("MATCH (n) WHERE n.id IN $ids RETURN n.id, labels(n), properties(n) ORDER BY n.id", {"ids": ids}).result_set
    edges = graph.query(
        "MATCH (a)-[r]->(b) WHERE a.id IN $ids AND b.id IN $ids RETURN a.id, type(r), b.id, properties(r) ORDER BY a.id, type(r), b.id",
        {"ids": ids},
    ).result_set
    return [nodes, edges]


class SelfSeedShapeTest(unittest.TestCase):
    """What the seed module says, checked against Quillfern and the repo, without a graph."""

    def test_no_id_is_one_quillfern_uses(self):
        self.assertEqual(_ids(company_brain) & _ids(quillfern), set())

    def test_no_name_or_alias_takes_a_quillfern_name(self):
        quillfern_words = _words(quillfern) | {"hubspot", "crm", "sales", "renewals"}  # the imported Sales team
        self.assertEqual(_words(company_brain) & quillfern_words, set())

    def test_every_path_is_in_the_repo(self):
        for service in company_brain.SERVICES:
            for path in service.get("paths") or []:
                with self.subTest(service=service["id"], path=path):
                    self.assertTrue((ROOT / path).exists(), f"{path} is not in the repo")

    def test_people_are_the_contributors_and_every_edge_names_a_known_id(self):
        self.assertEqual({person["github"] for person in company_brain.PEOPLE}, LOGINS)
        people = {person["id"] for person in company_brain.PEOPLE}
        services = {service["id"] for service in company_brain.SERVICES}
        for child, parent in company_brain.DEPENDS_ON:
            self.assertIn(child, services)
            self.assertIn(parent, services)
        for person, rel, service in company_brain.WORK:
            self.assertIn(person, people)
            self.assertIn(service, services, rel)
        owned = {service for _person, rel, service in company_brain.WORK if rel == "OWNS"}
        self.assertEqual({service["id"] for service in company_brain.SERVICES if service.get("paths")} - owned, set(), "every repo area has an owner")

    def test_a_pull_request_maps_to_the_area_it_changes(self):
        prefixes = sorted(((prefix, service["id"]) for service in company_brain.SERVICES for prefix in service.get("paths") or []), key=lambda item: -len(item[0]))

        def area(path: str) -> str | None:
            return next((service for prefix, service in prefixes if path == prefix or path.startswith(prefix.rstrip("/") + "/")), None)

        self.assertEqual(area("src/connectors/jira/mapper.py"), "cb-jira")
        self.assertEqual(area("src/connectors/graph.py"), "cb-connectors")
        self.assertEqual(area("src/api/v1/console_people.py"), "cb-console")
        self.assertEqual(area("src/static/console/teams.html"), "cb-console")
        self.assertEqual(area("src/static/index.html"), "cb-site")
        self.assertEqual(area("src/domain/knowledge/mcp_tools.py"), "cb-mcp")
        self.assertEqual(area("src/domain/knowledge/ticket_status.py"), "knowledge-tools")
        self.assertEqual(area("src/core/scheduler.py"), "scheduler")
        self.assertEqual(area("src/domain/slack/channels/rollout.py"), "cb-slack")
        self.assertEqual(area("src/domain/slack/turn/triage.py"), "turn-engine")
        self.assertEqual(area("deploy/backup/falkordb-backup.py"), "cb-ops")
        self.assertEqual(area("deploy/docker-compose.yml"), "cb-deploy")
        self.assertEqual(area(".github/workflows/ci.yml"), "cb-deploy")
        self.assertEqual(area("src/core/tenancy.py"), "cb-tenancy")
        self.assertIsNone(area("tests/integration/test_scheduler.py"), "tests belong to no area")

    def test_decisions_cite_pull_requests_by_number_in_their_summary(self):
        for decision in company_brain.DECISIONS:
            for number in decision.get("pull_requests") or []:
                with self.subTest(decision=decision["id"], number=number):
                    self.assertRegex(decision["summary"], rf"#{number}\b")


class SelfSeedMergeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def setUp(self):
        seed.reset_and_seed(quillfern)

    def test_merging_next_to_quillfern_changes_nothing_of_quillfern_and_twice_changes_nothing(self):
        _, graph = falkordb_client.connect()
        before = _snapshot(graph)
        first = seed.seed_company(company_brain)
        self.assertGreater(first.nodes_created, 30)
        self.assertEqual(_snapshot(graph), before, "no Quillfern node or edge is touched")
        again = seed.seed_company(company_brain)
        self.assertEqual((again.nodes_created, again.edges_created, again.properties_set), (0, 0, 0))
        companies = graph.query("MATCH (c:Company) RETURN c.id ORDER BY c.id").result_set
        self.assertEqual(companies, [["company-brain"], ["quillfern"]])
        # Each company's teams are its own.
        teams = graph.query("MATCH (t:Team)-[:PART_OF]->(c:Company {id: 'company-brain'}) RETURN t.name ORDER BY t.name").result_set
        self.assertEqual(teams, [["Answers"], ["Infra"]])
        # Quillfern's seed merged after it leaves ours alone too.
        mine = graph.query("MATCH (s:Service {id: 'cb-console'}) RETURN s.name, s.paths").result_set
        seed.seed_company(quillfern)
        self.assertEqual(graph.query("MATCH (s:Service {id: 'cb-console'}) RETURN s.name, s.paths").result_set, mine)

    def test_decisions_link_the_pull_requests_they_cite_once_github_has_written_them(self):
        seed.seed_company(company_brain)
        _, graph = falkordb_client.connect()
        repo = {"id": 1, "full_name": company_brain.REPO, "private": False, "html_url": "https://github.com/codesensei-tushar/graph_hacks"}
        other = {"id": 2, "full_name": "someone/graph_hacks", "private": False}
        graph.query("MERGE (:Repo {id: '1', full_name: $a, scope: 'org_shared'}) MERGE (:Repo {id: '2', full_name: $b, scope: 'org_shared'})", {"a": repo["full_name"], "b": other["full_name"]})
        ingest.upsert_pull_request(repo, {"number": 207, "title": "I1: batched connector writes", "state": "closed", "merged_at": "2026-10-18T17:15:37Z"}, files=[{"filename": "src/connectors/graph.py"}])
        ingest.upsert_pull_request(other, {"number": 216, "title": "someone else's 216", "state": "open"}, files=[])
        report = seed.seed_company(company_brain)
        self.assertEqual(report.edges_created, 1, "only the cited pull request in this repo")
        rows = graph.query("MATCH (d:Decision)-[:BASED_ON]->(p:PullRequest) RETURN d.id, p.name").result_set
        self.assertEqual(rows, [["decision-content-hash-sync", "graph_hacks#207"]])
        touches = graph.query("MATCH (:PullRequest {number: 207})-[:TOUCHES]->(s:Service) RETURN s.id").result_set
        self.assertEqual(touches, [["cb-connectors"]], "a real path maps to the self-seed's service")
        self.assertEqual(seed.seed_company(company_brain).edges_created, 0)

    def test_ops_seed_merges_the_named_company_into_the_legacy_graph(self):
        app = FastAPI()
        ops.install(app)
        app = tenancy.LegacyOrgRoutes(app)
        client = TestClient(app)
        armed = dataclasses.replace(settings, brain_admin_token=TOKEN, seed_company="quillfern", tenancy_write_strict=True)
        auth = {"Authorization": f"Bearer {TOKEN}"}
        with (
            mock.patch.object(slack_install, "settings", armed),
            mock.patch.object(ops, "settings", armed),
            mock.patch.object(falkordb_client, "settings", armed),
        ):
            self.assertEqual(client.post("/ops/seed?company=company_brain").status_code, 403)
            refused = client.post("/ops/seed?company=acme", headers=auth)
            self.assertEqual(refused.status_code, 400)
            self.assertEqual(refused.json()["error"], "unknown_company")
            response = client.post("/ops/seed?company=company_brain", headers=auth)
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual((body["company"], body["mode"]), ("company_brain", "merge"))
            self.assertGreater(body["nodes_created"], 30)
            again = client.post("/ops/seed?company=company_brain", headers=auth).json()
            self.assertEqual((again["nodes_created"], again["edges_created"], again["properties_set"]), (0, 0, 0))
            # No name keeps SEED_COMPANY, which merges nothing new on a Quillfern graph.
            default = client.post("/ops/seed", headers=auth).json()
            self.assertEqual((default["company"], default["nodes_created"]), ("quillfern", 0))
        _, graph = falkordb_client.connect()
        self.assertEqual(graph.query("MATCH (c:Company) RETURN count(c)").result_set[0][0], 2)


if __name__ == "__main__":
    unittest.main()
