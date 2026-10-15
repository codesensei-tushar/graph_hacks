"""Port of company-brain `brain/slack/bootstrap.ts`.

After an install: record the workspace's company domain, create or adopt the
#company-brain home channel, invite the installer, and hand off to the
public-channel rollout card when that module exists. Best-effort: never raises,
so a failure can't break the OAuth callback.

Company research on signup and the onboarding "journey" beats are not ported
(research is an agent feature; journeys are hosted-product nudges).
"""

import logging

from src.db.falkordb_client import connect
from src.domain.slack.company_domain import company_domain
from src.domain.slack.farewell import HOME_CHANNEL_NAME
from src.domain.slack.home_welcome import COMPANY_BRAIN_HOME_WELCOME_VERSION, company_brain_home_welcome_messages
from src.domain.slack.lifecycle_api import lookup_slack_user_info, slack_call
from src.domain.slack.workspace import set_home_channel

log = logging.getLogger(__name__)

# 25k channels: enough for any real workspace, still bounded.
CHANNEL_PAGE_SIZE = 1000
MAX_CHANNEL_PAGES = 25


def get_team_email_domain(web) -> str | None:
    """team.info email_domain can be a comma-separated list; take the first real one."""
    data = slack_call(web, "team.info")
    if data.get("ok") is not True:
        return None
    for part in ((data.get("team") or {}).get("email_domain") or "").split(","):
        domain = company_domain(part)
        if domain:
            return domain
    return None


def get_installer_email_domain(email: str | None) -> str | None:
    at = (email or "").rfind("@")
    if at < 0:
        return None
    return company_domain(email[at + 1 :])


def find_channel_by_name(web, name: str) -> str | None:
    cursor = None
    for _page in range(MAX_CHANNEL_PAGES):
        body: dict = {"types": "public_channel", "exclude_archived": True, "limit": CHANNEL_PAGE_SIZE}
        if cursor:
            body["cursor"] = cursor
        data = slack_call(web, "conversations.list", **body)
        if data.get("ok") is not True:
            log.warning("[slack] conversations.list failed: %s", data.get("error"))
            return None
        hit = next((c for c in data.get("channels") or [] if c.get("name") == name), None)
        if hit:
            return hit["id"]
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return None
    log.warning("[slack] gave up looking for #%s after %d pages", name, MAX_CHANNEL_PAGES)
    return None


def invite_to_home_channel(web, channel: str, slack_user_id: str) -> None:
    """already_in_channel / cant_invite_self are expected on reinstall."""
    res = slack_call(web, "conversations.invite", channel=channel, users=slack_user_id)
    if res.get("ok") is not True and res.get("error") not in ("already_in_channel", "cant_invite_self"):
        log.warning("[slack] conversations.invite failed: %s", res.get("error"))


def ensure_home_channel(web) -> dict | None:
    """``{"channel_id", "created"}`` or None."""
    created = slack_call(web, "conversations.create", name=HOME_CHANNEL_NAME)
    if created.get("ok") is True:
        channel_id = (created.get("channel") or {}).get("id")
        return {"channel_id": channel_id, "created": True} if channel_id else None
    if created.get("error") != "name_taken":
        log.warning("[slack] conversations.create failed: %s", created.get("error"))
        return None
    existing = find_channel_by_name(web, HOME_CHANNEL_NAME)
    if not existing:
        return None
    slack_call(web, "conversations.join", channel=existing)
    return {"channel_id": existing, "created": False}


def _set_workspace_metadata(team_id: str, domain: str | None, team_name: str | None) -> None:
    _, graph = connect()
    graph.query(
        """
        MERGE (w:SlackWorkspace {team_id: $team})
        SET w.company_domain = coalesce(w.company_domain, $domain),
            w.team_name = coalesce(w.team_name, $name)
        """,
        {"team": team_id, "domain": domain, "name": team_name},
    )


def _stored_domain(team_id: str) -> str | None:
    _, graph = connect()
    rows = graph.query("MATCH (w:SlackWorkspace {team_id: $team}) RETURN w.company_domain", {"team": team_id}).result_set
    return company_domain(rows[0][0]) if rows and rows[0][0] else None


def bootstrap_slack_workspace(*, workspace, slack_user_id: str | None) -> dict | None:
    """Returns the home channel ``{"channel_id", "created"}`` when one was set up."""
    try:
        web = workspace.client.web
        installer = lookup_slack_user_info(web, slack_user_id).user if slack_user_id else None

        domain = _stored_domain(workspace.team_id)
        if not domain:
            domain = get_team_email_domain(web) or get_installer_email_domain(installer.email if installer else None)
        _set_workspace_metadata(workspace.team_id, domain, workspace.team_name)

        home = ensure_home_channel(web)
        log.info(
            "[slack] bootstrap team=%s domain=%s home=%s created=%s",
            workspace.team_id, domain or "?", home["channel_id"] if home else "none", home["created"] if home else False,
        )
        if not home:
            return None
        set_home_channel(workspace.team_id, home["channel_id"])
        # The installer asked for this; everyone else joins on their own.
        if slack_user_id:
            invite_to_home_channel(web, home["channel_id"], slack_user_id)
        admin_name = (installer.display_name or installer.name or installer.handle) if installer else None
        welcome = {
            "version": COMPANY_BRAIN_HOME_WELCOME_VERSION,
            "messages": company_brain_home_welcome_messages(admin_name),
        }
        if not _call_rollout_hook(
            "ensure_public_channel_rollout_card",
            team_id=workspace.team_id,
            home_channel_id=home["channel_id"],
            installer_slack_user_id=slack_user_id,
            welcome=welcome,
        ):
            # Without the rollout module, post the welcome ourselves (it normally rides on the rollout card).
            post_home_welcome(web, home["channel_id"], welcome["messages"], workspace.team_id)
        _call_rollout_hook("arm_public_channel_beachhead", team_id=workspace.team_id, installer_slack_user_id=slack_user_id)
        return home
    except Exception:
        log.warning("[slack] workspace bootstrap failed", exc_info=True)
        return None


def post_home_welcome(web, channel_id: str, messages: list[str], team_id: str) -> None:
    """Exactly-once welcome via a deterministic client_msg_id per version."""
    from src.domain.slack.lifecycle_api import deterministic_uuid, post_slack_message_idempotent

    for index, message in enumerate(messages):
        client_msg_id = deterministic_uuid(f"cb-home-welcome-v{COMPANY_BRAIN_HOME_WELCOME_VERSION}-{index}:{team_id}:{channel_id}")
        post_slack_message_idempotent(web, channel_id, message, client_msg_id)


def _call_rollout_hook(name: str, **kwargs) -> bool:
    """Public-channel rollout lives in another module; call it when present."""
    try:
        from src.domain.slack.channels import rollout as public_channel_rollout
    except ImportError:
        return False
    hook = getattr(public_channel_rollout, name, None)
    if hook is None:
        return False
    try:
        # Rollout hooks take one payload dict, like their scheduler jobs.
        hook(kwargs)
    except Exception:
        log.warning("[slack] %s failed", name, exc_info=True)
    return True
