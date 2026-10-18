"""Create Quillfern's synthetic customers and deals in a HubSpot account, for the demo.

Dry run by default: it reads the account (owners, the deal pipeline, what it created before)
and prints what it would create. ``--apply`` creates it. Without ``CONNECTOR_HUBSPOT_TOKEN``
it prints the plan alone.

It only ever creates. It never updates or deletes a record, its own included, and it touches
nothing it did not make: every company it makes has a ``.example`` domain (reserved by RFC
2606, so no real company has one) and a description that starts with ``MARKER``, and every
deal carries the marker and is associated with one of those companies. A company whose
domain is already in the account is skipped, with its deals, unless it carries the marker;
a marked company or deal that is already there is left as it is. So a second run creates
nothing. No contacts, no amounts: every company, deal and name here is invented.

Each account has an account manager from the Sales team file (Priya, Dev or Noor). The
script gives it to the HubSpot user with that person's email, so the synced ``OWNS`` lands
on their Person; when the account has no such user, it says so and spreads those accounts
over the account's own users instead (docs/HUBSPOT.md, "Identity").

    set -a; . ~/.config/company-brain/connectors.env; set +a
    .venv/bin/python scripts/seed_hubspot.py            # the plan
    .venv/bin/python scripts/seed_hubspot.py --apply    # create it

Needs the connector's read scopes plus ``crm.objects.companies.write`` and ``crm.objects.deals.write``.
"""

import argparse
import os
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.connectors.base import ConnectorError  # noqa: E402
from src.connectors.hubspot.client import API_VERSION, BATCH, HubSpotClient  # noqa: E402

MARKER = "[company-brain-seed] Synthetic demo record created by Company Brain's scripts/seed_hubspot.py; not a real company."
TEAM_FILE = Path(__file__).resolve().parents[1] / "examples" / "teams" / "quillfern-sales.toml"
PIPELINE = "default"
STAGES = ("appointmentscheduled", "qualifiedtobuy", "presentationscheduled", "decisionmakerboughtin", "contractsent", "closedwon", "closedlost")
# HubSpot-defined association type: a deal to its primary company.
DEAL_TO_PRIMARY_COMPANY = 5


@dataclass(frozen=True)
class Deal:
    name: str
    stage: str
    owner: str  # a member id in the Sales team file
    close_in_days: int  # from today; negative for a deal already closed


@dataclass(frozen=True)
class Company:
    name: str
    lifecycle: str  # customer, opportunity, salesqualifiedlead, or other (a customer who left)
    owner: str
    deals: tuple[Deal, ...] = ()

    @property
    def domain(self) -> str:
        return self.name.lower().replace(" ", "-") + ".example"


def _renewal(stage: str, owner: str, days: int, name: str = "Annual renewal 2026") -> Deal:
    return Deal(name, stage, owner, days)


# Dev manages the existing accounts, Priya brings in new business, Noor runs the paperwork.
# Every name is invented; none names a Quillfern service, team or person.
PLAN = (
    Company("Larkspur Freightways", "customer", "dev", (_renewal("closedwon", "dev", -230), Deal("Fleet tier upgrade", "contractsent", "priya", 33))),
    Company("Saltwick Outfitters", "other", "dev", (_renewal("closedlost", "dev", -65),)),
    Company("Glimmerwick Studios", "customer", "dev", (_renewal("closedwon", "dev", -160),)),
    Company("Hollowpine Health", "customer", "dev", (_renewal("closedwon", "noor", -240), Deal("Expansion to six clinics", "decisionmakerboughtin", "dev", 48))),
    Company("Marrowfield Mutual", "customer", "dev", (_renewal("contractsent", "dev", 14),)),
    Company("Wrenhallow Manufacturing", "customer", "dev", (_renewal("closedwon", "dev", -110),)),
    Company("Cinderbay Freight", "customer", "dev", (_renewal("appointmentscheduled", "dev", 89),)),
    Company("Thistlecombe Travel", "other", "dev", (_renewal("closedlost", "dev", -198),)),
    Company("Pebblewade Schools", "customer", "dev", (_renewal("closedwon", "dev", -47), Deal("Seat expansion", "qualifiedtobuy", "dev", 61))),
    Company("Velvetfield Media", "customer", "dev", (Deal("Monthly to annual switch", "closedwon", "dev", -28),)),
    Company("Tidewhistle Robotics", "opportunity", "priya", (Deal("New business: platform tier", "qualifiedtobuy", "priya", 58),)),
    Company("Ashbourne Gridworks", "customer", "priya", (Deal("Pilot", "closedwon", "dev", -109), Deal("Enterprise rollout", "presentationscheduled", "priya", 105))),
    Company("Kestrelmere Farms", "customer", "priya", (Deal("Annual plan", "closedwon", "priya", -38),)),
    Company("Otterburn Biotech", "salesqualifiedlead", "priya", (Deal("New business", "closedlost", "priya", -128),)),
    Company("Silverbirch Clinics", "opportunity", "priya", (Deal("New business", "contractsent", "priya", 23),)),
    Company("Juniperlode Retail", "customer", "priya", (_renewal("closedwon", "priya", -179),)),
    Company("Quarrystone Legal", "opportunity", "priya", (Deal("New business", "closedlost", "priya", -169), Deal("Second attempt", "qualifiedtobuy", "priya", 75))),
    Company("Bramblecote Analytics", "customer", "noor", (_renewal("contractsent", "noor", 13), Deal("Reporting add-on", "closedwon", "dev", -271))),
    Company("Fernhollow Bakehouse", "customer", "noor", (Deal("Monthly to annual switch", "closedwon", "noor", -33),)),
    Company("Mossgarth Hotels", "opportunity", "noor", (Deal("New business", "appointmentscheduled", "noor", 96),)),
)


