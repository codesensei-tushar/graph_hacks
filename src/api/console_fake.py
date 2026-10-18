"""A fake console API for building and screenshotting the console pages: dev only, never production.

Mounted by ``console_web.install`` only when ``CONSOLE_ENABLED`` and ``CONSOLE_FIXTURES`` are both
set. It answers every ``/console/api/*`` route in the spec's shapes from the JSON in
``tests/fixtures/console/``, keeps what the pages change in memory, and fakes the two redirects
the pages leave for (Slack sign-in and a vendor's OAuth screen), so every page and every state can
be clicked through locally. ``/console/api/ask`` asks the real graph (``ask_api``), so the ask page
draws real answers.

A scenario is picked with ``GET /console/_fixture/<name>`` (a cookie, so the pages themselves carry
no fixture code): ``default``, ``attention``, ``empty``, ``member``, ``new``, ``error`` and
``signedout``. Each switch starts that scenario from its fixtures again.

Backfills move forward a little on every read of the connections, so the 10-second polling can be
watched: a container finishes, the count goes up, and the pill turns "Up to date".
"""

import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from src.domain.knowledge import ask_api

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "console"
SCENARIOS = ("default", "attention", "empty", "member", "new", "error", "signedout")
COOKIE = "console_fixture"
AGO = re.compile(r"^-(\d+)s$")

router = APIRouter(include_in_schema=False)
_state: dict[str, dict] = {}


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


def _when(value, now: datetime):
    """A fixture's "-180s" is 180 seconds before the scenario started."""
    match = AGO.match(value) if isinstance(value, str) else None
    return (now - timedelta(seconds=int(match.group(1)))).isoformat().replace("+00:00", "Z") if match else value


def _stamp(node, now: datetime):
    if isinstance(node, list):
        return [_stamp(item, now) for item in node]
    if isinstance(node, dict):
        return {key: _stamp(value, now) for key, value in node.items()}
    return _when(node, now)


def _fresh(scenario: str) -> dict:
    now = datetime.now(UTC)
    me = _load("me.json")
    connections = _load("connections-attention.json" if scenario == "attention" else "connections.json")
    if scenario == "member":
        me["role"] = "member"
        me["person"] = {"id": "person-maya-chen", "name": "Maya Chen"}
    if scenario == "new":
        me["org"] = None
        me["role"] = None
        me["slack_connected"] = False
    if scenario in ("empty", "new"):
        connections = []
    return {"scenario": scenario, "me": me, "connections": _stamp(connections, now), "removing": {}, "seq": 0}


def _scenario(request: Request) -> dict:
    name = request.cookies.get(COOKIE) or "default"
    name = name if name in SCENARIOS else "default"
    if name not in _state:
        _state[name] = _fresh(name)
    return _state[name]


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _denied(request: Request, state: dict, admin: bool = False, mutation: bool = False) -> JSONResponse | None:
    if state["scenario"] == "signedout":
        return JSONResponse({"detail": "signed_out"}, status_code=401)
    # The real API's codes and shapes (src/domain/console/session.py, csrf.py).
    if mutation and request.headers.get("x-csrf-token") != state["me"]["csrf"]:
        return JSONResponse({"detail": "bad_csrf_token"}, status_code=403)
    if admin and state["me"]["org"] and state["me"]["role"] == "pending":
        return JSONResponse({"detail": "slack_install_pending"}, status_code=403)
    if admin and state["me"]["role"] not in ("owner", "admin"):
        return JSONResponse({"detail": "admin_only"}, status_code=403)
    return None


def _find(state: dict, connection_id: str) -> dict | None:
    return next((c for c in state["connections"] if c["id"] == connection_id), None)


def _advance(state: dict) -> None:
    """Each read moves every backfill on by a few containers, and finishes a disconnect."""
    for connection in state["connections"]:
        pending = [c for c in connection["containers"] if not c["backfilled"]]
        for container in pending[:3]:
            container["backfilled"] = True
            container["items"] += 40 + len(container["name"]) * 3
            container["last_fetch_at"] = _now()
        if pending:
            connection["items"] = sum(c["items"] for c in connection["containers"])
            connection["last_sync_at"] = _now()
    for connection_id, reads in list(state["removing"].items()):
        if reads >= 2:
            state["connections"] = [c for c in state["connections"] if c["id"] != connection_id]
            del state["removing"][connection_id]
        else:
            state["removing"][connection_id] = reads + 1


