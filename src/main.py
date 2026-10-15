import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.v1 import health, slack, slack_install
from src.core import scheduler
from src.core.config import settings
from src.db.falkordb_client import connect
from src.domain.memory.memory_service import ensure_indexes
from src.domain.slack import team_invite  # noqa: F401  registers team invite jobs
from src.domain.slack.channel_membership import reconcile_channel_membership
from src.domain.slack.channels import observe, rollout  # noqa: F401  register rollout and observe jobs
from src.domain.slack.channels import reset as channel_reset
from src.domain.slack.turn import approval_expiry, reaction_queue  # noqa: F401  register expiry and reaction jobs
from src.domain.slack.turn import reset as turn_reset
from src.domain.slack.workspace import list_connected_workspaces
from src.seed.seed import reset_and_seed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)


def _seed_if_empty() -> None:
    if settings.seed_on_startup:
        _, graph = connect()
        nodes = graph.query("MATCH (n) RETURN count(n)").result_set[0][0]
        if nodes == 0:
            log.info("graph is empty; seeding the company (SEED_ON_STARTUP)")
            reset_and_seed()
    ensure_indexes()


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
    # Private-channel joins and leaves missed while the app was down.
    for workspace in list_connected_workspaces():
        try:
            reconcile_channel_membership(workspace.client)
        except Exception:
            log.exception("membership reconcile failed team=%s", workspace.team_id)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _seed_if_empty()
    turn_reset.register()
    channel_reset.register()
    scheduler.start()
    if settings.slack_enabled or settings.slack_oauth_enabled:
        threading.Thread(target=_recover, name="brain-recover", daemon=True).start()
    else:
        log.warning("Slack is not configured: set SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET, or the OAuth app credentials")
    yield


app = FastAPI(title="Company Brain", lifespan=lifespan)
app.include_router(slack.router)
app.include_router(slack_install.router)
app.include_router(health.router)
