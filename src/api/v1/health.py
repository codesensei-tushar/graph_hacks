from fastapi import APIRouter

from src.core.config import settings
from src.db.falkordb_client import connect

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
def health():
    _, graph = connect()
    nodes = graph.query("MATCH (n) RETURN count(n)").result_set[0][0]
    return {"ok": True, "nodes": nodes, "slack": settings.slack_enabled}
