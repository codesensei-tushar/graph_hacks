"""``GET /api/ask``: one read-only question against the shared graph, for the page and for curl.

Reads ``org_shared`` only and never runs a tool that writes; naming one answers 400. The
keyword router picks the tool. ``?model=1`` lets the model pick instead, behind the admin
token as a bearer header: 503 until ``BRAIN_ADMIN_TOKEN`` is set, 401 without a token, 403
with the wrong one. ``GET /api/ask/tools`` lists the read-only catalogue and the example
questions for the seeded company.

Every route here is public: each client (``X-Real-IP``, which nginx sets) gets
``ask_api.RATE_LIMIT`` calls a minute and then a 429 with ``Retry-After``; a question over
``ask_api.MAX_QUESTION_CHARS`` characters is a 400. Answers and the proof are cached in the
domain for a minute.
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


def _throttled(request: Request) -> JSONResponse | None:
    client = request.headers.get("x-real-ip") or (request.client.host if request.client else None)
    decision = ask_api.admit(client)
    if decision.allowed:
        return None
    return JSONResponse(
        {"error": f"too many questions from this address; try again in {decision.retry_after} seconds"},
        status_code=429,
        headers={"Retry-After": str(decision.retry_after)},
    )


@router.get("/ask")
def ask(request: Request, q: str = "", tool: str | None = None, model: bool = False):
    if throttled := _throttled(request):
        return throttled
    if model and (denied := _denial(request)):
        return denied
    try:
        answer = ask_api.answer_question(q, tool=tool, use_model=model)
    except (ask_api.ToolRefused, ask_api.QuestionTooLong) as refused:
        return JSONResponse({"error": str(refused)}, status_code=400)
    return answer.to_dict()


@router.get("/ask/tools")
def tools(request: Request):
    if throttled := _throttled(request):
        return throttled
    examples = ask_api.examples()
    return {
        "company": examples.company_id,
        "company_name": examples.company_name,
        "examples": examples.questions,
        "demo": ask_api.demo_steps(examples.company_id),
        "tools": [tool.to_dict() for tool in ask_api.read_tools()],
    }


@router.get("/ask/proof")
def ask_proof(request: Request):
    """The ablation a public page can show: org_shared questions, with the graph and without it.

    ``GET /api/proof`` stays behind the admin token. This route reads the same public
    slice the ask page already answers, and it writes nothing.
    """
    if throttled := _throttled(request):
        return throttled
    return ask_api.public_proof().to_dict()
