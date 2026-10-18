import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api import mcp as mcp_api
from src.api import console_web, ops, web
from src.api.v1 import auth, connectors, console_oauth, github, health, proof, slack, slack_install
from src.connectors import graph as connector_graph
from src.connectors import registry as connector_registry
from src.connectors import sync as connector_sync  # registers the connector sync job
from src.core import scheduler
from src.core.config import settings
from src.db.falkordb_client import connect
from src.domain.github import backfill as github_backfill  # registers the collaborator refresh job
from src.domain.knowledge.search import ensure_search_indexes
from src.domain.memory.memory_service import ensure_indexes
from src.domain.slack import team_invite  # noqa: F401  registers team invite jobs
from src.domain.slack import token_install
from src.domain.slack.agent_turn import answer_turn
from src.domain.slack.channel_membership import reconcile_channel_membership
from src.domain.slack.channels import observe, rollout  # noqa: F401  register rollout and observe jobs
from src.domain.slack.channels import reset as channel_reset
from src.domain.slack.compute import register_turn_agent
from src.domain.slack.turn import approval_expiry, reaction_queue  # noqa: F401  register expiry and reaction jobs
from src.domain.slack.turn import reset as turn_reset
from src.domain.slack.workspace import list_connected_workspaces
from src.seed.seed import company_module, reset_and_seed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)


def _seed_if_empty() -> None:
    if settings.seed_on_startup:
        _, graph = connect()
        nodes = graph.query("MATCH (n) RETURN count(n)").result_set[0][0]
        if nodes == 0:
            log.info("graph is empty; seeding %s (SEED_ON_STARTUP)", settings.seed_company)
            reset_and_seed(company_module(settings.seed_company))
    ensure_indexes()
    connector_graph.ensure_indexes()
    # Never raises: a failure is logged and the tools that would search keep their refusal.
    ensure_search_indexes()


def _recover() -> None:
    """Startup safety nets: work missed or orphaned while the app was down."""
    for job, name in (
        (reaction_queue.recover_passive_reaction_queue, "reaction queue"),
        (observe.reconcile_channel_observe_schedules, "channel observe schedules"),
    ):
        try:
            job()
        except Exception:
            log.exception("startup recovery failed: %s", name)
    for workspace in list_connected_workspaces():
        # Token-mode installs never hit the OAuth callback; run its after-install steps once.
        try:
            token_install.ensure_token_workspace_bootstrapped(workspace)
        except Exception:
            log.exception("token-mode bootstrap failed team=%s", workspace.team_id)
        # Private-channel joins and leaves missed while the app was down.
        try:
            reconcile_channel_membership(workspace.client)
        except Exception:
            log.exception("membership reconcile failed team=%s", workspace.team_id)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _seed_if_empty()
    register_turn_agent(answer_turn)
    turn_reset.register()
    channel_reset.register()
    token_install.register()
    scheduler.start()
    if settings.github_enabled and not scheduler.exists(github_backfill.COLLABORATOR_REFRESH_JOB_ID):
        github_backfill.schedule_collaborator_refresh()
    # Connectors with CONNECTOR_<SOURCE>_* set: configure each install and arm its sync.
    connector_registry.load(settings.connector_env)
    connector_sync.schedule_all()
    if settings.slack_enabled or settings.slack_oauth_enabled:
        threading.Thread(target=_recover, name="brain-recover", daemon=True).start()
    else:
        log.warning("Slack is not configured: set SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET, or the OAuth app credentials")
    if not settings.mcp_token:
        log.info("MCP is public and rate-limited; MCP_TOKEN lifts the limit")
    async with mcp_api.session_lifespan():
        yield


# The API docs list every route, admin ones included: served only when DOCS_ENABLED is set.
_docs = settings.docs_enabled
app = FastAPI(
    title="Company Brain",
    lifespan=lifespan,
    docs_url="/docs" if _docs else None,
    redoc_url="/redoc" if _docs else None,
    openapi_url="/openapi.json" if _docs else None,
)
app.include_router(slack.router)
app.include_router(slack_install.router)
app.include_router(github.router)
app.include_router(connectors.router)
app.include_router(console_oauth.router)
app.include_router(health.router)
app.include_router(proof.router)
app.include_router(auth.router)
ops.install(app)
web.install(app)
console_web.install(app)
mcp_api.install(app)
