"""A fake GitHub (api.github.com and github.com/login/oauth) over real HTTP on 127.0.0.1.

What the GitHub App code calls, with the auth GitHub demands:

- ``POST /app-manifests/{code}/conversions``: no auth; each code works once and answers the app
  (id, slug, client id and secret, webhook secret, and a real RSA ``pem`` made here).
- As the app (an RS256 JWT signed by that key, ``iss`` = the app id, checked here): ``GET /app``,
  ``GET /app/installations``, ``GET /app/installations/{id}``,
  ``POST /app/installations/{id}/access_tokens``.
- As an installation (its token): ``GET /installation/repositories`` and, for its repos only,
  ``/repos/{owner}/{repo}/collaborators|issues|pulls|pulls/{n}/files|pulls/{n}/reviews``, paged
  with ``per_page``/``page`` and a ``Link: rel="next"`` header as GitHub pages them.
- User sign-in: ``POST /login/oauth/access_token`` (form; ``user-code-<login>`` codes, once each)
  and, with that user token, ``GET /user`` and ``GET /user/installations``.

``rate_limit_next`` makes the next installation-token call answer 403 with
``X-RateLimit-Remaining: 0`` and a reset one second away. ``calls`` records (method, path).
"""

import json
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

APP_ID = 90210
APP_SLUG = "company-brain-test"
CLIENT_ID = "Iv23-test-client"
CLIENT_SECRET = "github-client-secret-value"
WEBHOOK_SECRET = "github-webhook-secret-value"


def user(user_id: int, login: str) -> dict:
    return {"id": user_id, "login": login, "type": "User"}


def make_repo(repo_id: int, full_name: str, private: bool, pulls: int = 3, issues: int = 2, authors: list[dict] | None = None) -> dict:
    """A repo with ``pulls`` pull requests (each with two changed files and one review) and ``issues`` issues."""
    authors = authors or [user(501, "dev-one"), user(502, "dev-two")]
    owner = full_name.split("/")[0]
    base = {"id": repo_id, "full_name": full_name, "name": full_name.split("/")[1], "private": private,
            "html_url": f"https://github.com/{full_name}", "owner": {"id": 0, "login": owner}}
    data = {"repo": base, "collaborators": list(authors), "pulls": [], "issues": [], "files": {}, "reviews": {}}
    for number in range(1, issues + 1):
        data["issues"].append({"number": number, "title": f"Issue {number} in {full_name}", "body": "", "state": "open",
                               "user": authors[number % len(authors)], "assignees": [], "html_url": f"https://github.com/{full_name}/issues/{number}",
                               "updated_at": "2026-10-01T00:00:00Z"})
    for index in range(pulls):
        number = issues + 1 + index
        data["pulls"].append({"number": number, "title": f"Change {number} in {full_name}", "body": "Closes #1" if index == 0 and issues else "",
                              "state": "open", "user": authors[index % len(authors)], "html_url": f"https://github.com/{full_name}/pull/{number}",
                              "updated_at": f"2026-10-{1 + index % 28:02d}T00:00:00Z", "head": {"ref": f"change-{number}"}})
        data["files"][number] = [{"filename": "src/domain/memory/search.py"}, {"filename": "README.md"}]
        data["reviews"][number] = [{"user": authors[(index + 1) % len(authors)], "state": "APPROVED"}]
    return data


