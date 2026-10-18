"""``GET /api/graph/stats`` and ``GET /api/graph/sample``: the real graph, for the landing page's hero.

Read-only and public: ``org_shared`` only, like the ask routes. Served from the one-minute
cache with ``Cache-Control`` and not counted against the per-address limit: every visitor gets
the same answer. ``limit`` is clamped to 1..60 in the domain, so the keys are bounded.
"""

from fastapi import APIRouter

from src.api.v1.ask import cached
from src.domain.knowledge import graph_overview

router = APIRouter(prefix="/api/graph", tags=["graph"])


@router.get("/stats")
def stats():
    return cached(graph_overview.public_stats().to_dict())


@router.get("/sample")
def sample(limit: int = graph_overview.DEFAULT_SAMPLE):
    return cached(graph_overview.public_sample(limit).to_dict())
