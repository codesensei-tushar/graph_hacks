"""Console sign-in, sessions, CSRF, invites and org isolation, against real FalkorDB/kv
with a fake Slack (OIDC token endpoint, signing keys and users.info).

Each org gets its own throwaway graph, the way package B's tenancy maps orgs to graphs,
and the control graph is a throwaway too. Needs FalkorDB on localhost:6379.
"""

import base64
import dataclasses
import hashlib
import json
import time
import unittest
import uuid
from unittest import mock
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import tests
from src.api.v1 import auth
from src.core.config import settings as real_settings
from src.db import falkordb_client
from src.db.falkordb_client import kv
from src.domain.access import roles
from src.core import tenancy
from src.domain import orgs
from src.domain.console import csrf, oidc
from src.domain.console import session as sessions
from src.domain.slack import workspace as ws_module
from src.domain.slack.token_crypto import encrypt_token
from src.domain.slack.workspace import upsert_workspace
from src.integrations.slack.client import SlackUserInfo

RUN = uuid.uuid4().hex[:6]
CONTROL = f"brain_control_test{tests.TEST_GRAPH_SUFFIX}_{RUN}"
ORIGIN = "https://testserver"
CLIENT_ID = "123.456"


CREATED_GRAPHS: set[str] = set()


SETTINGS = dataclasses.replace(
    real_settings,
    console_enabled=True,
    slack_client_id=CLIENT_ID,
    slack_client_secret="shh",
    public_url=ORIGIN,
    brain_admin_token="admin-token",
    slack_api_base_url="https://slack.test/api/",
)


