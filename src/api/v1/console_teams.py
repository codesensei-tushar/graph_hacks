"""The console's JSON API for teams: import a team file, and list the org's teams.

    GET  /console/api/teams          the org's teams, their members and what they run (members)
    POST /console/api/teams/import   {file, check?}: merge one team file into the org (admins)

The import is ``/ops/import`` for a console org: the same TOML file (docs/ONBOARDING-TEAMS.md),
the same size limit and the same checks, against the org's own graph, and the team joins the
org's Company (``team_import.company_id``). A refused file writes nothing and answers 400 with
one message per problem; ``check: true`` runs every check and writes nothing.

Everything is 404 unless ``CONSOLE_ENABLED``. ``console_user``/``console_admin`` check the
session and CSRF, and every route runs in the session's org, so it only sees that org's graph.
The team list reads what the asker could read in their Slack DM (``read_scopes``).
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from src.api.v1.console_connectors import _audit, _enabled, _org, read_scopes
from src.core import tenancy
from src.db.falkordb_client import connect
from src.domain.console.session import console_admin, console_user
from src.domain.knowledge import kinds
from src.domain.knowledge.resolve import SCOPED
from src.domain.onboarding.team_import import MAX_FILE_BYTES, TeamFileError, check_team, company_id, contents, describe, import_team

log = logging.getLogger(__name__)

router = APIRouter(prefix="/console/api", tags=["console"], dependencies=[Depends(_enabled)])

# The JSON wrapper around the file: the key names and the check flag.
_ENVELOPE_BYTES = 4 * 1024


async def _body(request: Request) -> dict | JSONResponse:
    """The JSON body, read with the size limit applied while it streams in."""
    if not (request.headers.get("content-type") or "").startswith("application/json"):
        raise HTTPException(status_code=415, detail="send JSON")
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_FILE_BYTES + _ENVELOPE_BYTES:
            return JSONResponse({"error": "too_large", "limit_bytes": MAX_FILE_BYTES}, status_code=413)
    try:
        body = json.loads(raw)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="invalid JSON") from error
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="send a JSON object")
    return body


def _import(text: str, check: bool) -> dict:
    """Runs in the org's context (``tenancy.in_org``)."""
    company = company_id()
    if check:
        team = check_team(text, company)
        return {"mode": "check", "company": company, "team": team.TEAM_ID, "contents": describe(team), "counts": contents(team)}
    result = import_team(text, company)
    report = result.report
    log.info(
        "[console] team imported org=%s team=%s created nodes=%d edges=%d set=%d",
        tenancy.current_org(), result.team.TEAM_ID, report.nodes_created, report.edges_created, report.properties_set,
    )
    return {"mode": "merge", **result.to_dict(), "company": company}


@router.post("/teams/import")
async def import_team_file(request: Request, session=Depends(console_admin)):
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    text, check = body.get("file"), body.get("check", False)
    if not isinstance(text, str) or not text.strip():
        return JSONResponse({"error": "file is the team file's text (TOML)"}, status_code=400)
    if not isinstance(check, bool):
        return JSONResponse({"error": "check is true or false"}, status_code=400)
    if len(text.encode()) > MAX_FILE_BYTES:
        return JSONResponse({"error": "too_large", "limit_bytes": MAX_FILE_BYTES}, status_code=413)
    org = _org(session)
    try:
        answer = await run_in_threadpool(tenancy.in_org, org, _import, text, check)
    except TeamFileError as error:
        return JSONResponse({"error": "refused", "problems": error.problems}, status_code=400)
    if not check:
        _audit(org.id, session, "teams.import", answer["team"])
    return answer


def list_teams(scopes: list[str]) -> list[dict]:
    """Every team the scopes can read: members, and each thing it runs with its owners."""
    _, graph = connect()
    params = {"scopes": scopes}
    teams = {
        team_id: {"id": team_id, "name": name, "summary": summary, "members": [], "runs": []}
        for team_id, name, summary in graph.ro_query(
            f"MATCH (t:Team) WHERE {SCOPED.format(var='t')} RETURN t.id, t.name, t.summary ORDER BY t.name, t.id", params
        ).result_set
    }
    if not teams:
        return []
    params["ids"] = list(teams)
    for team_id, person_id, name, role in graph.ro_query(
        "MATCH (p:Person)-[:MEMBER_OF]->(t:Team) WHERE t.id IN $ids RETURN t.id, p.id, p.name, p.role ORDER BY p.name, p.id", params
    ).result_set:
        teams[team_id]["members"].append({"id": person_id, "name": name, "role": role})
    for team_id, thing_id, name, kind, owners in graph.ro_query(
        f"""
        MATCH (t:Team)-[:RUNS]->(s:Service)
        WHERE t.id IN $ids AND {SCOPED.format(var="s")}
        OPTIONAL MATCH (o:Person)-[:OWNS]->(s)
        WITH t, s, o ORDER BY o.name, o.id
        RETURN t.id, s.id, s.name, {kinds.KIND_OF.format(var="s")}, collect(DISTINCT {{id: o.id, name: o.name}})
        ORDER BY s.name, s.id
        """,
        params,
    ).result_set:
        teams[team_id]["runs"].append(
            {"id": thing_id, "name": name, "kind": kinds.kind_or_default(kind), "owners": [owner for owner in owners if owner.get("id")]}
        )
    return list(teams.values())


@router.get("/teams")
def teams(session=Depends(console_user)):
    org = _org(session)
    with tenancy.org_context(org):
        return {"teams": list_teams(read_scopes(session))}
