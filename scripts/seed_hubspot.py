"""Create Quillfern's synthetic customers and deals in a HubSpot account, for the demo.

Dry run by default: it reads the account (its users, the deal pipeline, what it created
before) and prints what it would create. ``--apply`` creates it. Without
``CONNECTOR_HUBSPOT_TOKEN`` it prints the plan alone.

It only ever creates. It never updates or deletes a record, its own included, and it touches
nothing it did not make: every company it makes has a ``.example`` domain (reserved by RFC
2606, so no real company has one) and a description that starts with ``MARKER``, and every
deal carries the marker and is associated with one of those companies. A company whose
domain is already in the account is skipped, with its deals, unless it carries the marker;
a marked company or deal that is already there is left as it is. So a second run creates
nothing. No contacts, no amounts: every company and deal here is invented.

The records are split into three books of business (renewals, new business, operations),
and each book goes to one of the account's active users, in turn: with one user, that user
owns everything. Who those users are in the graph is the connector's business (docs/HUBSPOT.md,
"Identity").

    set -a; . ~/.config/company-brain/connectors.env; set +a
    .venv/bin/python scripts/seed_hubspot.py            # the plan
    .venv/bin/python scripts/seed_hubspot.py --apply    # create it

Needs the connector's read scopes plus ``crm.objects.companies.write`` and ``crm.objects.deals.write``.
"""

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.connectors.base import ConnectorError  # noqa: E402
from src.connectors.hubspot.client import API_VERSION, BATCH, HubSpotClient  # noqa: E402

MARKER = "[company-brain-seed] Synthetic demo record created by Company Brain's scripts/seed_hubspot.py; not a real company."
PIPELINE = "default"
STAGES = ("appointmentscheduled", "qualifiedtobuy", "presentationscheduled", "decisionmakerboughtin", "contractsent", "closedwon", "closedlost")
# HubSpot-defined association type: a deal to its primary company.
DEAL_TO_PRIMARY_COMPANY = 5
# Handed to the account's active users in this order, round robin.
BOOKS = ("renewals", "new business", "operations")


@dataclass(frozen=True)
class Deal:
    name: str
    stage: str
    book: str
    close_in_days: int  # from today; negative for a deal already closed


@dataclass(frozen=True)
class Company:
    name: str
    lifecycle: str  # customer, opportunity, salesqualifiedlead, or other (a customer who left)
    book: str
    deals: tuple[Deal, ...] = ()

    @property
    def domain(self) -> str:
        return self.name.lower().replace(" ", "-") + ".example"


RENEW, NEW, OPS = BOOKS


def _renewal(stage: str, book: str, days: int) -> Deal:
    return Deal("Annual renewal 2026", stage, book, days)


# Every name is invented; none names a Quillfern service, team or person.
PLAN = (
    Company("Larkspur Freightways", "customer", RENEW, (_renewal("closedwon", RENEW, -230), Deal("Fleet tier upgrade", "contractsent", NEW, 33))),
    Company("Saltwick Outfitters", "other", RENEW, (_renewal("closedlost", RENEW, -65),)),
    Company("Glimmerwick Studios", "customer", RENEW, (_renewal("closedwon", RENEW, -160),)),
    Company("Hollowpine Health", "customer", RENEW, (_renewal("closedwon", OPS, -240), Deal("Expansion to six clinics", "decisionmakerboughtin", RENEW, 48))),
    Company("Marrowfield Mutual", "customer", RENEW, (_renewal("contractsent", RENEW, 14),)),
    Company("Wrenhallow Manufacturing", "customer", RENEW, (_renewal("closedwon", RENEW, -110),)),
    Company("Cinderbay Freight", "customer", RENEW, (_renewal("appointmentscheduled", RENEW, 89),)),
    Company("Thistlecombe Travel", "other", RENEW, (_renewal("closedlost", RENEW, -198),)),
    Company("Pebblewade Schools", "customer", RENEW, (_renewal("closedwon", RENEW, -47), Deal("Seat expansion", "qualifiedtobuy", RENEW, 61))),
    Company("Velvetfield Media", "customer", RENEW, (Deal("Monthly to annual switch", "closedwon", RENEW, -28),)),
    Company("Tidewhistle Robotics", "opportunity", NEW, (Deal("New business: platform tier", "qualifiedtobuy", NEW, 58),)),
    Company("Ashbourne Gridworks", "customer", NEW, (Deal("Pilot", "closedwon", RENEW, -109), Deal("Enterprise rollout", "presentationscheduled", NEW, 105))),
    Company("Kestrelmere Farms", "customer", NEW, (Deal("Annual plan", "closedwon", NEW, -38),)),
    Company("Otterburn Biotech", "salesqualifiedlead", NEW, (Deal("New business", "closedlost", NEW, -128),)),
    Company("Silverbirch Clinics", "opportunity", NEW, (Deal("New business", "contractsent", NEW, 23),)),
    Company("Juniperlode Retail", "customer", NEW, (_renewal("closedwon", NEW, -179),)),
    Company("Quarrystone Legal", "opportunity", NEW, (Deal("New business", "closedlost", NEW, -169), Deal("Second attempt", "qualifiedtobuy", NEW, 75))),
    Company("Bramblecote Analytics", "customer", OPS, (_renewal("contractsent", OPS, 13), Deal("Reporting add-on", "closedwon", RENEW, -271))),
    Company("Fernhollow Bakehouse", "customer", OPS, (Deal("Monthly to annual switch", "closedwon", OPS, -33),)),
    Company("Mossgarth Hotels", "opportunity", OPS, (Deal("New business", "appointmentscheduled", OPS, 96),)),
)


