"""Public GitHub repos added by URL, end to end against a fake GitHub over HTTP, with no installation.

operator adds https://github.com/<owner>/<repo> to an org -> the stepped backfill reads every page
through the real scheduler (one page a job, rate limits waited out) -> pull requests, reviews,
issues and TOUCHES land in that org's graph only, as org_shared -> a poll picks up what changed ->
remove deletes it all in batches. Plus the console routes, the depth limit, and which credentials
the public reads use.
"""

import dataclasses
import json
import time
import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graphs before src loads

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api.v1 import github_repos, slack_install  # noqa: E402
from src.core import config, scheduler, tenancy  # noqa: E402
from src.db import falkordb_client  # noqa: E402
from src.db.falkordb_client import connect, kv  # noqa: E402
from src.domain import orgs  # noqa: E402
from src.domain.console import session as console_session  # noqa: E402
from src.domain.github import backfill, ingest, public_repos  # noqa: E402
from src.integrations.github import client  # noqa: E402
from src.seed import company_brain  # noqa: E402
from src.seed.seed import reset_and_seed  # noqa: E402
from tests.fixtures.github_server import PUBLIC_TOKEN, FakeGitHub, make_repo, user  # noqa: E402

PUBLIC = "https://brain.test"
TOKEN = "operator-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
REPO_URL = "https://github.com/codesensei-tushar/graph_hacks"
REPO_ID = "880001"


@dataclasses.dataclass
class FakeSession:
    org: str
    role: str = "owner"
    actor: str = "slack:T1:U1"


class PublicReposTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.github = FakeGitHub().start()
        cls.live = dataclasses.replace(config.settings, public_url=PUBLIC, brain_admin_token=TOKEN, console_enabled=True, github_public_repo_token="")
        cls.patches = [
            mock.patch.object(client, "settings", cls.live),
            mock.patch.object(slack_install, "settings", cls.live),
            mock.patch.object(github_repos, "settings", cls.live),
            mock.patch.object(client, "API", cls.github.base_url),
            mock.patch.object(backfill, "POLL_SECONDS", 10**7),
        ]
        for patch in cls.patches:
            patch.start()
        orgs.ensure_control_indexes()
        cls.acme = orgs.create_org("Acme Public Repos", "T9:U9")
        app = FastAPI()
        app.include_router(github_repos.ops_router)
        app.include_router(github_repos.console_router)
        cls.app = app
        cls.client = TestClient(app, base_url=PUBLIC)

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(cls.acme.graph).delete()
            tenancy.control_graph().query("MATCH (n:GitHubPublicRepo) DETACH DELETE n")
            tenancy.control_graph().query("MATCH (o:Org {id: $id}) DETACH DELETE o", {"id": cls.acme.id})
        finally:
            for patch in reversed(cls.patches):
                patch.stop()
            cls.github.stop()

    def setUp(self):
        with tenancy.org_context(tenancy.legacy_org()):
            reset_and_seed(company_brain)
        with tenancy.org_context(self.acme.ref()):
            _, graph = connect()
            graph.query("MATCH (n) WHERE n:Repo OR n:PullRequest OR n:Ticket OR n:GitHubUser DETACH DELETE n")
        tenancy.control_graph().query("MATCH (n:GitHubPublicRepo) DETACH DELETE n")
        kv().delete(scheduler.QUEUE, scheduler.PAYLOADS)
        self.github.repos.clear()
        self.github.installations.clear()
        self.github.auth_seen.clear()
        self.github.rate_limit_next = 0
        # About the size of the demo repo: every pull request needs the full backfill, not the first 50.
        self.github.add_public_repo(make_repo(int(REPO_ID), "codesensei-tushar/graph_hacks", private=False, pulls=230, issues=12))
        self.github.add_public_repo(make_repo(880002, "codesensei-tushar/secret", private=True))
        self.github.repos["codesensei-tushar/secret"]["repo"]["private"] = True

    # --- helpers ------------------------------------------------------------------------------

    def _drain(self, rounds: int = 400) -> int:
        """Run the scheduler until no backfill or removal job is left (rate-limit waits included)."""
        ran = 0
        for _ in range(rounds):
            pending = [json.loads(raw)["name"] for raw in (kv().hgetall(scheduler.PAYLOADS) or {}).values()]
            if not any(name in (backfill.REPO_STEP, backfill.REMOVE_JOB) for name in pending):
                return ran
            ran += scheduler.run_due(now=time.time() + backfill.MAX_WAIT_SECONDS + 60)
        self.fail("the backfill did not finish")

    def _count(self, org, query: str, params: dict | None = None):
        with tenancy.org_context(org):
            _, graph = connect()
            return graph.query(query, params or {}).result_set

    def _services(self, path: str) -> list[str]:
        """The seeded services whose Service.paths cover ``path`` (the seed owns which ones)."""
        with tenancy.org_context(tenancy.legacy_org()):
            return ingest.services_for_paths([path])

    def _add(self, url=REPO_URL, org=None, **body):
        params = {"org": org} if org else {}
        return self.client.post("/ops/github/public-repos", params=params, json={"repo": url, **body}, headers=AUTH)

    # --- tests -------------------------------------------------------------------------------

    def test_url_forms(self):
        for text in ("https://github.com/codesensei-tushar/graph_hacks", "github.com/codesensei-tushar/graph_hacks.git",
                     "https://www.github.com/codesensei-tushar/graph_hacks/pulls?q=1", "codesensei-tushar/graph_hacks"):
            self.assertEqual(public_repos.parse(text), "codesensei-tushar/graph_hacks", text)
        for text in ("", "https://gitlab.com/a/b", "https://github.com/onlyowner", "https://github.com/a/.."):
            with self.assertRaises(public_repos.PublicRepoError, msg=text):
                public_repos.parse(text)

    def test_operator_adds_the_whole_repo_to_the_legacy_org(self):
        # As the demo asks for it: the repo by name, every pull request.
        response = self._add(url="codesensei-tushar/graph_hacks", backfill="all")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "backfilling")
        self._drain()
        legacy = tenancy.legacy_org()
        pulls = self._count(legacy, "MATCH (p:PullRequest)-[:IN_REPO]->(:Repo {id: $id}) RETURN count(p), collect(DISTINCT p.scope)", {"id": REPO_ID})
        self.assertEqual(pulls[0][0], 230)
        self.assertEqual(pulls[0][1], ["org_shared"])
        issues = self._count(legacy, "MATCH (t:Ticket {source: 'github'})-[:IN_REPO]->(:Repo {id: $id}) RETURN count(t)", {"id": REPO_ID})
        self.assertEqual(issues[0][0], 12)
        # TOUCHES from Service.paths, reviews, and when it merged.
        touches = self._count(legacy, "MATCH (:PullRequest)-[:TOUCHES]->(s:Service) RETURN DISTINCT s.id")
        self.assertEqual(touches, [[service] for service in self._services("src/domain/memory/search.py")])
        self.assertTrue(touches)
        reviewed = self._count(legacy, "MATCH (:GitHubUser)-[:REVIEWED]->(p:PullRequest) RETURN count(DISTINCT p)")
        self.assertEqual(reviewed[0][0], 230)
        merged = self._count(legacy, "MATCH (p:PullRequest {number: 13}) RETURN p.merged_at, p.created_at, p.merged")
        self.assertEqual(merged, [["2026-09-02T10:00:00Z", "2026-09-01T00:00:00Z", True]])
        # No app at all: every read went out unauthenticated, and no collaborators were asked for.
        self.assertEqual(set(self.github.auth_seen), {"anonymous"})
        self.assertNotIn(("GET", "/repos/codesensei-tushar/graph_hacks/collaborators"), self.github.calls)

        status = self.client.get("/ops/github/public-repos", headers=AUTH).json()
        self.assertEqual(status["org"], "quillfern")
        [row] = status["repos"]
        self.assertEqual((row["status"], row["pull_requests"], row["issues"], row["backfill_limit"], row["scope"]), ("ready", 230, 12, 0, "org_shared"))
        # And the other org saw none of it.
        self.assertEqual(self._count(self.acme.ref(), "MATCH (n) WHERE n:Repo OR n:PullRequest RETURN count(n)")[0][0], 0)

    def test_default_depth_is_fifty_and_a_depth_is_kept(self):
        self.assertEqual(self._add().status_code, 200)
        self._drain()
        legacy = tenancy.legacy_org()
        self.assertEqual(self._count(legacy, "MATCH (p:PullRequest) RETURN count(p)")[0][0], 50)
        # Newest first: the most recent pull request is in.
        self.assertEqual(self._count(legacy, "MATCH (p:PullRequest {number: 242}) RETURN count(p)")[0][0], 1)
        resync = self.client.post(f"/ops/github/public-repos/{REPO_ID}/sync", json={"backfill": 60}, headers=AUTH)
        self.assertEqual(resync.status_code, 200, resync.text)
        self._drain()
        self.assertEqual(self._count(legacy, "MATCH (p:PullRequest) RETURN count(p)")[0][0], 60)
        self.assertEqual(self.client.get("/ops/github/public-repos", headers=AUTH).json()["repos"][0]["backfill_limit"], 60)
        bad = self.client.post(f"/ops/github/public-repos/{REPO_ID}/sync", json={"backfill": "most"}, headers=AUTH)
        self.assertEqual((bad.status_code, bad.json()["error"]), (400, "bad_limit"))

    def test_a_rate_limit_is_waited_out(self):
        self._add(backfill_limit=20)
        self.github.rate_limit_next = 3
        self._drain()
        self.assertEqual(self._count(tenancy.legacy_org(), "MATCH (p:PullRequest) RETURN count(p)")[0][0], 20)
        self.assertEqual(self.github.rate_limit_next, 0)

    def test_refusals(self):
        self.assertEqual(self._add(url="https://github.com/codesensei-tushar/secret").json()["error"], "not_found")
        self.assertEqual(self._add(url="https://github.com/nobody/nothing").json()["error"], "not_found")
        self.assertEqual(self._add(url="not a url").json()["error"], "bad_url")
        self.assertEqual(self._add(org="no_such_org").status_code, 404)
        self.assertEqual(self.client.post("/ops/github/public-repos", json={"repo": REPO_URL}).status_code, 403)
        self.assertEqual(self.client.get("/ops/github/public-repos").status_code, 403)

    def test_another_org_and_removal(self):
        acme = self.acme.ref()
        self.assertEqual(self._add(org=self.acme.id, backfill_limit=15).status_code, 200)
        self._drain()
        self.assertEqual(self._count(acme, "MATCH (p:PullRequest) RETURN count(p)")[0][0], 15)
        self.assertEqual(self._count(tenancy.legacy_org(), "MATCH (r:Repo {id: $id}) RETURN count(r)", {"id": REPO_ID})[0][0], 0)
        self.assertEqual(self.client.get("/ops/github/public-repos", headers=AUTH).json()["repos"], [])

        removed = self.client.delete(f"/ops/github/public-repos/{REPO_ID}", params={"org": self.acme.id}, headers=AUTH)
        self.assertEqual(removed.status_code, 202)
        self._drain()
        left = self._count(acme, "MATCH (n) WHERE n:Repo OR n:PullRequest OR (n:Ticket AND n.source = 'github') OR n:GitHubUser RETURN count(n)")
        self.assertEqual(left[0][0], 0)
        self.assertEqual(tenancy.control_graph().query("MATCH (p:GitHubPublicRepo) RETURN count(p)").result_set[0][0], 0)
        # A step that was still queued finds the repo gone and does nothing.
        with tenancy.org_context(acme):
            backfill.run_repo_step({"repo": REPO_ID})
        self.assertEqual(self._count(acme, "MATCH (n:PullRequest) RETURN count(n)")[0][0], 0)

    def test_poll_reads_what_changed(self):
        self._add(backfill_limit=5)
        self._drain()
        data = self.github.repos["codesensei-tushar/graph_hacks"]
        data["pulls"].append({"number": 999, "title": "A brand new change", "body": "", "state": "open", "user": user(503, "newcomer"),
                              "html_url": "https://github.com/x/pull/999", "updated_at": "2099-01-01T00:00:00Z", "created_at": "2099-01-01T00:00:00Z",
                              "merged_at": None, "closed_at": None})
        data["files"][999] = [{"filename": "src/domain/memory/a.py"}]
        data["reviews"][999] = []
        with tenancy.org_context(tenancy.legacy_org()):
            backfill.run_public_poll({"repo": REPO_ID})
        rows = self._count(tenancy.legacy_org(), "MATCH (p:PullRequest {number: 999})-[:TOUCHES]->(s:Service) RETURN s.id")
        self.assertEqual(rows, [[service] for service in self._services("src/domain/memory/a.py")])
        self.assertTrue(rows)

    def test_console_admin_adds_to_their_own_org(self):
        session = FakeSession(org=self.acme.id)
        self.app.dependency_overrides[console_session.console_user] = lambda: session
        self.app.dependency_overrides[console_session.console_admin] = lambda: session
        self.addCleanup(self.app.dependency_overrides.clear)
        response = self.client.post("/console/api/github/public-repos", json={"repo": REPO_URL, "backfill": 10})
        self.assertEqual(response.status_code, 200, response.text)
        self._drain()
        self.assertEqual(self._count(self.acme.ref(), "MATCH (p:PullRequest) RETURN count(p)")[0][0], 10)
        listed = self.client.get("/console/api/github/public-repos").json()
        self.assertEqual([row["full_name"] for row in listed["repos"]], ["codesensei-tushar/graph_hacks"])
        self.assertEqual(self._count(tenancy.legacy_org(), "MATCH (p:PullRequest) RETURN count(p)")[0][0], 0)
        self.assertEqual(self.client.delete(f"/console/api/github/public-repos/{REPO_ID}").status_code, 202)

    def test_public_reads_use_the_token_and_fall_back_when_it_is_refused(self):
        with mock.patch.object(client, "settings", dataclasses.replace(self.live, github_public_repo_token=PUBLIC_TOKEN)):
            reader = client.public_client()
            self.assertEqual(reader.mode, "token")
            reader.get_repo("codesensei-tushar/graph_hacks")
            self.assertEqual(self.github.auth_seen[-1], "token")
        with mock.patch.object(client, "settings", dataclasses.replace(self.live, github_public_repo_token="revoked")):
            reader = client.public_client()
            self.assertEqual(reader.get_repo("codesensei-tushar/graph_hacks")["id"], int(REPO_ID))
            self.assertEqual(self.github.auth_seen[-1], "anonymous")
            self.assertEqual(reader.mode, "anonymous")

if __name__ == "__main__":
    unittest.main()
