"""``GET /api/ask``: one read-only question against the shared graph, for the page and for curl.

Reads ``org_shared`` only and never runs a tool that writes; naming one answers 400. The
keyword router picks the tool. ``?model=1`` lets the model pick instead, behind the admin
token as a bearer header: 503 until ``BRAIN_ADMIN_TOKEN`` is set, 401 without a token, 403
with the wrong one. ``GET /api/ask/tools`` lists the read-only catalogue and the example
questions for the seeded company.
"""

import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.core.config import settings
from src.domain.knowledge import ask_api

router = APIRouter(prefix="/api", tags=["ask"])


def _denial(request: Request) -> JSONResponse | None:
    if not settings.brain_admin_token:
        return JSONResponse({"error": "the model picker is disabled: set BRAIN_ADMIN_TOKEN"}, status_code=503)
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return JSONResponse({"error": "bearer token required"}, status_code=401)
    if not secrets.compare_digest(header[7:], settings.brain_admin_token):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return None


@router.get("/ask")
def ask(request: Request, q: str = "", tool: str | None = None, model: bool = False):
    if model and (denied := _denial(request)):
        return denied
    try:
        answer = ask_api.answer_question(q, tool=tool, use_model=model)
    except ask_api.ToolRefused as refused:
        return JSONResponse({"error": str(refused)}, status_code=400)
    return answer.to_dict()


@router.get("/ask/tools")
def tools():
    examples = ask_api.examples()
    return {
        "company": examples.company_id,
        "company_name": examples.company_name,
        "examples": examples.questions,
        "demo": ask_api.demo_steps(examples.company_id),
        "tools": [tool.to_dict() for tool in ask_api.read_tools()],
    }


@router.get("/ask/proof")
def ask_proof():
    """The ablation a public page can show: org_shared questions, with the graph and without it.

    ``GET /api/proof`` stays behind the admin token. This route reads the same public
    slice the ask page already answers, and it writes nothing.
    """
    from src.domain.knowledge.proof import run_proof

    return run_proof().to_dict()
