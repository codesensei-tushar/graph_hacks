"""Operator routes for orgs and stored credentials. All need ``BRAIN_ADMIN_TOKEN``.

``POST /ops/orgs/migrate``   once, before the console is turned on: creates the legacy org's
                             ``Org`` node in the control graph and a ``WorkspaceRoute`` to it
                             for every workspace installed today. Idempotent.
``GET  /ops/orgs``           every org: id, slug, name, graph, its routed Slack teams.
``POST /ops/orgs``           ``{name}``: a new org with its own graph (no invite code: the operator).
``POST /ops/tokens/rewrap``  every stored Slack bot token and connector secret, in every org,
                             re-encrypted under the first ``TOKEN_ENCRYPTION_KEYS`` key: plaintext
                             legacy values get encrypted, values under an older key move to the
                             new one. Run it after adding a key, before dropping the old one.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.api.v1.slack_install import _admin_denial
from src.connectors import store
from src.core import tenancy
from src.db.falkordb_client import connect
from src.domain import orgs
from src.domain.slack.token_crypto import encryption_available, rewrap_token
from src.domain.slack.workspace import bot_identity, list_workspace_rows

log = logging.getLogger(__name__)
router = APIRouter(tags=["ops"])


def _all_orgs() -> list[tenancy.OrgRef]:
    return [tenancy.legacy_org(), *(org.ref() for org in orgs.list_orgs() if not org.legacy)]


def migrate_legacy_org() -> dict:
    """The legacy Org node and a route for each of its workspaces (stored installs and the env token's team)."""
    orgs.ensure_control_indexes()
    legacy = orgs.ensure_legacy_org()
    with tenancy.org_context(tenancy.legacy_org()):
        teams = [row["team_id"] for row in list_workspace_rows() if row.get("team_id")]
        identity = bot_identity()
    if identity and identity.get("team_id") and identity["team_id"] not in teams:
        teams.append(identity["team_id"])
    routed, skipped = [], []
    for team in teams:
        current = orgs.routed_org_id(team)
        if current not in (None, legacy.id):
            # Already another org's: never moved back by the migration.
            skipped.append(team)
            continue
        orgs.route_team(team, legacy.id)
        routed.append(team)
    orgs.clear_route_cache()
    log.info("[orgs] migration: legacy org %s, routed=%d skipped=%d", legacy.id, len(routed), len(skipped))
    return {"org": legacy.to_dict(), "routed": routed, "skipped": skipped}


@router.post("/ops/orgs/migrate")
def migrate(request: Request):
    if denied := _admin_denial(request):
        return denied
    return migrate_legacy_org()


@router.get("/ops/orgs")
def list_all(request: Request):
    if denied := _admin_denial(request):
        return denied
    orgs.ensure_control_indexes()
    found = orgs.list_orgs()
    if not any(org.legacy for org in found):
        found.insert(0, orgs.get_org(tenancy.LEGACY_ORG_ID))
    return {"orgs": [{**org.to_dict(), "teams": orgs.teams_of(org.id)} for org in found]}


@router.post("/ops/orgs")
async def create(request: Request):
    if denied := _admin_denial(request):
        return denied
    try:
        body = await request.json()
    except ValueError:
        body = None
    name = (body or {}).get("name") if isinstance(body, dict) else None
    if not isinstance(name, str) or not name.strip():
        return JSONResponse({"error": "name is required"}, status_code=400)
    orgs.ensure_control_indexes()
    org = orgs.create_org(name, created_by="operator")
    return {"org": org.to_dict()}


def rewrap_everything() -> dict:
    counts = {"slack_tokens": 0, "connector_secrets": 0, "failed": 0}
    for org in _all_orgs():
        with tenancy.org_context(org):
            _, graph = connect()
            rows = graph.query("MATCH (w:SlackWorkspace) WHERE w.bot_token_enc IS NOT NULL RETURN w.team_id, w.bot_token_enc").result_set
            for team_id, value in rows:
                try:
                    rewrapped = rewrap_token(value)
                except Exception:
                    log.warning("[ops] bot token of team=%s could not be re-wrapped", team_id)
                    counts["failed"] += 1
                    continue
                if rewrapped:
                    graph.query(
                        "MATCH (w:SlackWorkspace {team_id: $team}) WHERE w.bot_token_enc = $old SET w.bot_token_enc = $new",
                        {"team": team_id, "old": value, "new": rewrapped},
                    )
                    counts["slack_tokens"] += 1
            try:
                counts["connector_secrets"] += store.rewrap_all()
            except Exception:
                log.warning("[ops] connector secrets of org=%s could not be re-wrapped", org.id)
                counts["failed"] += 1
    return counts


@router.post("/ops/tokens/rewrap")
def rewrap(request: Request):
    if denied := _admin_denial(request):
        return denied
    if not encryption_available():
        return JSONResponse({"error": "set TOKEN_ENCRYPTION_KEYS first"}, status_code=409)
    return {"ok": True, **rewrap_everything()}
