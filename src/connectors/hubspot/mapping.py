"""HubSpot records to the connector contract's records. Pure functions; no calls.

What comes in, and nothing else:

- an owner (an active HubSpot user who can own records) becomes an ``ExternalUser``: name and
  email. A user with no name is named by their email's local part, never the whole address.
  A deactivated user owns and works on nothing here: a company whose owner has left
  reads as having no owner, which is the gap to see;
- a company that is a customer, or has any deal, becomes a ``Service`` of kind ``account``,
  with ``OWNS`` from its company owner;
- a deal is never a node. A deal that is open, or was created or closed in the last 12 months,
  becomes ``WORKED_ON {evidence: 'deal', stage}`` from the deal's owner to its company.

Contacts, emails, calls, notes, meetings, amounts, quotes, forecasts and custom properties are
never read: the properties below are the whole list the connector asks HubSpot for.
"""

from datetime import datetime, timezone

from src.connectors.base import Edge, ExternalUser, Install, Item

COMPANY_PROPERTIES = [
    "name", "domain", "industry", "lifecyclestage", "hubspot_owner_id",
    "hs_num_open_deals", "num_associated_deals", "hs_lastmodifieddate",
]
DEAL_PROPERTIES = ["dealstage", "pipeline", "hubspot_owner_id", "hs_is_closed", "createdate", "closedate", "hs_lastmodifieddate"]

# Lifecycle stages that make a company a customer. A company is an account while it is one of
# these or has any deal; it is ``open`` while it is a customer or has an open deal, and ``closed``
# otherwise: a customer moved off these stages (churned) or a company whose deals were all lost.
CUSTOMER_STAGES = ("customer", "evangelist")
DEAL_WINDOW_DAYS = 365

_LIFECYCLE_LABELS = {
    "subscriber": "subscriber", "lead": "lead", "marketingqualifiedlead": "marketing qualified lead",
    "salesqualifiedlead": "sales qualified lead", "opportunity": "opportunity", "customer": "customer",
    "evangelist": "evangelist", "other": "other",
}
_CLOSED_STAGES = frozenset({"closedwon", "closedlost"})


def company_external_id(company_id: str | int) -> str:
    # Companies and deals share HubSpot's number space; the prefix keeps item ids apart.
    return f"company:{company_id}"