def deal_name(company: Company, deal: Deal) -> str:
    return f"{company.name}: {deal.name}"


def team_emails() -> dict[str, tuple[str, str]]:
    """Member id -> (name, email) from the Sales team file."""
    team = tomllib.loads(TEAM_FILE.read_text())
    return {member["id"]: (member["name"], member["email"].lower()) for member in team["members"]}


def assign_owners(owners: list[dict], members: dict[str, tuple[str, str]]) -> tuple[dict[str, dict], list[str]]:
    """Member id -> the HubSpot owner for their accounts, and a line per member saying how."""
    people = sorted((owner for owner in owners if owner.get("type", "PERSON") == "PERSON" and not owner.get("archived")), key=lambda owner: int(owner["id"]))
    by_email = {(owner.get("email") or "").lower(): owner for owner in people}
    chosen: dict[str, dict] = {}
    notes: list[str] = []
    spare = [owner for owner in people if (owner.get("email") or "").lower() not in {email for _, email in members.values()}] or people
    for index, (member, (name, email)) in enumerate(sorted(members.items())):
        if email in by_email:
            chosen[member] = by_email[email]
            notes.append(f"{name}: HubSpot user {by_email[email]['id']} has {email}; their accounts will link to {name}")
        elif spare:
            owner = spare[index % len(spare)]
            chosen[member] = owner
            notes.append(f"{name}: no HubSpot user has {email}; their accounts go to user {owner['id']} ({_owner_name(owner)}), who will not link to {name}")
        else:
            notes.append(f"{name}: no HubSpot user to give their accounts to; they will have no owner")
    return chosen, notes


def _owner_name(owner: dict) -> str:
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
    members = team_emails()
    counts = {"companies": 0, "deals": 0, "skipped": 0, "existing": 0}
    if client is None:
        out("No CONNECTOR_HUBSPOT_TOKEN: the plan only, without the account's owners or what is already there.")
        owners, existing_companies, existing_deals = {}, {}, set()
    else:
        stages = client.deal_stages()
        missing = [stage for stage in STAGES if stage not in stages]
        if missing:
            raise SystemExit(f"the default deal pipeline lacks stage ids {', '.join(missing)}; nothing created")
        owners, notes = assign_owners(client.owners(), members)
        for note in notes:
            out(f"owner  {note}")
        existing_companies = {(record.get("properties") or {}).get("domain", "").lower(): record for record in _list(client, "companies", ["domain", "description"])}
        existing_deals = {(record.get("properties") or {}).get("dealname") for record in _list(client, "deals", ["dealname", "description"]) if _is_ours(record)}

    for company in PLAN:
        owner = owners.get(company.owner)
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
            out(f"create company {company.name} [{company.lifecycle}] {company.domain} owner={_who(company.owner, owner, members)}")
            company_id = None
            if apply:
                properties = {"name": company.name, "domain": company.domain, "lifecyclestage": company.lifecycle, "description": MARKER}
                if owner:
                    properties["hubspot_owner_id"] = str(owner["id"])
                company_id = client.http.post(client.path("objects", "companies"), {"properties": properties})["id"]
            counts["companies"] += 1
        for deal in company.deals:
            name = deal_name(company, deal)
            if name in existing_deals:
                out(f"exists   deal {name}")
                counts["existing"] += 1
                continue
            out(f"create   deal {name} [{deal.stage}, closes {_date(deal.close_in_days)[:10]}] owner={_who(deal.owner, owners.get(deal.owner), members)}")
            if apply:
                properties = {"dealname": name, "pipeline": PIPELINE, "dealstage": deal.stage, "closedate": _date(deal.close_in_days), "description": MARKER}
                if owners.get(deal.owner):
                    properties["hubspot_owner_id"] = str(owners[deal.owner]["id"])
                association = {"to": {"id": str(company_id)}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": DEAL_TO_PRIMARY_COMPANY}]}
                client.http.post(client.path("objects", "deals"), {"properties": properties, "associations": [association]})
            counts["deals"] += 1
    verb = "created" if apply else "would create"
    out(f"{verb} {counts['companies']} companies and {counts['deals']} deals; {counts['existing']} already there; {counts['skipped']} skipped")
    return counts


def _who(member: str, owner: dict | None, members: dict[str, tuple[str, str]]) -> str:
    name = members[member][0]
    return f"{name} (HubSpot user {owner['id']})" if owner else name


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