class FakeGitHub:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls: list[tuple[str, str]] = []
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        self.public_key = key.public_key()
        self.app_id = APP_ID
        self.app_owner = user(77, "brain-operator")
        self.manifest_codes = {"manifest-code"}
        self.installations: dict[int, dict] = {}  # id -> {id, account, repos: [full_name], users: [login], suspended_at}
        self.repos: dict[str, dict] = {}  # full_name -> make_repo(...)
        self.tokens: dict[str, int] = {}  # installation token -> installation id
        self.user_codes: dict[str, str] = {}  # code -> login
        self.user_tokens: dict[str, str] = {}  # token -> login
        self.users: dict[str, dict] = {}
        self.rate_limit_next = 0
        self.counter = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> "FakeGitHub":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # --- set up ------------------------------------------------------------------------------

    def add_installation(self, installation_id: int, account: dict, repos: list[dict], installers: list[str]) -> None:
        for data in repos:
            data["repo"]["owner"] = {"id": account["id"], "login": account["login"]}
            self.repos[data["repo"]["full_name"]] = data
        self.installations[installation_id] = {
            "id": installation_id, "account": account, "repos": [data["repo"]["full_name"] for data in repos],
            "users": list(installers), "suspended_at": None,
        }

    def user_code(self, login: str, user_id: int) -> str:
        self.users[login] = user(user_id, login)
        self.counter += 1
        code = f"user-code-{login}-{self.counter}"
        self.user_codes[code] = login
        return code

    def installation_payload(self, installation_id: int) -> dict:
        found = self.installations[installation_id]
        return {"id": found["id"], "account": found["account"], "target_type": found["account"].get("type", "User"),
                "repository_selection": "selected", "html_url": f"https://github.com/settings/installations/{found['id']}",
                "suspended_at": found["suspended_at"], "app_id": self.app_id, "app_slug": APP_SLUG}

    # --- auth --------------------------------------------------------------------------------

    def app_ok(self, header: str) -> bool:
        if not header.startswith("Bearer "):
            return False
        try:
            claims = jwt.decode(header[7:], self.public_key, algorithms=["RS256"], options={"verify_aud": False})
        except jwt.PyJWTError:
            return False
        return str(claims.get("iss")) == str(self.app_id) and claims["exp"] - claims["iat"] <= 600

    def installation_of(self, header: str) -> int | None:
        token = header[7:] if header.startswith("Bearer ") else header[6:] if header.startswith("token ") else ""
        return self.tokens.get(token)


def _page(items: list, query: dict, base: str, path: str) -> tuple[list, str | None]:
    per_page = min(int(query.get("per_page", ["30"])[0]), 100)
    page = int(query.get("page", ["1"])[0])
    chunk = items[(page - 1) * per_page : page * per_page]
    more = page * per_page < len(items)
    if not more:
        return chunk, None
    rest = {key: values[0] for key, values in query.items()}
    rest.update(page=str(page + 1), per_page=str(per_page))
    return chunk, f'<{base}{path}?{urllib.parse.urlencode(rest)}>; rel="next"'