@router.get("/console/_fixture/{name}")
def pick(name: str, next: str = "/console"):
    if name not in SCENARIOS:
        return JSONResponse({"error": f"scenarios: {', '.join(SCENARIOS)}"}, status_code=404)
    _state[name] = _fresh(name)
    target = next if next.startswith("/") and not next.startswith("//") else "/console"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(COOKIE, name, httponly=True, samesite="lax")
    return response


@router.get("/auth/slack/start")
def slack_start(request: Request):
    state = _scenario(request)
    return RedirectResponse("/console/start" if state["me"]["org"] is None else "/console", status_code=303)


@router.post("/auth/logout")
def logout():
    return Response(status_code=204)


@router.get("/console/api/me")
def me(request: Request):
    state = _scenario(request)
    return _denied(request, state) or state["me"]


@router.post("/console/api/orgs")
async def create_org(request: Request):
    state = _scenario(request)
    if denied := _denied(request, state, mutation=True):
        return denied
    body = await request.json()
    name, code = (body.get("name") or "").strip(), (body.get("invite_code") or "").strip()
    if len(name) < 2:
        return JSONResponse({"error": "bad_name"}, status_code=400)
    if state["me"]["org"]:
        return JSONResponse({"error": "already_in_org"}, status_code=409)
    if code.upper() != "BETA-QUILL-2026":
        return JSONResponse({"error": "invalid_invite"}, status_code=400)
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    # As the real route: the creator is "pending" until the Slack install confirms them, and the
    # answer is the whole session again, rotated, with a new CSRF token.
    state["me"].update(org={"id": "org_01JBFAKE00", "name": name, "slug": slug}, role="pending", csrf="fixture-csrf-token-2")
    return JSONResponse(state["me"], status_code=201)


@router.get("/slack/oauth/install")
def slack_install(request: Request):
    """Stands in for Slack's install screen: the install confirms the creator as owner."""
    state = _scenario(request)
    if state["me"]["org"]:
        state["me"].update(role="owner", slack_connected=True)
    return RedirectResponse("/console/start", status_code=303)


@router.get("/console/api/connections")
def connections(request: Request):
    state = _scenario(request)
    if denied := _denied(request, state):
        return denied
    if state["scenario"] == "error":
        return JSONResponse({"error": "the graph is unavailable"}, status_code=503)
    _advance(state)
    return state["connections"]


@router.post("/console/api/connectors/{source}/connect")
def connect(source: str, request: Request):
    state = _scenario(request)
    if denied := _denied(request, state, admin=True, mutation=True):
        return denied
    if source not in ("jira", "hubspot"):
        return JSONResponse({"error": "unknown source"}, status_code=404)
    return {"authorize_url": f"/console/_fake/oauth/{source}"}


@router.get("/console/_fake/oauth/{source}")
def vendor_screen(source: str, request: Request):
    """Stands in for the vendor's consent screen and the callback that follows it."""
    state = _scenario(request)
    state["seq"] += 1
    stamp = _now()
    if source == "jira":
        connection = {
            "id": f"jira:01JBNEW{state['seq']:03d}",
            "source": "jira",
            "status": "connecting",
            "account": None,
            "site": None,
            "auth": "oauth",
            "created_by": state["me"]["person"]["name"],
            "created_at": stamp,
            "items": 0,
            "last_sync_at": None,
            "last_error": None,
            "containers": [],
        }
        state["connections"].append(connection)
        return RedirectResponse(f"/console/connect/jira/site?id={connection['id']}", status_code=303)
    connection = {
        "id": f"hubspot:01JBNEW{state['seq']:03d}",
        "source": "hubspot",
        "status": "active",
        "account": "Quillfern",
        "site": "quillfern.com",
        "auth": "oauth",
        "created_by": state["me"]["person"]["name"],
        "created_at": stamp,
        "items": 0,
        "last_sync_at": None,
        "last_error": None,
        "containers": [
            {
                "id": f"hubspot:01JBNEW{state['seq']:03d}:container:44012345",
                "name": "HubSpot portal 44012345",
                "items": 0,
                "backfilled": False,
                "capped": False,
                "readers": None,
                "available": None,
                "last_fetch_at": None,
                "last_error": None,
            }
        ],
    }
    state["connections"].append(connection)
    return RedirectResponse(f"/console/connectors/{connection['id']}?connected=1", status_code=303)


