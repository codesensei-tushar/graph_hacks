"""``GET /api/teams`` and ``GET /api/team/{team_id}``: what the team pages show, read-only and public.

Reads ``org_shared`` only, like the ask routes, and shares their per-address rate limit and
their one-minute cache. An unknown team is a 404.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.api.v1.ask import _throttled
from src.domain.knowledge import team_page

router = APIRouter(prefix="/api", tags=["teams"])


@router.get("/teams")
def teams(request: Request):
    if throttled := _throttled(request):
        return throttled
    return {"teams": [team.to_dict() for team in team_page.public_teams()]}


@router.get("/team/{team_id}")
def team(request: Request, team_id: str):
    if throttled := _throttled(request):
        return throttled
    page = team_page.public_team_page(team_id) if len(team_id) <= team_page.MAX_TEAM_ID else None
    if page is None:
        return JSONResponse({"error": f"no team with id {team_id[: team_page.MAX_TEAM_ID]!r} is in the graph"}, status_code=404)
    return page.to_dict()
