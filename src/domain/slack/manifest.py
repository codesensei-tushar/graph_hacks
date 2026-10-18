"""Port of company-brain `setup/manifest.ts`: the Slack app manifest for this deployment.

Scopes follow company-brain's bot scope list (`SLACK_SCOPES`), minus the
Slack-assistant and file scopes nothing here uses yet, plus the read scopes this
build needs for group DMs. The only user scopes are OpenID Connect's
(``openid email profile``), for signing in to the console with Slack; nothing
searches Slack on a user's behalf, and MCP is not used.
"""

SLACK_APP_NAME = "Company Brain"

# Must stay in sync with slack_app/manifest.yaml.
SLACK_BOT_SCOPES = [
    "app_mentions:read",
    "channels:history",
    "channels:join",
    "channels:manage",
    "channels:read",
    "channels:write.invites",
    "chat:write",
    "groups:history",
    "groups:read",
    "im:history",
    "im:read",
    "im:write",
    "mpim:history",
    "mpim:read",
    "reactions:read",
    "reactions:write",
    "team:read",
    "usergroups:read",
    "users:read",
    "users:read.email",
]

SLACK_BOT_EVENTS = [
    "app_mention",
    "message.channels",
    "message.groups",
    "message.im",
    "message.mpim",
    "member_joined_channel",
    "member_left_channel",
    "reaction_added",
    "reaction_removed",
    "team_join",
    "user_change",
    "channel_shared",
    "channel_unshared",
]

# Sign in with Slack (OpenID Connect) for the console. Must stay in sync with both YAML manifests.
SLACK_USER_SCOPES = ["openid", "email", "profile"]

# The OAuth `scope` parameter for /slack/oauth/install.
SLACK_SCOPES = ",".join(SLACK_BOT_SCOPES)


def oauth_callback_url(origin: str) -> str:
    return f"{origin.rstrip('/')}/slack/oauth/callback"


def signin_callback_url(origin: str) -> str:
    """Where Slack returns after Sign in with Slack (src/api/v1/auth.py)."""
    return f"{origin.rstrip('/')}/auth/slack/callback"


def slack_app_manifest(origin: str, app_name: str = SLACK_APP_NAME) -> dict:
    origin = origin.rstrip("/")
    return {
        "display_information": {
            "name": app_name,
            "description": "A company brain on a knowledge graph that lives in your Slack.",
            "background_color": "#000b36",
        },
        "features": {
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": False,
            },
            "bot_user": {"display_name": "company-brain", "always_online": True},
        },
        "oauth_config": {
            "redirect_urls": [oauth_callback_url(origin), signin_callback_url(origin)],
            "scopes": {"bot": list(SLACK_BOT_SCOPES), "user": list(SLACK_USER_SCOPES)},
            "pkce_enabled": False,
        },
        "settings": {
            "event_subscriptions": {"request_url": f"{origin}/slack/events", "bot_events": list(SLACK_BOT_EVENTS)},
            "interactivity": {"is_enabled": True, "request_url": f"{origin}/slack/interactions"},
            "org_deploy_enabled": False,
            "socket_mode_enabled": False,
            "token_rotation_enabled": False,
        },
    }
