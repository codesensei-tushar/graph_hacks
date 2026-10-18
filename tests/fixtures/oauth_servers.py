"""Fake Atlassian and HubSpot servers over real HTTP on 127.0.0.1, for the console OAuth tests.

One ``ThreadingHTTPServer`` answers as all of them, by path:

- Atlassian: ``POST /oauth/token`` (JSON only; authorization_code and refresh_token grants, with
  refresh tokens that rotate and die when spent), ``GET /oauth/token/accessible-resources``, and
  Jira REST v3 under ``/ex/jira/<cloudId>/rest/api/3/`` from ``tests/fixtures/jira``.
- HubSpot: ``POST /oauth/2026-09/token`` (form only; refresh tokens that do not rotate),
  ``POST /oauth/2026-09/token/introspect``, ``GET /account-info/2026-09/details`` and the CRM
  calls ``HubSpotClient`` makes, from ``tests/fixtures/hubspot``.

Every API call checks its bearer token against the live access tokens, so a stale or invented
token gets the 401 a vendor would give. ``calls`` records (method, path) of every request.
"""

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.fixtures.hubspot.fake_hubspot import SERVER_DATE, FakeHubSpot
from tests.fixtures.jira import FakeJiraClient
from tests.fixtures.jira import load as load_jira

CLIENT = {"jira": ("jira-client-id", "jira-client-secret"), "hubspot": ("hubspot-client-id", "hubspot-client-secret")}
CLOUD_ID = "11111111-2222-4333-8444-555555555555"
HUB_ID = 24681357


