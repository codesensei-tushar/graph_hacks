"""HubSpot on the connector contract: one portal, its accounts, and who owns and works them.

One code path for the backfill and every poll: the CRM search API, filtered and sorted on
``hs_lastmodifieddate``. The cursor is a small JSON object:

- ``{"mode": "backfill", "after": ..., "start": ms, "count": n}``: accounts newest first,
  100 a page, until ``MAX_COMPANIES``; then the first poll starts from ``start``;
- ``{"mode": "poll", "phase": "companies"|"deals", "since": ms, "after": ..., "next": ms, "owners": [...]}``:
  accounts changed since ``since``, then deals changed since ``since`` (a new deal or a stage
  or owner change), each re-sent as its company with every edge it has. A cycle's ``next``
  is HubSpot's clock when the cycle began, less ``OVERLAP_MS``, because a change takes a
  few moments to reach search. ``window`` is where a long query started again, since search
  stops at 10,000 results. ``owners`` are the active owners when the cycle began: one gone
  by the next cycle has the accounts they held re-sent without them.

Writing a company replaces its edges, so a page always carries a company with all of them:
OWNS from its owner and WORKED_ON from each owner of a deal that counts (``mapping.py``).

An install connected from the console (``auth='oauth'``) sends a bearer token from
``oauth.bearer_for`` instead of a private app's token, and its credentials check fails unless
the portal the CRM answers for is the ``hub_id`` the install claimed (``hubspot/oauth.py``).
"""

import email.utils
import json
import logging
import time

from src.connectors.base import Connector, ConnectorError, Container, Install, InstallInfo, OAuthSpec, Page
from src.connectors.hubspot import mapping
from src.connectors.hubspot import oauth as hubspot_oauth
from src.connectors.hubspot.client import API_VERSION, BATCH, HubSpotClient
from src.connectors.http import HttpError
from src.connectors.oauth import OAUTH_SECRET_FIELDS, bearer_for

log = logging.getLogger(__name__)

# Accounts kept per portal, newest first.
MAX_COMPANIES = 500
PAGE_SIZE = BATCH
OVERLAP_MS = 2 * 60 * 1000
# HubSpot search returns at most this many results for one query.
SEARCH_LIMIT = 10_000
# How long the owners list and the deal stage labels are reused.
DIRECTORY_SECONDS = 600


