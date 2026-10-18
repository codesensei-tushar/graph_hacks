"""TENANCY_WRITE_STRICT: a graph write with no org in context raises; reads keep the legacy fallback.

The legacy-only surfaces (public site and ask, MCP, GitHub, /ops/seed and /ops/import) set the
legacy org explicitly through ``tenancy.LegacyOrgRoutes``, and so does startup, so they keep
writing with the switch on. Every route of the app is either such a surface or one that sets its
own org: the inventory test makes a new route choose.
"""

import unittest
import unittest.mock
import uuid

import tests  # noqa: F401  pins the test graph before src loads

from fastapi import BackgroundTasks, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.core import tenancy  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.db import falkordb_client  # noqa: E402

# Routes that set their own org: Slack per team, the console per session, the operator's org routes
# per ?org=, connector webhooks and admin routes per install. Anything else must be a legacy path.
OWN_ORG_PREFIXES = ("/slack/", "/console/", "/auth/", "/ops/orgs", "/ops/tokens", "/ops/invites", "/ops/connectors", "/connectors/", "/healthz")


def _write_strict(on: bool) -> None:
    object.__setattr__(settings, "tenancy_write_strict", on)


class WriteStrictTest(unittest.TestCase):
    def setUp(self):
        _write_strict(True)
        self.addCleanup(_write_strict, False)
        self.marker = f"strict-{uuid.uuid4().hex[:8]}"

    def tearDown(self):
        with tenancy.org_context(tenancy.legacy_org()):
            _, graph = falkordb_client.connect()
            graph.query("MATCH (n:StrictProbe) DELETE n")

    def test_a_write_with_no_org_raises_and_a_read_falls_back(self):
        self.assertIsNone(tenancy.current_org_or_none())
        _, graph = falkordb_client.connect()
        self.assertEqual(graph.name, settings.falkordb_graph)  # the read fallback is unchanged
        self.assertEqual(graph.query("RETURN 1").result_set, [[1]])
        self.assertEqual(graph.ro_query("MATCH (n:StrictProbe) RETURN count(n)").result_set, [[0]])
        for write in ("CREATE (:StrictProbe {id: $id})", "MERGE (n:StrictProbe {id: $id})", "MATCH (n:StrictProbe {id: $id}) SET n.x = 1", "MATCH (n:StrictProbe {id: $id}) DELETE n"):
            with self.subTest(write=write), self.assertRaises(tenancy.NoOrgContext):
                graph.query(write, {"id": self.marker})
        with self.assertRaises(tenancy.NoOrgContext):
            graph.delete()
        with tenancy.org_context(tenancy.legacy_org()):
            self.assertEqual(falkordb_client.connect()[1].ro_query("MATCH (n:StrictProbe) RETURN count(n)").result_set, [[0]])

    def test_the_same_write_in_an_org_context_goes_through(self):
        with tenancy.org_context(tenancy.legacy_org()):
            _, graph = falkordb_client.connect()
            graph.query("CREATE (:StrictProbe {id: $id})", {"id": self.marker})
        # A graph handed out inside the context keeps working after it: it was chosen, not defaulted.
        graph.query("MATCH (n:StrictProbe {id: $id}) SET n.x = 1", {"id": self.marker})
        self.assertEqual(graph.ro_query("MATCH (n:StrictProbe {id: $id}) RETURN n.x", {"id": self.marker}).result_set, [[1]])

    def test_off_the_fallback_still_writes(self):
        _write_strict(False)
        _, graph = falkordb_client.connect()
        graph.query("CREATE (:StrictProbe {id: $id})", {"id": self.marker})

    def test_legacy_routes_run_as_the_legacy_org_and_others_do_not(self):
        app = FastAPI()
        seen = {}

        def write(where: str) -> None:
            _, graph = falkordb_client.connect()
            graph.query("CREATE (:StrictProbe {id: $id, where: $where})", {"id": self.marker, "where": where})
            seen[where] = tenancy.current_org()

        @app.post("/github/webhook")
        def github(background: BackgroundTasks):
            background.add_task(write, "github-background")
            return {"ok": True}

        @app.post("/ops/seed")
        def seed():
            write("seed")
            return {"ok": True}

        @app.post("/console/api/thing")
        def console():
            write("console")
            return {"ok": True}

        app.add_middleware(tenancy.LegacyOrgRoutes)
        client = TestClient(app, raise_server_exceptions=False)
        self.assertEqual(client.post("/github/webhook").status_code, 200)
        self.assertEqual(client.post("/ops/seed").status_code, 200)
        self.assertEqual(seen, {"github-background": tenancy.LEGACY_ORG_ID, "seed": tenancy.LEGACY_ORG_ID})
        # A route that forgot its org fails loudly instead of writing into the legacy graph.
        self.assertEqual(client.post("/console/api/thing").status_code, 500)
        self.assertNotIn("console", seen)

    def test_every_route_is_a_legacy_surface_or_sets_its_own_org(self):
        from src.main import app

        paths = sorted(app.openapi()["paths"])
        self.assertIn("/github/webhook", paths)
        unclassified = [p for p in paths if not tenancy.is_legacy_path(p) and not p.startswith(OWN_ORG_PREFIXES)]
        self.assertEqual(unclassified, [], "a new route: add it to tenancy.LEGACY_* or set its org, then list it here")
        for legacy in ("/ops/seed", "/ops/import", "/github/webhook", "/github/oauth/callback", "/api/ask", "/mcp", "/", "/explore", "/team/payments"):
            self.assertTrue(tenancy.is_legacy_path(legacy), legacy)
        for own in ("/slack/events", "/console/api/ask", "/ops/orgs", "/connectors/jira/webhook", "/ops/seedling"):
            self.assertFalse(tenancy.is_legacy_path(own), own)

    def test_startup_writes_run_as_the_legacy_org(self):
        from src import main

        with unittest.mock.patch.object(main, "_seed_if_empty", side_effect=lambda: seen.append(tenancy.current_org())):
            seen: list = []
            import asyncio

            async def boot():
                async with main.lifespan(main.app):
                    pass

            import contextlib

            @contextlib.asynccontextmanager
            async def no_mcp():
                yield

            with (
                unittest.mock.patch.object(main.scheduler, "start"),
                unittest.mock.patch.object(main.threading, "Thread"),
                unittest.mock.patch.object(main.mcp_api, "session_lifespan", no_mcp),
            ):
                asyncio.run(boot())
        self.assertEqual(seen, [tenancy.LEGACY_ORG_ID])


if __name__ == "__main__":
    unittest.main()
