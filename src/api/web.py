"""The judge-facing page at ``/`` and its files under ``/static``.

``install`` adds the ask routes, the page route and the static mount in one call, so
``main.py`` needs one line. The page route is a plain route and the mount sits under its
own prefix, so neither shadows ``/healthz``, ``/api/*``, ``/slack/*``, ``/github/*`` or
``/mcp``; an unknown path still answers FastAPI's JSON 404.
"""

from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.api.v1 import ask

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
INDEX = STATIC_DIR / "index.html"

router = APIRouter(tags=["web"], include_in_schema=False)


@router.get("/")
def index():
    return FileResponse(INDEX, media_type="text/html", headers={"Cache-Control": "no-cache"})


def install(app: FastAPI) -> None:
    app.include_router(ask.router)
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
