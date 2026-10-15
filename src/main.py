import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.v1 import health, slack
from src.core.config import settings
from src.db.falkordb_client import connect
from src.domain.memory.memory_service import ensure_indexes
from src.domain.slack.channel_membership import reconcile_channel_membership
from src.domain.slack.workspace import bot_identity, get_workspace_by_team_id
from src.seed.seed import reset_and_seed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)


def _seed_if_empty() -> None:
    _, graph = connect()
    nodes = graph.query("MATCH (n) RETURN count(n)").result_set[0][0]
    if nodes == 0:
        log.info("graph is empty; seeding the sample company")
        reset_and_seed()
    ensure_indexes()


def _reconcile_membership() -> None:
    """Catch private-channel joins and leaves missed while the app was down."""
    identity = bot_identity()
    workspace = get_workspace_by_team_id(identity["team_id"]) if identity else None
    if workspace is not None:
        reconcile_channel_membership(workspace.client)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _seed_if_empty()
    if settings.slack_enabled:
        threading.Thread(target=_reconcile_membership, daemon=True).start()
    else:
        log.warning("Slack is not configured: set SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET")
    yield


app = FastAPI(title="Company Brain", lifespan=lifespan)
app.include_router(slack.router)
app.include_router(health.router)
