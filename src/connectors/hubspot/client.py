"""HubSpot CRM reads for the connector, on ``src.connectors.http.JsonClient``.

One static access token from a private app (``CONNECTOR_HUBSPOT_TOKEN``), sent as a bearer
header to ``api.hubapi.com`` only. Everything here is read-only and asks only for the
properties ``mapping.py`` uses: never contacts, engagements, amounts or custom fields.

HubSpot's APIs are date-versioned (``/crm/objects/2026-09/companies``); ``api_version="v3"``
uses the older numbered paths (``/crm/v3/objects/companies``) instead. ``HubSpotClient`` is
the seam tests replace with a fake.
"""

from src.connectors.http import JsonClient, bearer

BASE_URL = "https://api.hubapi.com"
API_VERSION = "2026-09"
# Search and batch reads take at most 100 ids or results per call.
BATCH = 100


class HubSpotClient:
    def __init__(self, token: str, api_version: str = API_VERSION, http: JsonClient | None = None):
        self.api_version = api_version or API_VERSION
        self.http = http or JsonClient(BASE_URL, headers=bearer(token))

    def __repr__(self) -> str:
        return f"HubSpotClient({self.api_version!r})"

    def path(self, api: str, rest: str = "") -> str:
        """``/crm/objects/2026-09/companies`` on a date version, ``/crm/v3/objects/companies`` on v3."""
        legacy = self.api_version.startswith("v")
        if api == "objects":
            base = f"/crm/{self.api_version}/objects" if legacy else f"/crm/objects/{self.api_version}"
        elif api == "associations":
            base = "/crm/v4/associations" if legacy else f"/crm/associations/{self.api_version}"
        elif api == "account-info":
            base = f"/account-info/{self.api_version}"
        else:  # owners, pipelines
            base = f"/crm/{self.api_version}/{api}" if legacy else f"/crm/{api}/{self.api_version}"
        return f"{base}/{rest}" if rest else base

    def request(self, method: str, path: str, params: dict | None = None, body: object = None) -> tuple[object, dict]:
        return self.http.request(method, path, params=params, body=body)

    def account_details(self) -> dict:
        """``portalId`` and ``uiDomain`` (``app.hubspot.com``, ``app-eu1.hubspot.com``)."""
        return self.http.get(self.path("account-info", "details")) or {}

    def owners(self, archived: bool = False) -> list[dict]:
        owners: list[dict] = []
        after = None
        while True:
            params = {"limit": BATCH, "archived": "true" if archived else "false"}
            if after:
                params["after"] = after
            data = self.http.get(self.path("owners"), params) or {}
            owners.extend(data.get("results") or [])
            after = ((data.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                return owners

    def deal_stages(self) -> dict[str, str]:
        """Every deal stage id to its label, across pipelines: ``closedwon`` -> ``Closed won``."""
        data = self.http.get(self.path("pipelines", "deals")) or {}
        return {str(stage["id"]): stage.get("label") or str(stage["id"]) for pipeline in data.get("results") or [] for stage in pipeline.get("stages") or []}

    def search(self, object_type: str, body: dict) -> tuple[list[dict], str | None, dict]:
        """One page of a CRM search, the ``after`` of the next page (None at the end) and the response headers."""
        data, headers = self.http.request("POST", self.path("objects", f"{object_type}/search"), body=body)
        data = data or {}
        after = ((data.get("paging") or {}).get("next") or {}).get("after")
        return data.get("results") or [], (str(after) if after else None), headers

    def batch_read(self, object_type: str, ids: list[str], properties: list[str]) -> list[dict]:
        records: list[dict] = []
        for start in range(0, len(ids), BATCH):
            chunk = ids[start : start + BATCH]
            body = {"properties": properties, "inputs": [{"id": str(record_id)} for record_id in chunk]}
            data = self.http.post(self.path("objects", f"{object_type}/batch/read"), body) or {}
            records.extend(data.get("results") or [])
        return records

    def associations(self, from_type: str, to_type: str, ids: list[str]) -> dict[str, list[str]]:
        """Each record id to the ids of the ``to_type`` records it is associated with."""
        linked: dict[str, list[str]] = {}
        for start in range(0, len(ids), BATCH):
            chunk = ids[start : start + BATCH]
            body = {"inputs": [{"id": str(record_id)} for record_id in chunk]}
            data = self.http.post(self.path("associations", f"{from_type}/{to_type}/batch/read"), body) or {}
            for row in data.get("results") or []:
                source = str((row.get("from") or {}).get("id"))
                linked.setdefault(source, []).extend(str(to["toObjectId"]) for to in row.get("to") or [] if to.get("toObjectId") is not None)
        return linked
