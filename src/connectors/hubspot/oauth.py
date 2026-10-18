"""HubSpot over OAuth: a developer-platform app installed into one portal, and the proof it is that portal.

The token answers for one HubSpot account (portal). Which one is read from the token's metadata
(``POST /oauth/2026-09/token/introspect``, ``hub_id``), and checked against what the CRM itself
says (``account-info`` ``portalId``, the connector's ``_portal()``) at connect time and on every
credentials check, so an install can never read a portal other than the one it claimed
(``ExternalAccount 'hubspot:<hubId>'``). An install (``Install.auth == 'oauth'``) keeps ``hub_id``
in its config.
"""

import re

from src.connectors.base import ConnectorError, Install, OAuthSpec
from src.connectors.hubspot.client import BASE_URL

AUTHORIZE_URL = "https://app.hubspot.com/oauth/authorize"
OAUTH_VERSION = "2026-09"
TOKEN_URL = f"{BASE_URL}/oauth/{OAUTH_VERSION}/token"
INTROSPECT_URL = f"{BASE_URL}/oauth/{OAUTH_VERSION}/token/introspect"
SCOPES = ("crm.objects.companies.read", "crm.objects.deals.read", "crm.objects.owners.read", "oauth")
HUB_ID = re.compile(r"^[1-9][0-9]{0,19}$")
# Where the portal's records open: app.hubspot.com, or a regional app-eu1.hubspot.com.
UI_DOMAIN = re.compile(r"^app(-[a-z0-9]{1,10})?\.hubspot\.com$")

SPEC = OAuthSpec(
    authorize_url=AUTHORIZE_URL,
    token_url=TOKEN_URL,
    scopes=SCOPES,
    client_id_setting="HUBSPOT_OAUTH_CLIENT_ID",
    client_secret_setting="HUBSPOT_OAUTH_CLIENT_SECRET",
    token_body="form",
)


def is_oauth(install: Install) -> bool:
    return install.auth == "oauth"


def token_metadata(access_token: str, spec: OAuthSpec = SPEC, introspect_url: str | None = None) -> dict:
    """The access token's metadata: ``{hub_id, app_id, user_id, scopes, ...}``. Raises ``ConnectorError``
    when HubSpot says the token is not active, or names no portal, or another app's client id."""
    from src.connectors import oauth

    meta = oauth._post(
        introspect_url or INTROSPECT_URL,
        {"client_id": oauth.client_id(spec), "client_secret": oauth.client_secret(spec), "token_type_hint": "access_token", "token": access_token},
        "form",
    )
    if meta.get("active") is False:
        raise ConnectorError("HubSpot says the new token is not active")
    if meta.get("client_id") and meta.get("client_id") != oauth.client_id(spec):
        raise ConnectorError("HubSpot issued the token to another app")
    hub_id = str(meta.get("hub_id") or "")
    if not HUB_ID.fullmatch(hub_id):
        raise ConnectorError("HubSpot's token metadata named no portal")
    return {**meta, "hub_id": hub_id}


def hub_id(install: Install) -> str:
    value = install.get("hub_id")
    if not HUB_ID.fullmatch(value):
        raise ConnectorError("the HubSpot connection names no portal; reconnect it")
    return value
