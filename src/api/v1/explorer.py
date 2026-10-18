"""``GET /api/explore``, ``/api/explore/neighbors`` and ``/api/explore/start``: what the explorer page walks.

Read-only and public: ``org_shared`` only, like the ask routes, sharing their one-minute cache.
A walk takes any node name, so it is counted against their per-address limit; the starters are
the same for every visitor, go out with ``Cache-Control`` and are not counted. A node name over ``explorer.MAX_NODE_CHARS`` characters is a
400; a node the graph does not have answers 200 with ``found: false`` and the nearest nodes.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.api.v1.ask import _throttled, cached
from src.domain.knowledge import explorer
from src.domain.knowledge.ask_api import QuestionTooLong

router = APIRouter(prefix="/api/explore", tags=["explore"])


@router.get("")
def explore(request: Request, node: str = "", depth: int = 1):
    if throttled := _throttled(request):
        return throttled
    try:
        return explorer.public_explore(node, depth).to_dict()
    except QuestionTooLong as refused:
        return JSONResponse({"error": str(refused)}, status_code=400)


@router.get("/neighbors")
def neighbors(request: Request, node: str = "", relation: str = "", direction: str = "both", depth: int = 1):
    if throttled := _throttled(request):
        return throttled
    try:
        return explorer.public_neighbors(node, relation, direction, depth).to_dict()
    except QuestionTooLong as refused:
        return JSONResponse({"error": str(refused)}, status_code=400)


@router.get("/start")
def start():
    return cached({"starters": [node.to_dict() for node in explorer.public_starters()]})
