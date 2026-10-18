"""``GET /api/explore``, ``/api/explore/neighbors`` and ``/api/explore/start``: what the explorer page walks.

Read-only and public: ``org_shared`` only, like the ask routes, sharing their one-minute cache.
A walk takes any node name, so it is counted against their per-address limit; the starters are
the same for every visitor, go out with ``Cache-Control`` and are not counted. A node name over ``explorer.MAX_NODE_CHARS`` characters is a
400; a node the graph does not have answers 200 with ``found: false`` and the nearest nodes.
A walk whose query runs past FalkorDB's timeout is a 503 that says so, like a question.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.api.v1.ask import _throttled, cached, timed_out
from src.domain.knowledge import explorer, tool_guard
from src.domain.knowledge.ask_api import QuestionTooLong

router = APIRouter(prefix="/api/explore", tags=["explore"])


@router.get("")
def explore(request: Request, node: str = "", depth: int = 1):
    if throttled := _throttled(request):
        return throttled
    try:
        with tool_guard.guard("explore", node, timeout_ms=tool_guard.PUBLIC_QUERY_TIMEOUT_MS):
            return explorer.public_explore(node, depth).to_dict()
    except QuestionTooLong as refused:
        return JSONResponse({"error": str(refused)}, status_code=400)
    except tool_guard.ToolTimedOut as error:
        return timed_out(error)


@router.get("/neighbors")
def neighbors(request: Request, node: str = "", relation: str = "", direction: str = "both", depth: int = 1):
    if throttled := _throttled(request):
        return throttled
    try:
        with tool_guard.guard("explore", f"{node} {relation} {direction}", timeout_ms=tool_guard.PUBLIC_QUERY_TIMEOUT_MS):
            return explorer.public_neighbors(node, relation, direction, depth).to_dict()
    except QuestionTooLong as refused:
        return JSONResponse({"error": str(refused)}, status_code=400)
    except tool_guard.ToolTimedOut as error:
        return timed_out(error)


@router.get("/start")
def start():
    return cached({"starters": [node.to_dict() for node in explorer.public_starters()]})