class FakeVendors:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls: list[tuple[str, str]] = []
        self.codes = {"jira-code": "jira", "hubspot-code": "hubspot"}
        self.access: dict[str, str] = {}  # token -> vendor
        self.refresh: dict[str, str] = {}  # live refresh token -> vendor
        self.spent: set[str] = set()
        self.counter = 0
        self.refreshes = {"jira": 0, "hubspot": 0}
        self.resources = [
            {"id": CLOUD_ID, "name": "Quillfern", "url": "https://quillfern.atlassian.net", "scopes": ["read:jira-work", "read:jira-user"], "avatarUrl": ""},
        ]
        self.hub_id = HUB_ID
        self.jira = FakeJiraClient()
        self.hubspot = FakeHubSpot()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> "FakeVendors":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _issue(self, vendor: str, refresh: str | None = None) -> dict:
        self.counter += 1
        access = f"{vendor}-access-{self.counter}"
        self.access[access] = vendor
        if refresh is None:
            refresh = f"{vendor}-refresh-{self.counter}"
            self.refresh[refresh] = vendor
        answer = {"access_token": access, "refresh_token": refresh, "expires_in": 3600 if vendor == "jira" else 1800, "token_type": "bearer"}
        if vendor == "jira":
            answer["scope"] = "read:jira-work read:jira-user offline_access"
        else:
            answer.update(hub_id=self.hub_id, scopes=["oauth", "crm.objects.companies.read"])
        return answer

    # --- token endpoints ------------------------------------------------------------------------

    def token(self, vendor: str, fields: dict) -> tuple[int, dict]:
        client_id, client_secret = CLIENT[vendor]
        if fields.get("client_id") != client_id or fields.get("client_secret") != client_secret:
            return 401, {"error": "invalid_client", "error_description": "bad client secret jira-client-secret"}
        with self.lock:
            if fields.get("grant_type") == "authorization_code":
                if self.codes.pop(fields.get("code"), None) != vendor:
                    return 400, {"error": "invalid_grant", "error_description": "code expired"}
                return 200, self._issue(vendor)
            if fields.get("grant_type") == "refresh_token":
                token = fields.get("refresh_token")
                if self.refresh.get(token) != vendor:
                    return (403 if vendor == "jira" else 400), {"error": "invalid_grant", "error_description": f"Unknown or invalid refresh token {token}"}
                self.refreshes[vendor] += 1
                if vendor == "jira":  # rotates: the spent one is dead
                    del self.refresh[token]
                    self.spent.add(token)
                    return 200, self._issue(vendor)
                return 200, self._issue(vendor, refresh=token)
        return 400, {"error": "unsupported_grant_type"}

    def introspect(self, fields: dict) -> tuple[int, dict]:
        client_id, client_secret = CLIENT["hubspot"]
        if fields.get("client_id") != client_id or fields.get("client_secret") != client_secret:
            return 401, {"error": "invalid_client"}
        token = fields.get("token")
        if self.access.get(token) != "hubspot":
            return 200, {"active": False}
        return 200, {"active": True, "hub_id": self.hub_id, "user_id": 222, "client_id": client_id, "app_id": 1234, "scopes": ["oauth"], "token_use": "access_token", "token_type": "Bearer"}

    # --- APIs -------------------------------------------------------------------------------------

    def api(self, method: str, path: str, query: dict, body: dict | None, bearer: str) -> tuple[int, object, dict]:
        vendor = self.access.get(bearer)
        if path == "/oauth/token/accessible-resources":
            return (200, self.resources, {}) if vendor == "jira" else (401, {"message": "Unauthorized"}, {})
        if path.startswith(f"/ex/jira/{CLOUD_ID}/rest/api/3/"):
            if vendor != "jira":
                return 401, {"message": "Unauthorized"}, {}
            rest = path.removeprefix(f"/ex/jira/{CLOUD_ID}/rest/api/3/")
            if rest == "myself":
                return 200, load_jira("myself.json"), {}
            if rest == "project/search":
                return 200, {"values": self.jira.projects(query.get("keys", [])), "isLast": True}, {}
            if rest == "search/jql":
                return 200, self.jira.search_page(body["jql"], body.get("nextPageToken"), body.get("maxResults", 100)), {}
            return 404, {}, {}
        if vendor != "hubspot":
            return 401, {"status": "error", "category": "INVALID_AUTHENTICATION"}, {}
        fake = self.hubspot
        if path == "/account-info/2026-09/details":
            return 200, {**fake.account_details(), "portalId": self.hub_id}, {}
        if path == "/crm/owners/2026-09":
            return 200, {"results": fake.owners(query.get("archived", ["false"])[0] == "true")}, {}
        if path == "/crm/pipelines/2026-09/deals":
            return 200, {"results": [{"stages": [{"id": key, "label": label} for key, label in fake.deal_stages().items()]}]}, {}
        parts = path.strip("/").split("/")
        if parts[:3] == ["crm", "objects", "2026-09"] and parts[4:] == ["search"]:
            records, after, headers = fake.search(parts[3], body)
            return 200, {"results": records, **({"paging": {"next": {"after": after}}} if after else {})}, {"Date": SERVER_DATE}
        if parts[:3] == ["crm", "objects", "2026-09"] and parts[4:] == ["batch", "read"]:
            return 200, {"results": fake.batch_read(parts[3], [row["id"] for row in body["inputs"]], body["properties"])}, {}
        if parts[:3] == ["crm", "associations", "2026-09"] and parts[5:] == ["batch", "read"]:
            linked = fake.associations(parts[3], parts[4], [row["id"] for row in body["inputs"]])
            return 200, {"results": [{"from": {"id": key}, "to": [{"toObjectId": int(value)} for value in values]} for key, values in linked.items()]}, {}
        return 404, {}, {}


def _handler(vendors: FakeVendors):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _answer(self, status: int, payload: object, headers: dict | None = None) -> None:
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _route(self, method: str) -> None:
            url = urllib.parse.urlsplit(self.path)
            vendors.calls.append((method, url.path))
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if method == "POST" else b""
            content_type = (self.headers.get("Content-Type") or "").split(";")[0]
            if url.path in ("/oauth/token", "/oauth/2026-09/token", "/oauth/2026-09/token/introspect"):
                jira = url.path == "/oauth/token"
                if content_type != ("application/json" if jira else "application/x-www-form-urlencoded"):
                    return self._answer(415, {"error": "unsupported_media_type"})
                fields = json.loads(raw) if jira else dict(urllib.parse.parse_qsl(raw.decode()))
                if url.path.endswith("introspect"):
                    return self._answer(*vendors.introspect(fields))
                return self._answer(*vendors.token("jira" if jira else "hubspot", fields))
            auth = self.headers.get("Authorization") or ""
            body = json.loads(raw) if raw else None
            status, payload, headers = vendors.api(method, url.path, urllib.parse.parse_qs(url.query), body, auth.removeprefix("Bearer "))
            return self._answer(status, payload, headers)

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

    return Handler
