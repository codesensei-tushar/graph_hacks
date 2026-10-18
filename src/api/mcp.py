"""The tool catalogue over MCP: mount, the caller gate, and the session manager's lifespan.

The MCP server itself is the SDK's low-level ``Server`` with explicit handlers, so each
tool's input schema is the catalogue's ``args_schema`` verbatim rather than one derived
from a Python signature. What a tool does lives in ``src/domain/knowledge/mcp_tools.py``;
this module only moves bytes: it answers ``tools/list`` and ``tools/call`` over
streamable HTTP at ``/mcp`` (stateless, JSON responses), gates each request, and runs the
SDK's session manager inside the app's lifespan.

``/mcp`` is public, like the ask page: the tools are read-only and read ``org_shared``.
The server is stateless JSON-RPC over POST: it has no stream to hold open for a GET and no
session to end with a DELETE, so both are 405 before anything else. The gate, per request:

- no bearer token: counted against the address's ask-page budget (``ask_api.admit``);
  over it, 429 with ``Retry-After``;
- a bearer token while ``MCP_TOKEN`` is unset: the same as no token;
- ``MCP_TOKEN`` itself: passes and is never counted;
- any other token while ``MCP_TOKEN`` is set: 403.
"""

import json
import logging
import secrets
from contextlib import asynccontextmanager

import anyio
import mcp.types as types
from fastapi import FastAPI
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from src.api.ops import GIT_SHA
from src.core.config import settings
from src.domain.knowledge import ask_api, mcp_tools

log = logging.getLogger(__name__)

MOUNT_PATH = "/mcp"
SERVER_NAME = "company-brain"
# Built from the catalogue, so the chain it describes names only tools the server lists.
INSTRUCTIONS = mcp_tools.instructions()


# --- the MCP server ---------------------------------------------------------------------------


async def _list_tools(_ctx, _params) -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[types.Tool(name=tool.name, description=tool.description, input_schema=tool.input_schema) for tool in mcp_tools.read_only_tools()]
    )


async def _call_tool(_ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    try:
        # Tools read FalkorDB synchronously; keep the event loop free for other calls.
        payload = await anyio.to_thread.run_sync(mcp_tools.call_tool, params.name, params.arguments or {})
    except mcp_tools.ToolError as error:
        return types.CallToolResult(content=[types.TextContent(type="text", text=str(error))], is_error=True)
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(payload))], structured_content=payload)


server = Server(SERVER_NAME, version=GIT_SHA, instructions=INSTRUCTIONS, on_list_tools=_list_tools, on_call_tool=_call_tool)


# --- the caller gate --------------------------------------------------------------------------


async def _reply(send: Send, status: int, body: dict, headers: list[tuple[bytes, bytes]] | None = None) -> None:
    payload = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode()), *(headers or [])],
        }
    )
    await send({"type": "http.response.body", "body": payload})


# Stateless JSON-RPC: every call is a POST. A GET would otherwise hold an empty 200 open.
ALLOWED_METHODS = ("POST",)


class CallerGate:
    """Pure ASGI: only POST is served, the right token passes uncounted, a wrong one is 403, anyone else is rate-limited."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("method") not in ALLOWED_METHODS:
            allow = [(b"allow", ", ".join(ALLOWED_METHODS).encode())]
            await _reply(send, 405, {"error": "this MCP server is stateless JSON-RPC: POST each call"}, allow)
            return
        headers = dict(scope.get("headers") or [])
        header = headers.get(b"authorization", b"").decode("latin-1")
        presented = header[7:] if header.lower().startswith("bearer ") else ""
        expected = settings.mcp_token
        if presented and expected:
            if not secrets.compare_digest(presented, expected):
                await _reply(send, 403, {"error": "forbidden"})
                return
            await self.app(scope, receive, send)
            return
        # nginx sets X-Real-IP; a direct caller is known by its socket.
        client = headers.get(b"x-real-ip", b"").decode("latin-1") or (scope.get("client") or (None,))[0]
        decision = ask_api.admit(client)
        if not decision.allowed:
            await _reply(
                send,
                429,
                {"error": f"too many calls from this address; try again in {decision.retry_after} seconds"},
                [(b"retry-after", str(decision.retry_after).encode())],
            )
            return
        await self.app(scope, receive, send)


class BareMountPath:
    """A mount answers ``/mcp/`` but redirects the bare ``/mcp`` (307), which MCP clients do
    not follow. Rewrite the bare path so both forms reach the server."""

    def __init__(self, app: ASGIApp, path: str = MOUNT_PATH) -> None:
        self.app = app
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") == self.path:
            scope = {**scope, "path": self.path + "/", "raw_path": (self.path + "/").encode()}
        await self.app(scope, receive, send)


# --- wiring ------------------------------------------------------------------------------------


def build_app() -> ASGIApp:
    """The streamable-HTTP app at the mount root, behind the caller gate. Host checks are
    off because nginx fronts the public name and the gate does the rest."""
    inner = server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return CallerGate(inner)


def install(app: FastAPI) -> None:
    app.mount(MOUNT_PATH, build_app())
    app.add_middleware(BareMountPath)


@asynccontextmanager
async def session_lifespan():
    """Run inside the app's lifespan; ``install`` must have been called first."""
    async with server.session_manager.run():
        yield