class HubSpotConnector(Connector):
    source = "hubspot"
    required = ("token",)
    poll_seconds = 300
    accepts_webhooks = False
    oauth: OAuthSpec = hubspot_oauth.SPEC
    # The private app's token (token installs) and the OAuth tokens: kept encrypted, never in config_json.
    secret_fields = ("token", *OAUTH_SECRET_FIELDS)

    def __init__(self, client_for=None, clock=time.time):
        self._client_for = client_for or _client_for
        self._clock = clock
        # install.key -> what the portal is, its owners and stage labels, and when those expire.
        self._portals: dict[tuple[str, str], tuple[str, str]] = {}
        self._directories: dict[str, tuple[float, dict[str, dict], dict[str, str]]] = {}

    # --- the contract -----------------------------------------------------------------------

    def check_credentials(self, install: Install) -> InstallInfo:
        portal, ui_domain = self._portal(install, refresh=True)
        if hubspot_oauth.is_oauth(install) and portal != hubspot_oauth.hub_id(install):
            # The token answers for another portal than the one this install claimed: read nothing.
            self._portals.pop(install.key, None)
            raise ConnectorError("the HubSpot token answers for a different portal than this connection; reconnect it")
        return InstallInfo(account=f"HubSpot portal {portal}", site=f"https://{ui_domain}")

    def containers(self, install: Install) -> list[Container]:
        portal, ui_domain = self._portal(install)
        return [
            Container(
                id=install.container_id(portal), name=f"HubSpot portal {portal}", external_id=portal,
                url=f"https://{ui_domain}/contacts/{portal}/companies", kind="portal", available=self._company_total(install),
            )
        ]

    def _company_total(self, install: Install) -> int | None:
        """The portal's companies in scope, as HubSpot counts them: one search asking for one record, made
        when the containers are listed (hourly). A failure is no count, never a failed listing."""
        try:
            return self._client_for(install).total("companies", mapping.scope_filters())
        except Exception as error:
            log.info("[hubspot] company count unavailable for %s: %s", install.key, type(error).__name__)
            return None

    def fetch(self, install: Install, container: Container, cursor: str | None) -> Page:
        state = _decode(cursor)
        client = self._client_for(install)
        if state.get("mode") == "poll":
            if state.get("phase") == "deals":
                return self._poll_deals(install, container, client, state)
            return self._poll_companies(install, container, client, state)
        return self._backfill(install, container, client, state)

    # --- the three kinds of page ------------------------------------------------------------

    def _backfill(self, install: Install, container: Container, client: HubSpotClient, state: dict) -> Page:
        body = _search_body(mapping.scope_filters(), "DESCENDING", state.get("after"))
        companies, after, headers = client.search("companies", body)
        start = state.get("start") or self._server_ms(headers)
        companies = [company for company in companies if mapping.in_scope(company)][: MAX_COMPANIES - state.get("count", 0)]
        count = state.get("count", 0) + len(companies)
        page = self._page(install, container, client, companies)
        owners, _ = self._directory(install, client)
        # Polling resumes from when the backfill began, wherever the framework ends it.
        page.poll_cursor = _encode(mode="poll", phase="companies", since=start - OVERLAP_MS, owners=sorted(owners))
        if after and count < MAX_COMPANIES:
            page.next_cursor, page.has_more = _encode(mode="backfill", after=after, start=start, count=count), True
        else:
            page.next_cursor = page.poll_cursor
        return page

    def _poll_companies(self, install: Install, container: Container, client: HubSpotClient, state: dict) -> Page:
        since = int(state["since"])
        window = int(state.get("window") or since)
        companies, after, headers = client.search("companies", _search_body(mapping.scope_filters(window), "ASCENDING", state.get("after")))
        upcoming = state.get("next") or self._server_ms(headers) - OVERLAP_MS
        changed = [company for company in companies if mapping.in_scope(company)]
        owners = state.get("owners")
        cycle_start = not state.get("after") and state.get("next") is None
        if cycle_start:
            active, _ = self._directory(install, client)
            if owners is not None:
                # Deactivating a user changes none of their records, so their accounts are asked for here.
                changed += self._held_by(client, sorted(set(owners) - set(active)))
            owners = sorted(active)
        page = self._page(install, container, client, list({str(company["id"]): company for company in changed}.values()))
        if cycle_start:
            # Every active owner once a cycle, so one whose Person arrives later is linked within a poll.
            active, _ = self._directory(install, client)
            sent = {user.external_id for user in page.users}
            page.users += self._users(install, active, [owner for owner in active if owner not in sent])
        if after:
            window, after = _window(window, after, companies)
            page.next_cursor = _encode(mode="poll", phase="companies", since=since, window=window, after=after, next=upcoming, owners=owners)
        else:
            page.next_cursor = _encode(mode="poll", phase="deals", since=since, next=upcoming, owners=owners)
        page.has_more = True
        return page

    def _poll_deals(self, install: Install, container: Container, client: HubSpotClient, state: dict) -> Page:
        since = int(state["since"])
        window = int(state.get("window") or since)
        changed = [{"filters": [{"propertyName": "hs_lastmodifieddate", "operator": "GTE", "value": str(window)}]}]
        body = _search_body(changed, "ASCENDING", state.get("after"), properties=["hs_lastmodifieddate"])
        deals, after, _ = client.search("deals", body)
        page = self._page(install, container, client, self._companies_of(client, deals))
        if after:
            window, after = _window(window, after, deals)
            page.next_cursor = _encode(mode="poll", phase="deals", since=since, window=window, after=after, next=state.get("next"), owners=state.get("owners"))
            page.has_more = True
        else:
            page.next_cursor = _encode(mode="poll", phase="companies", since=max(since, int(state.get("next") or since)), owners=state.get("owners"))
        return page

    def _companies_of(self, client: HubSpotClient, deals: list[dict]) -> list[dict]:
        """The accounts these deals belong to."""
        linked = client.associations("deals", "companies", [str(deal["id"]) for deal in deals]) if deals else {}
        company_ids = list(dict.fromkeys(company for deal in deals for company in linked.get(str(deal["id"]), [])))
        companies = client.batch_read("companies", company_ids, mapping.COMPANY_PROPERTIES) if company_ids else []
        return [company for company in companies if mapping.in_scope(company)]

    def _held_by(self, client: HubSpotClient, owner_ids: list[str]) -> list[dict]:
        """Accounts owned by these owners, or with a deal they own: up to a page of each, re-sent without them."""
        if not owner_ids:
            return []
        owned = [{"filters": [{"propertyName": "hubspot_owner_id", "operator": "IN", "values": owner_ids}]}]
        companies, _, _ = client.search("companies", _search_body(owned, "DESCENDING", None))
        deals, _, _ = client.search("deals", _search_body(owned, "DESCENDING", None, properties=["hs_lastmodifieddate"]))
        return [company for company in companies if mapping.in_scope(company)] + self._companies_of(client, deals)

    def _page(self, install: Install, container: Container, client: HubSpotClient, companies: list[dict]) -> Page:
        """The companies as accounts, each with all its edges, and the owners those edges name."""
        if not companies:
            return Page()
        company_ids = [str(company["id"]) for company in companies]
        linked = client.associations("companies", "deals", company_ids)
        deal_ids = list(dict.fromkeys(deal for company in company_ids for deal in linked.get(company, [])))
        now = self._clock()
        counted = {str(deal["id"]): deal for deal in (client.batch_read("deals", deal_ids, mapping.DEAL_PROPERTIES) if deal_ids else []) if mapping.deal_counts(deal, now)}
        owners, stages = self._directory(install, client)
        portal, ui_domain = self._portal(install)
        page = Page()
        for company in companies:
            page.items.append(mapping.company_item(install, container.id, company, portal, ui_domain))
            deals = [counted[deal] for deal in linked.get(str(company["id"]), []) if deal in counted]
            page.edges.extend(mapping.company_edges(install, company, deals, stages, active=owners))
        page.users = self._users(install, owners, mapping.owner_ids(companies, list(counted.values())))
        return page

    def _users(self, install: Install, owners: dict[str, dict], owner_ids: list[str]) -> list:
        """The active owners among ``owner_ids``, with the email an admin set for them (``people``) if any."""
        emails = mapping.people(install.get_list("people"))
        return [mapping.owner_user(install, owners[owner], emails.get(owner)) for owner in owner_ids if owner in owners]

    # --- what is reused between pages --------------------------------------------------------

    def _portal(self, install: Install, refresh: bool = False) -> tuple[str, str]:
        if refresh or (install.org, install.key) not in self._portals:
            details = self._client_for(install).account_details()
            portal = str(details.get("portalId") or "")
            if not portal:
                raise ConnectorError("HubSpot's account details named no portal")
            self._portals[(install.org, install.key)] = (portal, details.get("uiDomain") or "app.hubspot.com")
        return self._portals[(install.org, install.key)]

    def _directory(self, install: Install, client: HubSpotClient) -> tuple[dict[str, dict], dict[str, str]]:
        cached = self._directories.get(install.key)
        if cached and cached[0] > self._clock():
            return cached[1], cached[2]
        # Active users only (mapping.py): someone who has left is nobody's owner or successor.
        owners = {str(owner["id"]): owner for owner in client.owners()}
        try:
            stages = client.deal_stages()
        except HttpError:
            stages = {}  # the raw stage ids, "closedwon", still read
        self._directories[install.key] = (self._clock() + DIRECTORY_SECONDS, owners, stages)
        return owners, stages

    def _server_ms(self, headers: dict) -> int:
        """HubSpot's clock from the response's Date header, so a poll window never depends on ours."""
        try:
            return int(email.utils.parsedate_to_datetime(headers["date"]).timestamp() * 1000)
        except (KeyError, TypeError, ValueError):
            return int(self._clock() * 1000)


