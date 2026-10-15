"""After-install steps for a single-workspace (``SLACK_BOT_TOKEN``) install.

An app installed from the manifest never hits the OAuth callback, so the steps
``slack_install.oauth_callback`` runs after an install would never happen: no
#company-brain home channel, no rollout card, no beachhead. This runs the same
steps once per workspace on startup, with ``SLACK_OWNER_ID`` as the installer.
"""

import logging

from src.core.config import settings
from src.db.falkordb_client import kv
from src.domain.slack.bootstrap import bootstrap_slack_workspace
from src.domain.slack.install_greeting import greet_slack_installer
from src.domain.slack.workspace import get_home_channel, get_workspace_by_team_id, register_workspace_reset_hook

log = logging.getLogger(__name__)


def _bootstrapped_key(team_id: str) -> str:
    return f"brain:bootstrap:env:{team_id}"


def ensure_token_workspace_bootstrapped(workspace) -> bool:
    """True when this call ran the bootstrap. A no-op for OAuth installs and after the first success."""
    if getattr(workspace, "source", None) != "env":
        return False
    team_id = workspace.team_id
    if kv().get(_bootstrapped_key(team_id)):
        return False
    if get_home_channel(team_id):
        # Bootstrapped before this marker existed, or the home channel was set up by hand.
        kv().set(_bootstrapped_key(team_id), "1")
        return False

    installer = settings.slack_owner_id or None
    if not installer:
        log.warning("[slack] SLACK_OWNER_ID is not set: bootstrapping without an installer, so no admin rollout card")
    home = bootstrap_slack_workspace(workspace=workspace, slack_user_id=installer)
    if not home:
        # Leave the marker unset so the next start tries again.
        log.warning("[slack] token-mode bootstrap did not set up a home channel team=%s", team_id)
        return False
    kv().set(_bootstrapped_key(team_id), "1")

    if installer:
        # Reload so the greeting sees the home channel bootstrap just set.
        workspace = get_workspace_by_team_id(team_id) or workspace
        greet_slack_installer(workspace=workspace, slack_user_id=installer, team_name=workspace.team_name)
    log.info("[slack] token-mode bootstrap done team=%s home=%s", team_id, home["channel_id"])
    return True


def _forget_bootstrap(team_id: str) -> None:
    kv().delete(_bootstrapped_key(team_id))


def register() -> None:
    """A disconnect clears the marker, so a reinstall bootstraps again."""
    register_workspace_reset_hook(_forget_bootstrap)
