"""The tool catalogue over MCP: mount, token check, and the session manager's lifespan.

The MCP server itself is the SDK's low-level ``Server`` with explicit handlers, so each
tool's input schema is the catalogue's ``args_schema`` verbatim rather than one derived
from a Python signature. What a tool does lives in ``src/domain/knowledge/mcp_tools.py``;
this module only moves bytes: it answers ``tools/list`` and ``tools/call`` over
streamable HTTP at ``/mcp`` (stateless, JSON responses), checks the ``MCP_TOKEN`` bearer
header first (503 until the token is set, 401 without a bearer header, 403 when it is
wrong, like the other token-gated routes), and runs the SDK's session manager inside
the app's lifespan.
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
from src.domain.knowledge import mcp_tools

log = logging.getLogger(__name__)

MOUNT_PATH = "/mcp"
SERVER_NAME = "company-brain"
INSTRUCTIONS = (
    "Read-only tools over one company's knowledge graph. Every answer cites the edges it walked. "
    "Callers read the company-wide scope; nothing here writes the graph."
)


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


# --- the token check --------------------------------------------------------------------------


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


class BearerToken:
    """Pure ASGI: every MCP request carries ``Authorization: Bearer <MCP_TOKEN>``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        expected = settings.mcp_token
        if not expected:
            await _reply(send, 503, {"error": "MCP is disabled: set MCP_TOKEN"})
            return
        header = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
        presented = header[7:] if header.lower().startswith("bearer ") else ""
        if not presented:
            await _reply(send, 401, {"error": "unauthorized: send Authorization: Bearer <MCP_TOKEN>"}, [(b"www-authenticate", b"Bearer")])
            return
        if not secrets.compare_digest(presented, expected):
            await _reply(send, 403, {"error": "forbidden"})
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
    """The streamable-HTTP app at the mount root, behind the token check. Host checks are
    off because nginx fronts the public name and the token is the gate."""
    inner = server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return BearerToken(inner)


def install(app: FastAPI) -> None:
    app.mount(MOUNT_PATH, build_app())
    app.add_middleware(BareMountPath)


@asynccontextmanager
async def session_lifespan():
    """Run inside the app's lifespan; ``install`` must have been called first."""
    async with server.session_manager.run():
        yield