def _client_for(install: Install) -> HubSpotClient:
    token = bearer_for(install, hubspot_oauth.SPEC) if hubspot_oauth.is_oauth(install) else install.get("token")
    return HubSpotClient(token, install.get("api_version", API_VERSION))


def _window(since: int, after: str | None, records: list[dict]) -> tuple[int, str | None]:
    """The next page's ``since`` and ``after``. HubSpot answers 400 past 10,000 results of one query, so
    before that the query starts again from the last record's time (GTE, so nothing is skipped)."""
    if after and int(after) + PAGE_SIZE > SEARCH_LIMIT and records:
        last = mapping.parse_time((records[-1].get("properties") or {}).get("hs_lastmodifieddate"))
        if last is not None and int(last * 1000) > since:
            return int(last * 1000), None
    return since, after


def _search_body(filter_groups: list[dict], direction: str, after: str | None, properties: list[str] | None = None) -> dict:
    body = {
        "filterGroups": filter_groups,
        "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": direction}],
        "properties": properties or mapping.COMPANY_PROPERTIES,
        "limit": PAGE_SIZE,
    }
    if after:
        body["after"] = after
    return body


def _encode(**state) -> str:
    return json.dumps({key: value for key, value in state.items() if value is not None}, separators=(",", ":"), sort_keys=True)


def _decode(cursor: str | None) -> dict:
    if not cursor:
        return {}
    try:
        state = json.loads(cursor)
    except ValueError:
        return {}  # an unreadable cursor starts over
    return state if isinstance(state, dict) else {}
