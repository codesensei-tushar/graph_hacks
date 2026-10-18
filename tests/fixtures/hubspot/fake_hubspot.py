"""An in-memory HubSpot portal behind the ``HubSpotClient`` interface, built from the JSON fixtures.

Search applies the filter groups, the sort and the ``after`` offset the connector sends, so a
test can change a record's properties and watch the next poll pick it up. Every call is
recorded in ``calls``.
"""

import copy
import json
from pathlib import Path

from src.connectors.hubspot.mapping import parse_time

HERE = Path(__file__).resolve().parent
# HubSpot's clock in the Date header of every search response.
SERVER_DATE = "Sun, 18 Oct 2026 00:00:00 GMT"
SERVER_MS = 1792281600000


def load(name: str) -> dict:
    return json.loads((HERE / name).read_text())


class FakeHubSpot:
    def __init__(self):
        self.details = load("account_details.json")
        self.companies = {record["id"]: record for record in load("companies_search.json")["results"]}
        self.deals = {record["id"]: record for record in load("deals_batch_read.json")["results"]}
        self.company_deals = {row["from"]["id"]: [str(to["toObjectId"]) for to in row["to"]] for row in load("associations_companies_deals.json")["results"]}
        self.active_owners = load("owners.json")["results"] + load("owners_page2.json")["results"]
        self.archived_owners = load("owners_archived.json")["results"]
        self.stages = {stage["id"]: stage["label"] for pipeline in load("pipelines_deals.json")["results"] for stage in pipeline["stages"]}
        self.server_date = SERVER_DATE
        self.calls: list[tuple] = []

    # --- the HubSpotClient interface ---------------------------------------------------------

    def account_details(self) -> dict:
        self.calls.append(("account_details",))
        return dict(self.details)

    def owners(self, archived: bool = False) -> list[dict]:
        self.calls.append(("owners", archived))
        return copy.deepcopy(self.archived_owners if archived else self.active_owners)

    def deal_stages(self) -> dict[str, str]:
        self.calls.append(("deal_stages",))
        return dict(self.stages)

    def search(self, object_type: str, body: dict) -> tuple[list[dict], str | None, dict]:
        self.calls.append(("search", object_type, copy.deepcopy(body)))
        records = self.companies if object_type == "companies" else self.deals
        found = [record for record in records.values() if any(all(_matches(record, rule) for rule in group["filters"]) for group in body["filterGroups"])]
        sort = body["sorts"][0]
        found.sort(key=lambda record: parse_time(record["properties"].get(sort["propertyName"])) or 0, reverse=sort["direction"] == "DESCENDING")
        start = int(body.get("after") or 0)
        page = found[start : start + body["limit"]]
        after = str(start + len(page)) if start + len(page) < len(found) else None
        return copy.deepcopy(page), after, {"date": self.server_date}

    def batch_read(self, object_type: str, ids: list[str], properties: list[str]) -> list[dict]:
        self.calls.append(("batch_read", object_type, list(ids)))
        records = self.companies if object_type == "companies" else self.deals
        return [copy.deepcopy(records[record_id]) for record_id in ids if record_id in records]

    def associations(self, from_type: str, to_type: str, ids: list[str]) -> dict[str, list[str]]:
        self.calls.append(("associations", from_type, to_type, list(ids)))
        if from_type == "companies":
            return {record_id: list(self.company_deals.get(record_id, [])) for record_id in ids if record_id in self.company_deals}
        linked: dict[str, list[str]] = {}
        for company, deals in self.company_deals.items():
            for deal in deals:
                if deal in ids:
                    linked.setdefault(deal, []).append(company)
        return linked

    # --- for tests ---------------------------------------------------------------------------

    def searches(self, object_type: str | None = None) -> list[dict]:
        return [call[2] for call in self.calls if call[0] == "search" and object_type in (None, call[1])]


def _matches(record: dict, rule: dict) -> bool:
    value = record["properties"].get(rule["propertyName"])
    operator = rule["operator"]
    if operator == "GTE":
        return (parse_time(value) or 0) * 1000 >= int(rule["value"])
    if operator == "IN":
        return (value or "").lower() in rule["values"]
    if operator == "GT":
        return float(value or 0) > float(rule["value"])
    if operator == "EQ":
        return str(value or "").lower() == str(rule["value"]).lower()
    raise ValueError(f"the fake does not know {operator}")
