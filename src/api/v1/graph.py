"""``GET /api/graph/stats`` and ``GET /api/graph/sample``: the real graph, for the landing page's hero.

Read-only and public: ``org_shared`` only, like the ask routes, sharing their per-address
rate limit and their one-minute cache. ``limit`` is clamped to 1..60 in the domain.
"""

from fastapi import APIRouter, Request

from src.api.v1.ask import _throttled
from src.domain.knowledge import graph_overview

router = APIRouter(prefix="/api/graph", tags=["graph"])


@router.get("/stats")
def stats(request: Request):
    if throttled := _throttled(request):
        return throttled
    return graph_overview.public_stats().to_dict()


@router.get("/sample")
def sample(request: Request, limit: int = graph_overview.DEFAULT_SAMPLE):
    if throttled := _throttled(request):
        return throttled
    return graph_overview.public_sample(limit).to_dict()