def _handler(fake: FakeGitHub):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _send(self, status: int, body, headers: dict | None = None):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        def do_DELETE(self):
            self._send(404, {"message": "Not Found"})

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            with fake.lock:
                fake.calls.append(("POST", path))
            if match := re.fullmatch(r"/app-manifests/([^/]+)/conversions", path):
                code = urllib.parse.unquote(match.group(1))
                with fake.lock:
                    if code not in fake.manifest_codes:
                        return self._send(404, {"message": "Not Found"})
                    fake.manifest_codes.discard(code)
                return self._send(201, {
                    "id": fake.app_id, "slug": APP_SLUG, "node_id": "A_1", "name": "Company Brain Test", "owner": fake.app_owner,
                    "html_url": f"https://github.com/apps/{APP_SLUG}", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                    "webhook_secret": WEBHOOK_SECRET, "pem": fake.pem, "permissions": {}, "events": [],
                })
            if match := re.fullmatch(r"/app/installations/(\d+)/access_tokens", path):
                if not fake.app_ok(self.headers.get("Authorization", "")):
                    return self._send(401, {"message": "A JSON web token could not be decoded"})
                installation_id = int(match.group(1))
                if installation_id not in fake.installations:
                    return self._send(404, {"message": "Not Found"})
                with fake.lock:
                    fake.counter += 1
                    token = f"ghs_{installation_id}_{fake.counter}"
                    fake.tokens[token] = installation_id
                expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
                return self._send(201, {"token": token, "expires_at": expires})
            if path == "/login/oauth/access_token":
                fields = dict(urllib.parse.parse_qsl(raw.decode()))
                if fields.get("client_id") != CLIENT_ID or fields.get("client_secret") != CLIENT_SECRET:
                    return self._send(200, {"error": "incorrect_client_credentials"})
                with fake.lock:
                    login = fake.user_codes.pop(fields.get("code") or "", None)
                    if login is None:
                        return self._send(200, {"error": "bad_verification_code"})
                    fake.counter += 1
                    token = f"ghu_{login}_{fake.counter}"
                    fake.user_tokens[token] = login
                return self._send(200, {"access_token": token, "token_type": "bearer"})
            self._send(404, {"message": "Not Found"})

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
            auth = self.headers.get("Authorization", "")
            with fake.lock:
                fake.calls.append(("GET", path))
            if path == "/app" or path.startswith("/app/"):
                if not fake.app_ok(auth):
                    return self._send(401, {"message": "A JSON web token could not be decoded"})
                if path == "/app":
                    return self._send(200, {"id": fake.app_id, "slug": APP_SLUG, "name": "Company Brain Test", "owner": fake.app_owner})
                if path == "/app/installations":
                    items = [fake.installation_payload(i) for i in sorted(fake.installations)]
                    chunk, link = _page(items, query, fake.base_url, path)
                    return self._send(200, chunk, {"Link": link} if link else None)
                if match := re.fullmatch(r"/app/installations/(\d+)", path):
                    installation_id = int(match.group(1))
                    if installation_id not in fake.installations:
                        return self._send(404, {"message": "Not Found"})
                    return self._send(200, fake.installation_payload(installation_id))
                return self._send(404, {"message": "Not Found"})
            if path in ("/user", "/user/installations"):
                login = fake.user_tokens.get(auth[7:] if auth.startswith("Bearer ") else "")
                if login is None:
                    return self._send(401, {"message": "Bad credentials"})
                if path == "/user":
                    return self._send(200, fake.users[login])
                mine = [fake.installation_payload(i) for i, found in sorted(fake.installations.items()) if login in found["users"]]
                return self._send(200, {"total_count": len(mine), "installations": mine})

            installation_id = fake.installation_of(auth)
            if installation_id is None or installation_id not in fake.installations:
                return self._send(401, {"message": "Bad credentials"})
            with fake.lock:
                if fake.rate_limit_next > 0:
                    fake.rate_limit_next -= 1
                    return self._send(403, {"message": "API rate limit exceeded"}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(time.time()) + 1)})
            installation = fake.installations[installation_id]
            if path == "/installation/repositories":
                items = [fake.repos[name]["repo"] for name in installation["repos"]]
                chunk, link = _page(items, query, fake.base_url, path)
                return self._send(200, {"total_count": len(items), "repositories": chunk}, {"Link": link} if link else None)
            match = re.fullmatch(r"/repos/([^/]+/[^/]+)/(collaborators|issues|pulls)(?:/(\d+)/(files|reviews))?", path)
            if not match or match.group(1) not in installation["repos"]:
                return self._send(404, {"message": "Not Found"})
            data = fake.repos[match.group(1)]
            if match.group(3):
                items = data[match.group(4)].get(int(match.group(3)), [])
            elif match.group(2) == "collaborators":
                items = data["collaborators"]
            elif match.group(2) == "issues":
                # The issues endpoint lists pull requests too, marked as such.
                items = data["issues"] + [{**pr, "pull_request": {}} for pr in data["pulls"]]
            else:
                items = sorted(data["pulls"], key=lambda pr: pr["number"], reverse=True)
            chunk, link = _page(items, query, fake.base_url, path)
            return self._send(200, chunk, {"Link": link} if link else None)

    return Handler
