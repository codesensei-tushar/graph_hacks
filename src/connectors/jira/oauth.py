"""Jira Cloud over Atlassian OAuth 2.0 (3LO): the app's ``OAuthSpec`` and the site it reads.

An OAuth install calls ``https://api.atlassian.com/ex/jira/<cloudId>/rest/api/3/...`` with a
bearer token from ``oauth.bearer_for``. One consent can cover several Jira sites; Atlassian's
``accessible-resources`` lists them, and the install reads exactly one, its ``cloud_id``.

The site's own URL (``site_url``, ``https://<name>.atlassian.net``) is used for links in answers
only, never to send a request, and must match ``SITE_URL``: a resource whose URL does not is not
offered at all. An install (``Install.auth == 'oauth'``) keeps ``cloud_id`` and ``site_url`` in its config.
"""

import re
from dataclasses import dataclass

from src.connectors.base import ConnectorError, Install, OAuthSpec
from src.connectors.http import JsonClient, bearer

AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"
API_ROOT = "https://api.atlassian.com"
# Classic scopes. offline_access is what makes Atlassian issue a refresh token.
SCOPES = ("read:jira-work", "read:jira-user", "offline_access")
# A resource is a Jira site only if its grant includes this scope (an id may also be a Confluence site).
JIRA_SCOPE = "read:jira-work"
SITE_URL = re.compile(r"^https://[a-z0-9][a-z0-9-]{0,62}\.atlassian\.net$")
# Atlassian cloud ids are UUIDs; this is looser but keeps the id one safe path segment.
CLOUD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")

SPEC = OAuthSpec(
    authorize_url=AUTHORIZE_URL,
    token_url=TOKEN_URL,
    scopes=SCOPES,
    client_id_setting="JIRA_OAUTH_CLIENT_ID",
    client_secret_setting="JIRA_OAUTH_CLIENT_SECRET",
    extra_params={"audience": "api.atlassian.com", "prompt": "consent"},
    rotating_refresh=True,
    token_body="json",
)


@dataclass(frozen=True)
class Site:
    cloud_id: str
    name: str
    url: str

    @property
    def external_account(self) -> str:
        return f"jira:{self.cloud_id}"


def is_oauth(install: Install) -> bool:
    return install.auth == "oauth"


def api_base(cloud_id: str) -> str:
    if not CLOUD_ID.fullmatch(cloud_id or ""):
        raise ConnectorError("the Jira connection has no valid site; choose one in the console")
    return f"{API_ROOT}/ex/jira/{cloud_id}"


def site_url(install: Install) -> str:
    """The site's URL for links. Refused unless it is an atlassian.net origin."""
    url = install.get("site_url").lower().rstrip("/")
    if not SITE_URL.fullmatch(url):
        raise ConnectorError("the Jira connection's site URL is not an atlassian.net site")
    return url


def accessible_resources(access_token: str) -> list[Site]:
    """The Jira sites this token may read, as Atlassian lists them, minus anything that is not one."""
    raw = JsonClient(API_ROOT, headers=bearer(access_token)).get("/oauth/token/accessible-resources") or []
    sites, seen = [], set()
    for resource in raw if isinstance(raw, list) else []:
        if not isinstance(resource, dict):
            continue
        cloud_id = str(resource.get("id") or "")
        url = str(resource.get("url") or "").lower().rstrip("/")
        scopes = resource.get("scopes") or []
        if JIRA_SCOPE not in scopes or not CLOUD_ID.fullmatch(cloud_id) or not SITE_URL.fullmatch(url) or cloud_id in seen:
            continue
        seen.add(cloud_id)
        sites.append(Site(cloud_id=cloud_id, name=str(resource.get("name") or url.removeprefix("https://"))[:200], url=url))
    return sites


def sites_for(install: Install) -> list[Site]:
    """The sites an OAuth install's grant covers (``GET /console/api/connections/{id}/sites``)."""
    from src.connectors.oauth import bearer_for

    return accessible_resources(bearer_for(install, SPEC))


def find_site(install: Install, cloud_id: str) -> Site | None:
    """The site ``cloud_id`` names, if the grant covers it (``POST /console/api/connections/{id}/site``)."""
    return next((site for site in sites_for(install) if site.cloud_id == cloud_id), None)


def choose_site(org: str, install_key: str, cloud_id: str) -> str:
    """Point a Jira OAuth install whose grant covers several sites at one of them
    (``POST /console/api/connections/{id}/site``). Claims ``jira:<cloudId>`` for the org.

    Returns the install id to continue with (``/console/connectors/<id>/projects``): this one, or,
    when the org already reads that site, its existing install, which takes these tokens while
    this one is deleted. Raises ``LookupError`` for a site the grant does not cover, and
    ``PermissionError`` when another org has claimed it or the org's install of it is being removed.
    """
    from src.connectors import oauth, store
    from src.core.tenancy import org_context

    with org_context(org):
        stored = store.get(install_key)
        if stored is None or stored.source != "jira" or stored.auth != "oauth":
            raise LookupError("no such Jira OAuth install")
        site = find_site(store.to_install(stored), cloud_id)
        if site is None:
            raise LookupError("the grant does not cover that site")
        with oauth.account_lock(site.external_account):
            return _claim_site(org, stored, site)


def _claim_site(org: str, stored, site: Site) -> str:
    """``choose_site`` under the account's lock (the same one the OAuth callback takes)."""
    from src.connectors import oauth, registry, store

    if not store.claim_external_account(site.external_account, org):
        raise PermissionError("another org reads that site")
    config = {"cloud_id": site.cloud_id, "site_url": site.url}
    current = next((row for row in store.list_installs("jira") if row.external_account == site.external_account and row.id != stored.id), None)
    if current is not None and current.status == "removing":
        raise PermissionError("that site's connection is being disconnected; try again when it is gone")
    if current is not None:
        store.update_config(current.id, config, site=site.url)
        store.set_secret(current.id, store.load_secrets(stored))
        if current.status == "error":
            store.set_status(current.id, "active")
        store.delete(stored.id)
        registry.drop(org, "jira", stored.install)
        oauth.forget(store.to_install(store.get(current.id)))
        return current.id
    store.update_config(stored.id, config, site=site.url, external_account=site.external_account)
    return stored.id