@router.get("/console/api/connections/{connection_id}/sites")
def sites(connection_id: str, request: Request):
    state = _scenario(request)
    if denied := _denied(request, state, admin=True):
        return denied
    return _load("sites.json") if _find(state, connection_id) else JSONResponse({"error": "not found"}, status_code=404)


@router.post("/console/api/connections/{connection_id}/site")
async def site(connection_id: str, request: Request):
    state = _scenario(request)
    if denied := _denied(request, state, admin=True, mutation=True):
        return denied
    connection = _find(state, connection_id)
    cloud_id = (await request.json()).get("cloud_id")
    chosen = next((s for s in _load("sites.json") if s["cloud_id"] == cloud_id), None)
    if not connection or not chosen:
        return JSONResponse({"error": "not found"}, status_code=404)
    connection.update(site=chosen["url"], account=chosen["name"])
    return {"ok": True}


@router.get("/console/api/connections/{connection_id}/available")
def available(connection_id: str, request: Request):
    state = _scenario(request)
    if denied := _denied(request, state, admin=True):
        return denied
    connection = _find(state, connection_id)
    if not connection:
        return JSONResponse({"error": "not found"}, status_code=404)
    time.sleep(0.3)  # a vendor call: the page shows its loading state
    return _load("available.json") if connection["source"] == "jira" else [{"external_id": "44012345", "key": "44012345", "name": "HubSpot portal 44012345"}]


@router.put("/console/api/connections/{connection_id}/containers")
async def containers(connection_id: str, request: Request):
    state = _scenario(request)
    if denied := _denied(request, state, admin=True, mutation=True):
        return denied
    connection = _find(state, connection_id)
    if not connection:
        return JSONResponse({"error": "not found"}, status_code=404)
    selected = [str(s) for s in (await request.json()).get("selected", [])]
    names = {a["external_id"]: a["name"] for a in _load("available.json")}
    kept = {c["id"]: c for c in connection["containers"]}
    connection["containers"] = [
        kept.get(f"{connection_id}:container:{s}")
        or {
            "id": f"{connection_id}:container:{s}",
            "name": names.get(s, s),
            "items": 0,
            "backfilled": False,
            "capped": False,
            "readers": None,
            "available": None,
            "last_fetch_at": None,
            "last_error": None,
        }
        for s in selected
    ]
    connection["status"] = "active"
    return {"ok": True}


@router.post("/console/api/connections/{connection_id}/sync")
def sync(connection_id: str, request: Request):
    state = _scenario(request)
    if denied := _denied(request, state, admin=True, mutation=True):
        return denied
    connection = _find(state, connection_id)
    if not connection:
        return JSONResponse({"error": "not found"}, status_code=404)
    connection["last_error"] = None
    if connection["status"] == "error":
        connection["status"] = "active"
    for container in connection["containers"]:
        container["last_error"] = None
    connection["last_sync_at"] = _now()
    return {"ok": True}


@router.delete("/console/api/connections/{connection_id}")
def disconnect(connection_id: str, request: Request):
    state = _scenario(request)
    if denied := _denied(request, state, admin=True, mutation=True):
        return denied
    connection = _find(state, connection_id)
    if not connection:
        return JSONResponse({"error": "not found"}, status_code=404)
    connection["status"] = "removing"
    state["removing"][connection_id] = 0
    return {"ok": True}


@router.post("/console/api/ask")
async def ask(request: Request):
    state = _scenario(request)
    if denied := _denied(request, state, mutation=True):
        return denied
    question = ((await request.json()).get("q") or "").strip()
    if not question or len(question) > ask_api.MAX_QUESTION_CHARS:
        return JSONResponse({"error": f"ask a question of at most {ask_api.MAX_QUESTION_CHARS} characters"}, status_code=400)
    try:
        return ask_api.answer_question(question).to_dict()
    except (ask_api.ToolRefused, ask_api.QuestionTooLong) as refused:
        return JSONResponse({"error": str(refused)}, status_code=400)


@router.get("/console/api/slack")
def slack(request: Request):
    """Proposed, not in the spec yet: the workspace card on Connections."""
    state = _scenario(request)
    if denied := _denied(request, state):
        return denied
    if not state["me"]["org"]:
        return JSONResponse({"detail": "no_org"}, status_code=403)
    return _stamp(_load("slack.json"), datetime.now(UTC))


@router.get("/console/api/audit")
def audit(request: Request):
    state = _scenario(request)
    return _denied(request, state, admin=True) or []


def install(app: FastAPI) -> None:
    app.include_router(router)