def deal_name(company: Company, deal: Deal) -> str:
    return f"{company.name}: {deal.name}"


def assign_books(owners: list[dict]) -> dict[str, dict]:
    """Each book of business to one of the account's active users, in turn."""
    people = sorted((owner for owner in owners if owner.get("type", "PERSON") == "PERSON" and not owner.get("archived")), key=lambda owner: int(owner["id"]))
    return {book: people[index % len(people)] for index, book in enumerate(BOOKS)} if people else {}


def owner_name(owner: dict) -> str:
    return " ".join(part for part in (owner.get("firstName"), owner.get("lastName")) if part) or owner.get("email") or f"owner {owner['id']}"


def _list(client: HubSpotClient, object_type: str, properties: list[str]) -> list[dict]:
    """Every record of a type (not search: a record made a moment ago is in the list at once)."""
    records, after = [], None
    while True:
        params = {"limit": BATCH, "properties": ",".join(properties), "archived": "false"}
        if after:
            params["after"] = after
        data = client.http.get(client.path("objects", object_type), params) or {}
        records.extend(data.get("results") or [])
        after = ((data.get("paging") or {}).get("next") or {}).get("after")
        if not after:
            return records


def _date(days: int) -> str:
    when = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=days)
    return when.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _is_ours(record: dict) -> bool:
    return ((record.get("properties") or {}).get("description") or "").startswith(MARKER)


def run(client: HubSpotClient | None, apply: bool, out=print) -> dict:
    counts = {"companies": 0, "deals": 0, "skipped": 0, "existing": 0}
    if client is None:
        out("No CONNECTOR_HUBSPOT_TOKEN: the plan only, without the account's users or what is already there.")
        books, existing_companies, existing_deals = {}, {}, set()
    else:
        stages = client.deal_stages()
        missing = [stage for stage in STAGES if stage not in stages]
        if missing:
            raise SystemExit(f"the default deal pipeline lacks stage ids {', '.join(missing)}; nothing created")
        books = assign_books(client.owners())
        for book in BOOKS:
            owner = books.get(book)
            out(f"owner  {book}: " + (f"HubSpot user {owner['id']}, {owner_name(owner)} <{(owner.get('email') or '').lower()}>" if owner else "no active user; no owner"))
        existing_companies = {(record.get("properties") or {}).get("domain", "").lower(): record for record in _list(client, "companies", ["domain", "description"])}
        existing_deals = {(record.get("properties") or {}).get("dealname") for record in _list(client, "deals", ["dealname", "description"]) if _is_ours(record)}

    for company in PLAN:
        found = existing_companies.get(company.domain)
        if found is not None and not _is_ours(found):
            out(f"skip   {company.name}: {company.domain} is already in the account and was not made by this script")
            counts["skipped"] += 1 + len(company.deals)
            continue
        if found is not None:
            company_id = found["id"]
            out(f"exists {company.name} (company {company_id})")
            counts["existing"] += 1
        else:
            out(f"create company {company.name} [{company.lifecycle}] {company.domain} owner={_who(company.book, books)}")
            company_id = None
            if apply:
                properties = {"name": company.name, "domain": company.domain, "lifecyclestage": company.lifecycle, "description": MARKER}
                if books.get(company.book):
                    properties["hubspot_owner_id"] = str(books[company.book]["id"])
                company_id = client.http.post(client.path("objects", "companies"), {"properties": properties})["id"]
            counts["companies"] += 1
        for deal in company.deals:
            name = deal_name(company, deal)
            if name in existing_deals:
                out(f"exists   deal {name}")
                counts["existing"] += 1
                continue
            out(f"create   deal {name} [{deal.stage}, closes {_date(deal.close_in_days)[:10]}] owner={_who(deal.book, books)}")
            if apply:
                properties = {"dealname": name, "pipeline": PIPELINE, "dealstage": deal.stage, "closedate": _date(deal.close_in_days), "description": MARKER}
                if books.get(deal.book):
                    properties["hubspot_owner_id"] = str(books[deal.book]["id"])
                association = {"to": {"id": str(company_id)}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": DEAL_TO_PRIMARY_COMPANY}]}
                client.http.post(client.path("objects", "deals"), {"properties": properties, "associations": [association]})
            counts["deals"] += 1
    verb = "created" if apply else "would create"
    out(f"{verb} {counts['companies']} companies and {counts['deals']} deals; {counts['existing']} already there; {counts['skipped']} skipped")
    return counts


def _who(book: str, books: dict[str, dict]) -> str:
    owner = books.get(book)
    return f"{owner_name(owner)} (user {owner['id']}, {book})" if owner else f"({book})"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="create the records (the default is a dry run)")
    args = parser.parse_args()
    token = os.environ.get("CONNECTOR_HUBSPOT_TOKEN", "").strip()
    if args.apply and not token:
        raise SystemExit("--apply needs CONNECTOR_HUBSPOT_TOKEN")
    client = HubSpotClient(token, os.environ.get("CONNECTOR_HUBSPOT_API_VERSION", "").strip() or API_VERSION) if token else None
    try:
        run(client, args.apply)
    except ConnectorError as error:
        # The message names the call and HubSpot's answer, never the token.
        raise SystemExit(f"HubSpot refused: {error}")