class FakeSlack:
    """Slack's openid.connect.token and signing keys, plus users.info for the bot client."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "kid-1"
        self.codes: dict[str, dict] = {}
        self.users: dict[str, SlackUserInfo] = {}

    def jwks(self) -> dict:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {"keys": [{**jwk, "kid": self.kid, "alg": "RS256", "use": "sig"}]}

    def id_token(self, claims: dict, key=None, kid=None) -> str:
        return jwt.encode(claims, key or self.key, algorithm="RS256", headers={"kid": kid or self.kid})

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url == oidc.JWKS_URL:
            return httpx.Response(200, json=self.jwks())
        if str(request.url) == "https://slack.test/api/openid.connect.token":
            form = parse_qs(request.content.decode())
            grant = self.codes.pop(form["code"][0], None)
            if grant is None or form["client_secret"] != ["shh"]:
                return httpx.Response(200, json={"ok": False, "error": "invalid_code"})
            challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
            if challenge != grant["challenge"]:
                return httpx.Response(200, json={"ok": False, "error": "invalid_code_verifier"})
            return httpx.Response(200, json={"ok": True, "id_token": grant["token"]})
        return httpx.Response(404)

    def user_info(self, user_id):
        return self.users.get(user_id)


class FakeBotClient:
    def __init__(self, slack: FakeSlack):
        self.slack = slack

    def user_info(self, user_id):
        return self.slack.user_info(user_id)

    def bot_user_id(self):
        return "UBOT"


def _test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(auth.router)

    @app.get("/console/api/whoami")
    def whoami(session=Depends(sessions.console_user)):
        return {"org": tenancy.current_org(), "session_org": session.org, "graph": falkordb_client.graph_name()}

    @app.post("/console/api/connectors/fake/sync")
    def sync(session=Depends(sessions.console_admin)):
        return {"ok": True, "org": tenancy.current_org()}

    @app.get("/console/api/pending")
    def pending(session=Depends(sessions.console_pending)):
        return {"org": tenancy.current_org(), "role": session.role}

    @app.post("/console/api/ping")
    def ping(session=Depends(sessions.console_user)):
        return {"ok": True}

    return app


class ConsoleAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patches = [
            mock.patch.object(tenancy, "CONTROL_GRAPH", CONTROL),
        ] + [mock.patch.object(module, "settings", SETTINGS) for module in (sessions, csrf, oidc, auth)]
        for patch in cls.patches:
            patch.start()

    @classmethod
    def tearDownClass(cls):
        db = falkordb_client.client()
        for name in db.list_graphs():
            if name == CONTROL or name in CREATED_GRAPHS:
                db.select_graph(name).delete()
        for patch in reversed(cls.patches):
            patch.stop()

    def setUp(self):
        self.slack = FakeSlack()
        oidc.keys.clear()
        self._transport = mock.patch.object(oidc, "transport", httpx.MockTransport(self.slack.handler))
        self._transport.start()
        self._bot = mock.patch.object(ws_module, "_client_for", return_value=FakeBotClient(self.slack))
        self._bot.start()
        self.client = TestClient(_test_app(), base_url=ORIGIN, follow_redirects=False)
        tenancy.control_graph().query("MATCH (n) DETACH DELETE n")
        orgs.clear_route_cache()

        self.team_x, self.team_y = f"TX{RUN.upper()}", f"TY{RUN.upper()}"
        self.org_x = self._org("Org X", self.team_x)
        self.org_y = self._org("Org Y", self.team_y)
        self.slack.users = {
            "UADA": SlackUserInfo(id="UADA", real_name="Ada Park", team_id=self.team_x),
            "UBEA": SlackUserInfo(id="UBEA", real_name="Bea", team_id=self.team_x),
            "UGUEST": SlackUserInfo(id="UGUEST", real_name="Guest", team_id=self.team_x, is_restricted=True),
            "UCONNECT": SlackUserInfo(id="UCONNECT", real_name="Elsewhere", team_id="TOTHER", is_stranger=True),
            "UYAN": SlackUserInfo(id="UYAN", real_name="Yan", team_id=self.team_y),
        }

    def tearDown(self):
        rows = tenancy.control_graph().query("MATCH (o:Org) WHERE o.legacy = false RETURN o.graph").result_set
        CREATED_GRAPHS.update(row[0] for row in rows)
        self._transport.stop()
        self._bot.stop()

    def _org(self, name: str, team: str) -> str:
        org = orgs.create_org(name, "TNOBODY:UNOBODY")
        CREATED_GRAPHS.add(org.graph)
        orgs.route_team(team, org.id)
        with tenancy.org_context(org.id):
            _, graph = falkordb_client.connect()
            graph.query("CREATE (:Person {id: 'p-ada', name: 'Ada Park', email: 'ada@x.example.com'})")
            upsert_workspace(team_id=team, bot_token_enc=encrypt_token("xoxb-test"), bot_user_id="UBOT", team_name=name, installed_by="UINSTALLER")
        return org.id

    # --- helpers ---------------------------------------------------------------------------

    def _claims(self, team: str, user: str, nonce: str, **extra) -> dict:
        now = int(time.time())
        return {
            "iss": "https://slack.com",
            "aud": CLIENT_ID,
            "sub": user,
            "iat": now,
            "exp": now + 300,
            "nonce": nonce,
            "https://slack.com/team_id": team,
            "https://slack.com/user_id": user,
            "email": "ada@x.example.com" if user == "UADA" else f"{user.lower()}@x.example.com",
            "email_verified": True,
            "name": user,
            **extra,
        }

    def _start(self) -> dict:
        response = self.client.get("/auth/slack/start")
        self.assertEqual(response.status_code, 302)
        return {k: v[0] for k, v in parse_qs(urlparse(response.headers["location"]).query).items()}

    def _sign_in(self, team: str, user: str, token_override=None, claims_extra=None):
        params = self._start()
        code = uuid.uuid4().hex
        token = token_override(params) if token_override else self.slack.id_token(self._claims(team, user, params["nonce"], **(claims_extra or {})))
        self.slack.codes[code] = {"token": token, "challenge": params["code_challenge"]}
        return self.client.get("/auth/slack/callback", params={"code": code, "state": params["state"]}), params

    def _signed_in(self, team: str, user: str) -> dict:
        response, _ = self._sign_in(team, user)
        self.assertEqual(response.headers["location"], "/console", response.headers.get("location"))
        return self.client.get("/console/api/me").json()

    def _mutate(self, path: str, body=None, **headers):
        me = self.client.get("/console/api/me").json()
        base = {"origin": ORIGIN, "x-csrf-token": me["csrf"], "content-type": "application/json"}
        base.update({k.replace("_", "-"): v for k, v in headers.items()})
        return self.client.post(path, content=json.dumps(body or {}), headers={k: v for k, v in base.items() if v is not None})

    def _session_key(self) -> str:
        return f"console:session:{hashlib.sha256(self.client.cookies[sessions.COOKIE].encode()).hexdigest()}"

    def _age(self, **fields):
        key = self._session_key()
        record = json.loads(kv().get(key))
        record.update(fields)
        kv().set(key, json.dumps(record), ex=3600)

    def _mint(self) -> str:
        response = self.client.post("/ops/invites", headers={"authorization": "Bearer admin-token"})
        self.assertEqual(response.status_code, 200)
        return response.json()["code"]

    def _audit(self, action: str) -> list:
        return tenancy.control_graph().query(
            "MATCH (e:AuditEvent {action: $a}) RETURN e.org, e.actor, e.result ORDER BY e.at", {"a": action}
        ).result_set

    # --- sign in -----------------------------------------------------------------------------

    def test_start_sends_state_nonce_and_pkce(self):
        params = self._start()
        self.assertEqual((params["client_id"], params["scope"], params["response_type"]), (CLIENT_ID, "openid email profile", "code"))
        self.assertEqual(params["redirect_uri"], f"{ORIGIN}/auth/slack/callback")
        self.assertEqual(params["code_challenge_method"], "S256")
        stored = json.loads(kv().get(f"console:oidc:{params['state']}"))
        self.assertEqual(stored["nonce"], params["nonce"])
        self.assertGreater(kv().ttl(f"console:oidc:{params['state']}"), 0)

    def test_member_signs_in_and_cookie_is_locked_down(self):
        response, _ = self._sign_in(self.team_x, "UADA")
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/console"))
        cookie = next(c for c in response.headers.get_list("set-cookie") if c.startswith(f"{sessions.COOKIE}="))
        lowered = cookie.lower()
        for flag in ("secure", "httponly", "samesite=lax", "path=/"):
            self.assertIn(flag, lowered)
        self.assertNotIn("domain=", lowered)
        sid = self.client.cookies[sessions.COOKIE]
        self.assertGreaterEqual(len(base64.urlsafe_b64decode(sid + "==")), 32)
        self.assertIsNone(kv().get(f"console:session:{sid}"))
        stored = json.loads(kv().get(self._session_key()))
        self.assertEqual({k: stored[k] for k in ("org", "person", "team", "slack_user", "role")},
                         {"org": self.org_x, "person": "p-ada", "team": self.team_x, "slack_user": "UADA", "role": "member"})
        self.assertLessEqual(kv().ttl(self._session_key()), 12 * 3600)

        me = self.client.get("/console/api/me").json()
        self.assertEqual(me["org"], {"id": self.org_x, "name": "Org X", "slug": "org-x"})
        self.assertEqual((me["person"]["id"], me["role"], me["slack_connected"]), ("p-ada", "member", True))
        self.assertTrue(me["csrf"])
        self.assertEqual(self._audit("signin")[-1], [self.org_x, f"slack:{self.team_x}:UADA", "ok"])

    def test_state_works_once(self):
        response, params = self._sign_in(self.team_x, "UADA")
        self.assertEqual(response.headers["location"], "/console")
        replay = self.client.get("/auth/slack/callback", params={"code": "x", "state": params["state"]})
        self.assertEqual(replay.headers["location"], "/signin?error=expired")
        unknown = self.client.get("/auth/slack/callback", params={"code": "x", "state": "nope"})
        self.assertEqual(unknown.headers["location"], "/signin?error=expired")

    def test_cancelled_sign_in_spends_the_state(self):
        params = self._start()
        response = self.client.get("/auth/slack/callback", params={"error": "access_denied", "state": params["state"]})
        self.assertEqual(response.headers["location"], "/signin?error=denied")
        self.assertIsNone(kv().get(f"console:oidc:{params['state']}"))

    def test_forged_or_mismatched_id_tokens_are_refused(self):
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cases = {
            "wrong nonce": lambda p: self.slack.id_token(self._claims(self.team_x, "UADA", "not-the-nonce")),
            "wrong audience": lambda p: self.slack.id_token({**self._claims(self.team_x, "UADA", p["nonce"]), "aud": "someone-else"}),
            "wrong issuer": lambda p: self.slack.id_token({**self._claims(self.team_x, "UADA", p["nonce"]), "iss": "https://evil.example"}),
            "expired": lambda p: self.slack.id_token({**self._claims(self.team_x, "UADA", p["nonce"]), "exp": int(time.time()) - 600}),
            "other signing key": lambda p: self.slack.id_token(self._claims(self.team_x, "UADA", p["nonce"]), key=other_key),
            "unknown kid": lambda p: self.slack.id_token(self._claims(self.team_x, "UADA", p["nonce"]), kid="kid-9"),
            "alg none": lambda p: jwt.encode(self._claims(self.team_x, "UADA", p["nonce"]), None, algorithm="none"),
            "HS256 with the public key": lambda p: jwt.encode(
                self._claims(self.team_x, "UADA", p["nonce"]), "x" * 32, algorithm="HS256", headers={"kid": self.slack.kid}
            ),
        }
        for name, forge in cases.items():
            with self.subTest(name):
                self.client.cookies.clear()
                response, _ = self._sign_in(self.team_x, "UADA", token_override=forge)
                self.assertEqual(response.headers["location"], "/signin?error=bad_token")
                self.assertNotIn(sessions.COOKIE, self.client.cookies)

    def test_token_endpoint_refusal_is_not_echoed(self):
        params = self._start()
        response = self.client.get("/auth/slack/callback", params={"code": "unknown-code", "state": params["state"]})
        self.assertEqual(response.headers["location"], "/signin?error=slack_refused")

    def test_guests_and_slack_connect_users_are_refused(self):
        for user in ("UGUEST", "UCONNECT"):
            with self.subTest(user):
                self.client.cookies.clear()
                response, _ = self._sign_in(self.team_x, user)
                self.assertEqual(response.headers["location"], "/signin?error=not_a_member")
                self.assertNotIn(sessions.COOKIE, self.client.cookies)
        self.assertEqual([row[2] for row in self._audit("signin")], ["refused:not_a_member"] * 2)

    def test_unknown_slack_user_and_left_person_are_refused(self):
        response, _ = self._sign_in(self.team_x, "UGHOST")
        self.assertEqual(response.headers["location"], "/signin?error=slack_unavailable")
        self._signed_in(self.team_x, "UADA")
        with tenancy.org_context(self.org_x):
            _, graph = falkordb_client.connect()
            graph.query("MATCH (p:Person {id: 'p-ada'}) SET p.org_member = false")
        self.client.cookies.clear()
        response, _ = self._sign_in(self.team_x, "UADA")
        self.assertEqual(response.headers["location"], "/signin?error=not_a_member")

    def test_sign_in_rotates_the_session_id(self):
        self._signed_in(self.team_x, "UADA")
        old_key = self._session_key()
        self._signed_in(self.team_x, "UBEA")
        self.assertIsNone(kv().get(old_key))
        self.assertNotEqual(old_key, self._session_key())

    # --- sessions -----------------------------------------------------------------------------

    def test_no_cookie_or_garbage_cookie_is_signed_out(self):
        self.assertEqual(self.client.get("/console/api/me").status_code, 401)
        self.client.cookies.set(sessions.COOKIE, "garbage")
        self.assertEqual(self.client.get("/console/api/me").status_code, 401)

    def test_idle_expiry(self):
        self._signed_in(self.team_x, "UADA")
        self._age(last_seen=time.time() - 12 * 3600 - 1)
        self.assertEqual(self.client.get("/console/api/me").status_code, 401)

    def test_absolute_expiry_survives_activity(self):
        self._signed_in(self.team_x, "UADA")
        self._age(created_at=time.time() - 7 * 24 * 3600 - 1)
        self.assertEqual(self.client.get("/console/api/me").status_code, 401)

    def test_ttl_never_outlives_the_absolute_limit(self):
        self._signed_in(self.team_x, "UADA")
        self._age(created_at=time.time() - 7 * 24 * 3600 + 120)
        self.assertEqual(self.client.get("/console/api/me").status_code, 200)
        self.assertLessEqual(kv().ttl(self._session_key()), 120)

    def test_role_is_re_read_after_a_minute(self):
        self._signed_in(self.team_x, "UBEA")
        self.assertEqual(self._mutate("/console/api/connectors/fake/sync").status_code, 403)
        with tenancy.org_context(self.org_x):
            roles.set_role("UBEA", roles.ROLE_ADMIN)
        self.assertEqual(self.client.get("/console/api/me").json()["role"], "member")
        self._age(role_checked_at=time.time() - 61)
        self.assertEqual(self.client.get("/console/api/me").json()["role"], "admin")
        self.assertEqual(self._mutate("/console/api/connectors/fake/sync").status_code, 200)

    def test_a_person_who_leaves_is_signed_out_at_the_next_refresh(self):
        self._signed_in(self.team_x, "UADA")
        with tenancy.org_context(self.org_x):
            _, graph = falkordb_client.connect()
            graph.query("MATCH (m:SlackWorkspaceMember) SET m.status = 'revoked'")
        self._age(role_checked_at=time.time() - 61)
        self.assertEqual(self.client.get("/console/api/me").status_code, 401)
        self.assertIsNone(kv().get(self._session_key()))

    def test_env_owner_is_not_owner_of_other_orgs(self):
        # tests/__init__ sets SLACK_OWNER_ID=UOWNER for the legacy deployment.
        with tenancy.org_context(self.org_x):
            self.assertEqual(roles.role_of("UOWNER", env_roles=False), roles.ROLE_MEMBER)
            self.assertEqual(roles.role_of("UOWNER"), roles.ROLE_OWNER)
            self.assertEqual(roles.role_of("UINSTALLER", env_roles=False), roles.ROLE_OWNER)

    def test_a_request_racing_logout_cannot_revive_the_session(self):
        self._signed_in(self.team_x, "UADA")
        sid = self.client.cookies[sessions.COOKIE]
        loaded = sessions.load(sid)
        sessions.destroy(sid)
        self.assertIsNone(sessions.refresh(loaded))
        self.assertIsNone(kv().get(self._session_key()))

    def test_logout(self):
        self._signed_in(self.team_x, "UADA")
        key = self._session_key()
        self.assertEqual(self.client.post("/auth/logout", headers={"origin": "https://evil.example"}).status_code, 403)
        self.assertIsNotNone(kv().get(key))
        response = self.client.post("/auth/logout", headers={"origin": ORIGIN})
        self.assertEqual(response.status_code, 303)
        self.assertIsNone(kv().get(key))
        self.assertEqual(self.client.get("/console/api/me").status_code, 401)

    # --- CSRF -----------------------------------------------------------------------------------

    def test_csrf(self):
        self._signed_in(self.team_x, "UADA")
        self.assertEqual(self._mutate("/console/api/ping").status_code, 200)
        self.assertEqual(self._mutate("/console/api/ping", x_csrf_token=None).status_code, 403)
        self.assertEqual(self._mutate("/console/api/ping", x_csrf_token="wrong").status_code, 403)
        self.assertEqual(self._mutate("/console/api/ping", origin=None).status_code, 403)
        self.assertEqual(self._mutate("/console/api/ping", origin="https://evil.example").status_code, 403)
        self.assertEqual(self._mutate("/console/api/ping", content_type="application/x-www-form-urlencoded").status_code, 415)
        self.assertEqual(self._mutate("/console/api/ping", content_type="text/plain").status_code, 415)

    # --- orgs and invites ----------------------------------------------------------------------

    def test_new_team_creates_an_org_with_an_invite(self):
        team = f"TNEW{RUN.upper()}"
        response, _ = self._sign_in(team, "UFOUNDER")
        self.assertEqual(response.headers["location"], "/console/start")
        me = self.client.get("/console/api/me").json()
        self.assertEqual((me["org"], me["role"]), (None, None))
        self.assertEqual(self.client.get("/console/api/whoami").status_code, 403)
        old_key = self._session_key()

        code = self._mint()
        response = self._mutate("/console/api/orgs", {"name": "  Acme   Robotics ", "invite_code": code})
        self.assertEqual(response.status_code, 201, response.text)
        created = response.json()
        # Pending, not owner, until the Slack install verifies them: only that step is open.
        self.assertEqual((created["org"]["name"], created["role"]), ("Acme Robotics", "pending"))
        self.assertIsNone(kv().get(old_key))
        org_id = created["org"]["id"]
        self.assertEqual(self.client.get("/console/api/whoami").json(), {"detail": "slack_install_pending"})
        self.assertEqual(self.client.get("/console/api/pending").json()["org"], org_id)
        self.assertEqual(self._audit("org.create")[-1], [org_id, f"slack:{team}:UFOUNDER", "ok"])

        # A second org from the same person, and the same code again, are both refused.
        self.assertEqual(self._mutate("/console/api/orgs", {"name": "Again", "invite_code": self._mint()}).status_code, 409)
        # Signing in again before the bot is installed finds the org they created.
        self.client.cookies.clear()
        response, _ = self._sign_in(team, "UFOUNDER")
        self.assertEqual(response.headers["location"], "/console/start")
        me = self.client.get("/console/api/me").json()
        self.assertEqual((me["org"]["id"], me["role"]), (org_id, "pending"))

        # The bot install (package B) verifies the creator, then routes the team: now owner.
        self.slack.users["UFOUNDER"] = SlackUserInfo(id="UFOUNDER", real_name="Founder", team_id=team)
        self._install_and_route(org_id, team, "UFOUNDER")
        # At once, not after the minute between role checks: the route is what a pending role waits for.
        self.assertEqual(self.client.get("/console/api/me").json()["role"], "owner")
        self.assertEqual(self.client.get("/console/api/whoami").json()["org"], org_id)

    def test_the_slack_install_callback_promotes_the_installers_pending_session(self):
        from src.api.v1 import slack_install

        team = f"TPRO{RUN.upper()}"
        self._sign_in(team, "UFOUNDER")
        org_id = self._mutate("/console/api/orgs", {"name": "Promoted", "invite_code": self._mint()}).json()["org"]["id"]
        self.slack.users["UFOUNDER"] = SlackUserInfo(id="UFOUNDER", real_name="Founder", team_id=team)
        self._install_and_route(org_id, team, "UFOUNDER")
        request = mock.Mock(cookies={sessions.COOKIE: self.client.cookies[sessions.COOKIE]})
        slack_install._promote_pending_creator(request, tenancy.resolve(org_id), team)
        # The stored record itself is owner, before the browser's next request reaches the console.
        self.assertEqual(json.loads(kv().get(self._session_key()))["role"], "owner")
        # A cookie that is no session is ignored.
        slack_install._promote_pending_creator(mock.Mock(cookies={sessions.COOKIE: "not-a-session"}), tenancy.resolve(org_id), team)

    def test_an_unreadable_bot_token_is_named_for_the_operator(self):
        with tenancy.org_context(self.org_x):
            from cryptography.fernet import Fernet

            # Encrypted under a key TOKEN_ENCRYPTION_KEYS does not hold (dropped in a rotation).
            lost = "fernet:" + Fernet(Fernet.generate_key()).encrypt(b"xoxb-test").decode()
            upsert_workspace(team_id=self.team_x, bot_token_enc=lost, bot_user_id="UBOT", team_name="Org X", installed_by="UINSTALLER")
            with self.assertLogs(sessions.log, "ERROR") as logged:
                self.assertEqual(sessions.slack_membership_refusal(self.team_x, "UADA"), "workspace_key")
        self.assertIn("TOKEN_ENCRYPTION_KEYS", logged.output[0])
        self.assertNotIn("xoxb", logged.output[0])
        response, _ = self._sign_in(self.team_x, "UADA")
        self.assertEqual(response.headers["location"], "/signin?error=workspace_key")
        # No workspace at all is still the transient code.
        with tenancy.org_context(self.org_x):
            self.assertEqual(sessions.slack_membership_refusal("TNOSUCH", "UADA"), "workspace_unavailable")

    def _install_and_route(self, org_id: str, team: str, installer: str) -> None:
        with tenancy.org_context(org_id):
            upsert_workspace(team_id=team, bot_token_enc=encrypt_token("xoxb-new"), bot_user_id="UBOT", team_name="New", installed_by=installer)
            self.assertIsNone(sessions.slack_membership_refusal(team, installer))
        orgs.route_team(team, org_id)

    # --- review regressions ----------------------------------------------------------------

    def test_login_csrf_code_from_another_browser_is_refused(self):
        # The attacker starts sign-in in their own browser and gets a code for their own account...
        params = self._start()
        code = uuid.uuid4().hex
        token = self.slack.id_token(self._claims(self.team_x, "UBEA", params["nonce"]))
        self.slack.codes[code] = {"token": token, "challenge": params["code_challenge"]}
        # ...then lures the victim's browser, which never started that sign-in, to the callback.
        victim = TestClient(_test_app(), base_url=ORIGIN, follow_redirects=False)
        response = victim.get("/auth/slack/callback", params={"code": code, "state": params["state"]})
        self.assertEqual(response.headers["location"], "/signin?error=wrong_browser")
        self.assertNotIn(sessions.COOKIE, victim.cookies)
        # The state is spent, so the attacker can't retry it either.
        retry = self.client.get("/auth/slack/callback", params={"code": code, "state": params["state"]})
        self.assertEqual(retry.headers["location"], "/signin?error=expired")

    def test_binding_cookie_is_locked_down_and_cleared(self):
        start = self.client.get("/auth/slack/start")
        cookie = start.headers["set-cookie"].lower()
        self.assertTrue(cookie.startswith(f"{oidc.BINDING_COOKIE.lower()}="))
        for flag in ("secure", "httponly", "samesite=lax", "path=/", "max-age=600"):
            self.assertIn(flag, cookie)
        self.client.cookies.clear()
        response, _ = self._sign_in(self.team_x, "UADA")
        self.assertIn(f'{oidc.BINDING_COOKIE}=""', response.headers.get_list("set-cookie")[0])
        self.assertNotIn(oidc.BINDING_COOKIE, self.client.cookies)

    def test_guest_of_an_unrouted_team_never_becomes_owner(self):
        team = f"TGST{RUN.upper()}"
        self.slack.users["UGST2"] = SlackUserInfo(id="UGST2", real_name="Guest", team_id=team, is_restricted=True)
        response, _ = self._sign_in(team, "UGST2")
        self.assertEqual(response.headers["location"], "/console/start")
        created = self._mutate("/console/api/orgs", {"name": "Guest Org", "invite_code": self._mint()})
        self.assertEqual((created.status_code, created.json()["role"]), (201, "pending"))
        org_id = created.json()["org"]["id"]
        self.assertEqual(self.client.get("/console/api/whoami").status_code, 403)
        self.assertEqual(self._mutate("/console/api/connectors/fake/sync").status_code, 403)
        self.assertEqual(self.client.get("/console/api/audit").status_code, 403)
        # The install's membership check refuses the guest, so the team is never routed.
        with tenancy.org_context(org_id):
            upsert_workspace(team_id=team, bot_token_enc=encrypt_token("xoxb-g"), bot_user_id="UBOT", team_name="G", installed_by="UGST2")
            self.assertEqual(sessions.slack_membership_refusal(team, "UGST2"), "not_a_member")
        self._age(role_checked_at=0)
        self.assertEqual(self.client.get("/console/api/me").json()["role"], "pending")
        # Even if something routed the team anyway, the next refresh asks Slack and signs them out.
        orgs.route_team(team, org_id)
        self._age(role_checked_at=0)
        self.assertEqual(self.client.get("/console/api/me").status_code, 401)

    def test_member_without_a_person_is_rechecked_with_slack(self):
        me = self._signed_in(self.team_x, "UBEA")
        self.assertIsNone(me["person"]["id"])
        self.slack.users.pop("UBEA")
        self._age(role_checked_at=time.time() - 61)
        self.assertEqual(self.client.get("/console/api/me").status_code, 401)

    def test_member_turned_guest_is_signed_out(self):
        self._signed_in(self.team_x, "UADA")
        self.slack.users["UADA"] = SlackUserInfo(id="UADA", real_name="Ada", team_id=self.team_x, is_restricted=True)
        self._age(role_checked_at=0)
        self.assertEqual(self.client.get("/console/api/me").status_code, 401)

    def test_non_ascii_secrets_are_refused_not_crashed(self):
        self._signed_in(self.team_x, "UADA")
        self.assertEqual(self._mutate("/console/api/ping", x_csrf_token="caf\xe9".encode("latin-1")).status_code, 403)
        bearer = {"authorization": "Bearer caf\xe9".encode("latin-1")}
        self.assertEqual(TestClient(_test_app(), base_url=ORIGIN).get("/console/api/whoami", headers=bearer).status_code, 401)
        self.assertEqual(self.client.post("/ops/invites", headers=bearer).status_code, 403)

    def test_invite_misuse(self):
        used = self._mint()
        self._sign_in(f"TA{RUN.upper()}", "UA")
        self.assertEqual(self._mutate("/console/api/orgs", {"name": "First", "invite_code": used}).status_code, 201)

        self.client.cookies.clear()
        self._sign_in(f"TB{RUN.upper()}", "UB")
        expired = self._mint()
        tenancy.control_graph().query("MATCH (i:InviteCode {code_hash: $h}) SET i.expires_at = 1", {"h": hashlib.sha256(expired.encode()).hexdigest()})
        for code in (used, expired, "brain-made-up", "", "x" * 500):
            with self.subTest(code=code[:20]):
                response = self._mutate("/console/api/orgs", {"name": "Second", "invite_code": code})
                self.assertEqual((response.status_code, response.json()), (400, {"error": "invalid_invite"}))
        self.assertEqual(self._mutate("/console/api/orgs", {"name": "x", "invite_code": self._mint()}).status_code, 400)
        stored = tenancy.control_graph().query("MATCH (i:InviteCode) RETURN i.code_hash").result_set
        self.assertNotIn(used, json.dumps(stored))

    def test_failed_org_creation_gives_the_invite_back(self):
        self._sign_in(f"TC{RUN.upper()}", "UC")
        code = self._mint()
        with mock.patch.object(orgs, "create_org", side_effect=RuntimeError("boom")):
            self.assertEqual(self._mutate("/console/api/orgs", {"name": "Crashy", "invite_code": code}).status_code, 500)
        self.assertEqual(self._mutate("/console/api/orgs", {"name": "Crashy", "invite_code": code}).status_code, 201)

    def test_minting_needs_the_admin_token(self):
        self.assertEqual(self.client.post("/ops/invites").status_code, 403)
        self.assertEqual(self.client.post("/ops/invites", headers={"authorization": "Bearer nope"}).status_code, 403)
        body = self.client.post("/ops/invites?ttl_days=3", headers={"authorization": "Bearer admin-token"}).json()
        self.assertTrue(body["code"].startswith("brain-"))
        self.assertAlmostEqual(body["expires_at"] / 1000 - time.time(), 3 * 86400, delta=60)

    # --- isolation and gating ----------------------------------------------------------------

    def test_session_in_org_x_cannot_act_in_org_y(self):
        self._signed_in(self.team_x, "UADA")
        for path in ("/console/api/whoami", f"/console/api/whoami?org={self.org_y}"):
            body = self.client.get(path).json()
            self.assertEqual((body["org"], body["session_org"]), (self.org_x, self.org_x))
            self.assertTrue(body["graph"] == f"org_{self.org_x}")
        # A team routed to org Y signs into org Y, never X, whatever the session before.
        self._signed_in(self.team_y, "UYAN")
        self.assertEqual(self.client.get("/console/api/whoami").json()["org"], self.org_y)
        # Org Y's admin rights don't come from org X's graph.
        with tenancy.org_context(self.org_x):
            roles.set_role("UYAN", roles.ROLE_ADMIN)
        self._age(role_checked_at=0)
        self.assertEqual(self._mutate("/console/api/connectors/fake/sync").status_code, 403)

    def test_a_user_of_one_team_cannot_claim_another_teams_org(self):
        # The id_token's team decides the org; UYAN (team Y) presenting team X is unknown to X's Slack.
        response, _ = self._sign_in(self.team_x, "UYAN")
        self.assertEqual(response.headers["location"], "/signin?error=not_a_member")

    def test_operator_token_picks_the_org(self):
        headers = {"authorization": "Bearer admin-token"}
        self.assertEqual(self.client.get(f"/console/api/whoami?org={self.org_y}", headers=headers).json()["org"], self.org_y)
        self.assertEqual(self.client.post(f"/console/api/connectors/fake/sync?org={self.org_x}", headers=headers).json()["org"], self.org_x)
        self.assertEqual(self.client.get("/console/api/whoami?org=nope", headers=headers).status_code, 404)
        self.assertEqual(self.client.get(f"/console/api/whoami?org={self.org_y}", headers={"authorization": "Bearer wrong"}).status_code, 401)

    def test_everything_is_404_when_the_console_is_off(self):
        off = dataclasses.replace(SETTINGS, console_enabled=False)
        with mock.patch.object(sessions, "settings", off), mock.patch.object(auth, "settings", off):
            for method, path in (("get", "/auth/slack/start"), ("get", "/auth/slack/callback"), ("post", "/auth/logout"),
                                 ("get", "/console/api/me"), ("post", "/console/api/orgs"), ("post", "/ops/invites"),
                                 ("get", "/console/api/whoami")):
                with self.subTest(path=path):
                    response = getattr(self.client, method)(path, headers={"authorization": "Bearer admin-token"})
                    self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
