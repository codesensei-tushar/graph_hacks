"""``GET /api/proof``: the same questions with and without the graph, behind the admin token.

503 until ``BRAIN_ADMIN_TOKEN`` is set, 401 without a bearer header, 403 with the wrong
one, like the Slack status route. JSON by default; ``?format=text`` for the terminal.
"""

import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from src.core.config import settings
from src.domain.knowledge.proof import run_proof

router = APIRouter(prefix="/api", tags=["proof"])


def _denial(request: Request) -> JSONResponse | None:
    if not settings.brain_admin_token:
        return JSONResponse({"error": "admin routes are disabled: set BRAIN_ADMIN_TOKEN"}, status_code=503)
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return JSONResponse({"error": "bearer token required"}, status_code=401)
    if not secrets.compare_digest(header[7:], settings.brain_admin_token):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return None


@router.get("/proof")
def proof(request: Request, format: str = "json"):
    if denied := _denial(request):
        return denied
    report = run_proof()
    if format == "text":
        return PlainTextResponse(report.to_text())
    return report.to_dict()
