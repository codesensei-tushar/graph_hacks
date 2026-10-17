"""The catalogue over MCP, end to end: a real HTTP server, the SDK's client, Quillfern.

Serves the app with uvicorn in a thread on a free port, then uses
``mcp.client.streamable_http`` to initialise, list tools and call ``who_owns``. Also
checks the token gate and that the bare ``/mcp`` path is served, not redirected.
Needs FalkorDB on localhost:6379 (``docker compose up -d``).
"""

import asyncio
import dataclasses
import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client  # noqa: E402

from src.api import mcp as mcp_api  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import mcp_tools  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_mcp_test{tests.TEST_GRAPH_SUFFIX}"
TOKEN = "mcp-test-token"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class McpServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
            mock.patch.object(mcp_api, "settings", dataclasses.replace(settings, mcp_token=TOKEN)),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)

        @asynccontextmanager
        async def lifespan(_app):
            async with mcp_api.session_lifespan():
                yield

        app = FastAPI(lifespan=lifespan)
        mcp_api.install(app)
        cls.port = _free_port()
        cls.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=cls.port, log_level="warning"))
        cls.thread = threading.Thread(target=cls.server.run, name="mcp-test-server", daemon=True)
        cls.thread.start()
        deadline = time.time() + 15
        while not cls.server.started and time.time() < deadline:
            time.sleep(0.05)
        assert cls.server.started, "uvicorn did not start"
        cls.url = f"http://127.0.0.1:{cls.port}/mcp"

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(10)
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    # --- through the SDK client --------------------------------------------------------------

    async def _with_session(self, work):
        async with create_mcp_http_client(headers={"Authorization": f"Bearer {TOKEN}"}) as http:
            async with streamable_http_client(self.url, http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    return await work(session)

    def test_lists_the_read_only_catalogue(self):
        async def work(session):
            return await session.list_tools()

        listed = asyncio.run(self._with_session(work))
        tools = {tool.name: tool for tool in listed.tools}
        self.assertEqual(sorted(tools), sorted(mcp_tools.tool_names()))
        self.assertNotIn("ingest_ticket", tools)
        self.assertNotIn("record_decision", tools)
        for expected in mcp_tools.read_only_tools():
            self.assertEqual(tools[expected.name].input_schema, expected.input_schema, expected.name)
            self.assertEqual(tools[expected.name].description, expected.description, expected.name)

    def test_who_owns_on_quillfern_cites_the_path(self):
        async def work(session):
            return await session.call_tool("who_owns", {"name": "billing"})

        result = asyncio.run(self._with_session(work))
        self.assertFalse(result.is_error, result)
        structured = result.structured_content
        self.assertEqual(structured["tool"], "who_owns")
        self.assertTrue(structured["found"])
        self.assertIn({"from": "Arjun Mehta", "rel": "OWNS", "to": "Billing Service"}, structured["path"])
        self.assertEqual(json.loads(result.content[0].text), structured)

    def test_a_miss_comes_back_with_candidates_not_a_guess(self):
        async def work(session):
            return await session.call_tool("blast_radius", {"name": "the espresso machine"})

        result = asyncio.run(self._with_session(work))
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["found"])
        self.assertEqual(result.structured_content["path"], [])

    def test_writing_tools_are_refused(self):
        async def work(session):
            return await session.call_tool("ingest_ticket", {"title": "billing exports time out"})

        result = asyncio.run(self._with_session(work))
        self.assertTrue(result.is_error)
        self.assertIn("no tool named 'ingest_ticket'", result.content[0].text)

    # --- the gate ----------------------------------------------------------------------------------

    def _post(self, path: str, token: str | None) -> int:
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=b"{}", method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json, text/event-stream")
        if token is not None:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    def test_token_gate_and_bare_path(self):
        self.assertEqual(self._post("/mcp", None), 401)
        self.assertEqual(self._post("/mcp/", "wrong"), 403)
        # The bare path is served by the MCP app (an empty body is a bad request there), not redirected.
        self.assertNotIn(self._post("/mcp", TOKEN), (307, 401, 403, 404))
        with mock.patch.object(mcp_api, "settings", dataclasses.replace(settings, mcp_token="")):
            self.assertEqual(self._post("/mcp/", TOKEN), 503)


if __name__ == "__main__":
    unittest.main()