def parse_time(value) -> float | None:
    """Epoch seconds from HubSpot's ISO 8601 (``2026-09-13T20:23:03.333Z``) or epoch-millisecond strings."""
    if value is None or value == "":
        return None
    text = str(value).strip()
    if text.isdigit():
        return int(text) / 1000
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def _number(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _props(record: dict) -> dict:
    return record.get("properties") or {}


def is_customer(company: dict) -> bool:
    return (_props(company).get("lifecyclestage") or "").strip().lower() in CUSTOMER_STAGES


def in_scope(company: dict) -> bool:
    """A customer, or a company with any deal. Everything else (leads, contacts' employers) stays out."""
    return is_customer(company) or _number(_props(company).get("num_associated_deals")) > 0


def account_state(company: dict) -> str:
    """``open`` for a customer or a company with an open deal; ``closed`` for churned or lost-only."""
    return "open" if is_customer(company) or _number(_props(company).get("hs_num_open_deals")) > 0 else "closed"


def deal_counts(deal: dict, now: float, window_days: int = DEAL_WINDOW_DAYS) -> bool:
    """An open deal, or one created or closed in the window, counts as work on its company."""
    props = _props(deal)
    closed = str(props.get("hs_is_closed") or "").lower() == "true" or (props.get("dealstage") or "") in _CLOSED_STAGES
    if not closed:
        return True
    cutoff = now - window_days * 86400
    return any((when := parse_time(props.get(name))) is not None and when >= cutoff for name in ("createdate", "closedate"))


def people(pairs: list[str]) -> dict[str, str]:
    """``CONNECTOR_HUBSPOT_PEOPLE``: ``ownerId=email`` pairs an admin asserts, for owners whose HubSpot
    login is not the email the graph knows them by. A pair that is not a numeric id and an email is skipped."""
    found: dict[str, str] = {}
    for pair in pairs:
        owner_id, _, email = pair.partition("=")
        owner_id, email = owner_id.strip(), email.strip().lower()
        if owner_id.isdigit() and "@" in email:
            found[owner_id] = email
    return found


def owner_user(install: Install, owner: dict, email: str | None = None) -> ExternalUser:
    """``email``, when given, is the admin's (``people``) and replaces the HubSpot login for linking."""
    owner_id = str(owner["id"])
    email = (email or owner.get("email") or "").strip().lower()
    name = " ".join(part for part in ((owner.get("firstName") or "").strip(), (owner.get("lastName") or "").strip()) if part)
    return ExternalUser(id=install.user_id(owner_id), source=install.source, external_id=owner_id, name=name or display_name(email, owner_id), email=email)


def display_name(email: str, owner_id: str) -> str:
    """What a user with no name in HubSpot is called: their email's local part, else ``HubSpot owner <id>``.

    Never the whole address: the name is shown in answers, and the email is contact data the
    graph keeps only to link the account to a person."""
    local = (email or "").split("@", 1)[0].strip()
    return local or f"HubSpot owner {owner_id}"


def company_name(company: dict) -> str:
    props = _props(company)
    name = " ".join((props.get("name") or "").split())
    return name or (props.get("domain") or "").strip().lower() or f"HubSpot company {company['id']}"


def _label(value: str) -> str:
    return value.replace("_", " ").strip().lower()


def summary(company: dict) -> str:
    props = _props(company)
    stage = (props.get("lifecyclestage") or "").strip().lower()
    open_deals = _number(props.get("hs_num_open_deals"))
    parts = [f"HubSpot company, lifecycle stage {_LIFECYCLE_LABELS.get(stage, stage)}" if stage else "HubSpot company"]
    parts.append(f"{open_deals} open deal{'' if open_deals == 1 else 's'}")
    if props.get("industry"):
        parts.append(_label(props["industry"]))
    if props.get("domain"):
        parts.append(props["domain"].strip().lower())
    return "; ".join(parts) + "."


def record_url(ui_domain: str, portal_id: str, object_type_id: str, record_id: str) -> str:
    if not (ui_domain and portal_id):
        return ""
    return f"https://{ui_domain}/contacts/{portal_id}/record/{object_type_id}/{record_id}"


def company_item(install: Install, container_id: str, company: dict, portal_id: str = "", ui_domain: str = "") -> Item:
    props = _props(company)
    name = company_name(company)
    domain = (props.get("domain") or "").strip().lower()
    extras: dict[str, object] = {"open_deals": _number(props.get("hs_num_open_deals"))}
    if domain:
        extras["domain"] = domain
    if props.get("industry"):
        extras["industry"] = _label(props["industry"])
    external_id = company_external_id(company["id"])
    return Item(
        label="Service",
        kind="account",
        id=install.item_id(external_id),
        external_id=external_id,
        container=container_id,
        name=name,
        url=record_url(ui_domain, portal_id, "0-2", str(company["id"])),
        state=account_state(company),
        status=(props.get("lifecyclestage") or "").strip().lower(),
        updated_at=props.get("hs_lastmodifieddate") or company.get("updatedAt") or "",
        summary=summary(company),
        props=extras,
    )


def company_edges(install: Install, company: dict, deals: list[dict], stages: dict[str, str] | None = None, active=None) -> list[Edge]:
    """Every edge of one company: OWNS from its owner, and WORKED_ON from each owner of a deal that
    counts, once per owner, naming the stage of their latest deal. ``deals`` are the ones that count;
    ``active``, when given, holds the owner ids that may hold an edge (the active users)."""
    item_id = install.item_id(company_external_id(company["id"]))
    edges: list[Edge] = []
    owner = (_props(company).get("hubspot_owner_id") or "").strip()
    if owner and (active is None or owner in active):
        edges.append(Edge("OWNS", install.user_id(owner), item_id))
    latest: dict[str, dict] = {}
    for deal in deals:
        deal_owner = (_props(deal).get("hubspot_owner_id") or "").strip()
        if not deal_owner or (active is not None and deal_owner not in active):
            continue
        seen = latest.get(deal_owner)
        if seen is None or _deal_time(deal) > _deal_time(seen):
            latest[deal_owner] = deal
    for deal_owner, deal in sorted(latest.items()):
        stage = (_props(deal).get("dealstage") or "").strip()
        edges.append(Edge("WORKED_ON", install.user_id(deal_owner), item_id, {"evidence": "deal", "stage": (stages or {}).get(stage, stage)}))
    return edges


def _deal_time(deal: dict) -> float:
    props = _props(deal)
    return max(parse_time(props.get(name)) or 0.0 for name in ("createdate", "closedate", "hs_lastmodifieddate"))


def owner_ids(companies: list[dict], deals: list[dict]) -> list[str]:
    """The owners a page names, in first-seen order."""
    found: dict[str, None] = {}
    for record in [*companies, *deals]:
        owner = (_props(record).get("hubspot_owner_id") or "").strip()
        if owner:
            found.setdefault(owner, None)
    return list(found)


def scope_filters(since_ms: int | None = None) -> list[dict]:
    """Search filter groups (OR'd) for the companies that are accounts, changed since ``since_ms`` if given."""
    since = [{"propertyName": "hs_lastmodifieddate", "operator": "GTE", "value": str(since_ms)}] if since_ms is not None else []
    return [
        {"filters": [{"propertyName": "lifecyclestage", "operator": "IN", "values": list(CUSTOMER_STAGES)}, *since]},
        {"filters": [{"propertyName": "num_associated_deals", "operator": "GT", "value": "0"}, *since]},
    ]
