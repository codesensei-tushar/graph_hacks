"""Which org each installation of the GitHub App feeds, in the control graph.

    (:GitHubInstallation {id: '<installation id>', org, account_id, account_login, account_type,
                          status: 'active'|'removing'|'disconnected', backfill_limit, suspended,
                          connected_by, created_at, updated_at})-[:OF]->(:Org)
    (:ExternalAccount {key: 'github:<account id>'})-[:OF]->(:Org)      one org per GitHub account

A company connects from the console (``console_github``: GitHub's install page, then a GitHub
sign-in that proves the person can reach the installation) or the operator binds one with the
admin token (``/ops/github/installations``). Webhooks are routed by ``route``:

- a bound, active installation: its org;
- one being removed or disconnected (a tombstone, kept so its events stay dropped): nothing;
- an unknown installation, with the app set by env (the legacy deployment): the legacy org,
  bound on first sight, exactly as before there were orgs. Not while a console Connect is in
  flight (``connect_started``), so a company's brand-new installation cannot land in the legacy
  graph before its own callback binds it: its ``installation.created`` is checked again later;
- an unknown installation of a registered (stored) app: nothing, until someone connects it.
"""

import logging
import time

from src.core import tenancy
from src.db.falkordb_client import kv
from src.domain import orgs

log = logging.getLogger(__name__)

ACTIVE, REMOVING, DISCONNECTED = "active", "removing", "disconnected"
INFLIGHT_KEY = "github:connect:inflight"
INFLIGHT_SECONDS = 15 * 60
_FIELDS = ("id", "org", "account_id", "account_login", "account_type", "status", "backfill_limit", "suspended", "connected_by", "created_at", "updated_at")
_RETURN = ", ".join(f"i.{name}" for name in _FIELDS)


def _now_ms() -> int:
    return int(time.time() * 1000)


def account_key(account_id) -> str:
    return f"github:{account_id}"


def get(installation_id) -> dict | None:
    rows = tenancy.control_graph().query(f"MATCH (i:GitHubInstallation {{id: $id}}) RETURN {_RETURN}", {"id": str(installation_id)}).result_set
    return dict(zip(_FIELDS, rows[0], strict=True)) if rows else None


def list_for_org(org_id: str) -> list[dict]:
    rows = tenancy.control_graph().query(
        f"MATCH (i:GitHubInstallation {{org: $org}}) WHERE i.status IN ['active', 'removing'] RETURN {_RETURN} ORDER BY i.created_at", {"org": org_id}
    ).result_set
    return [dict(zip(_FIELDS, row, strict=True)) for row in rows]


def orgs_with_installations() -> list[str]:
    rows = tenancy.control_graph().query("MATCH (i:GitHubInstallation {status: 'active'}) RETURN DISTINCT i.org").result_set
    return [row[0] for row in rows if row[0]]


class Claimed(Exception):
    """The GitHub account, or the installation, already feeds another org."""


def bind(installation_id, org_id: str, account: dict, connected_by: str, backfill_limit: int | None = None) -> dict:
    """Bind an installation to ``org_id`` and claim its GitHub account for that org. Raises ``Claimed``."""
    installation_id = str(installation_id)
    account_id = str(account.get("id") or "")
    current = get(installation_id)
    if current and current["status"] in (ACTIVE, REMOVING) and current["org"] != org_id:
        raise Claimed()
    if current and current["status"] == REMOVING:
        raise Claimed()
    if org_id == tenancy.LEGACY_ORG_ID:
        orgs.ensure_legacy_org()
    newly = False
    if account_id:
        newly = orgs.external_account_org(account_key(account_id)) is None
        if not orgs.claim_external_account(account_key(account_id), org_id):
            raise Claimed()
    try:
        tenancy.control_graph().query(
            """
            MATCH (o:Org {id: $org})
            MERGE (i:GitHubInstallation {id: $id})
            ON CREATE SET i.created_at = $now
            SET i.org = $org, i.account_id = $account_id, i.account_login = $login, i.account_type = $type, i.status = 'active',
                i.connected_by = $by, i.updated_at = $now, i.suspended = false,
                i.backfill_limit = coalesce($limit, i.backfill_limit)
            WITH i, o
            OPTIONAL MATCH (i)-[old:OF]->(:Org)
            DELETE old
            WITH i, o
            MERGE (i)-[:OF]->(o)
            """,
            {
                "id": installation_id, "org": org_id, "account_id": account_id or None, "login": str(account.get("login") or account.get("slug") or "") or None,
                "type": str(account.get("type") or "") or None, "by": connected_by, "now": _now_ms(), "limit": backfill_limit,
            },
        )
    except Exception:
        if newly:
            orgs.release_external_account(account_key(account_id), org_id)
        raise
    log.info("[github] installation=%s bound to org=%s account=%s", installation_id, org_id, account.get("login"))
    return get(installation_id)


def set_status(installation_id, status: str) -> None:
    tenancy.control_graph().query(
        "MATCH (i:GitHubInstallation {id: $id}) SET i.status = $status, i.updated_at = $now", {"id": str(installation_id), "status": status, "now": _now_ms()}
    )


def set_limit(installation_id, limit: int) -> None:
    tenancy.control_graph().query("MATCH (i:GitHubInstallation {id: $id}) SET i.backfill_limit = $limit", {"id": str(installation_id), "limit": limit})


def set_suspended(installation_id, suspended: bool) -> None:
    tenancy.control_graph().query("MATCH (i:GitHubInstallation {id: $id}) SET i.suspended = $s", {"id": str(installation_id), "s": suspended})


def finish_removal(installation_id, forget: bool) -> None:
    """After its data is gone: release the account (unless another installation of it still feeds the
    org), then keep a tombstone (a console disconnect: the app is still installed on GitHub, so its
    events must stay dropped) or forget it entirely (GitHub uninstalled it)."""
    current = get(installation_id)
    if current is None:
        return
    if current["account_id"]:
        others = tenancy.control_graph().query(
            "MATCH (i:GitHubInstallation {account_id: $account, org: $org, status: 'active'}) WHERE i.id <> $id RETURN count(i)",
            {"account": current["account_id"], "org": current["org"], "id": str(installation_id)},
        ).result_set
        if not (others and others[0][0]):
            orgs.release_external_account(account_key(current["account_id"]), current["org"])
    if forget:
        tenancy.control_graph().query("MATCH (i:GitHubInstallation {id: $id}) DETACH DELETE i", {"id": str(installation_id)})
    else:
        tenancy.control_graph().query(
            "MATCH (i:GitHubInstallation {id: $id}) OPTIONAL MATCH (i)-[r:OF]->() DELETE r SET i.status = 'disconnected', i.updated_at = $now",
            {"id": str(installation_id), "now": _now_ms()},
        )


def forget(installation_id) -> None:
    tenancy.control_graph().query("MATCH (i:GitHubInstallation {id: $id}) DETACH DELETE i", {"id": str(installation_id)})


# --- a console Connect in flight (env app only matters) ------------------------------------------


def connect_started(token: str) -> None:
    kv().zadd(INFLIGHT_KEY, {token: time.time() + INFLIGHT_SECONDS})


def connect_finished(token: str) -> None:
    kv().zrem(INFLIGHT_KEY, token)


def connect_in_flight() -> bool:
    now = time.time()
    kv().zremrangebyscore(INFLIGHT_KEY, 0, now)
    return bool(kv().zcount(INFLIGHT_KEY, now, "+inf"))


# --- routing a webhook ---------------------------------------------------------------------------


def _account_of(payload: dict) -> dict:
    installation = payload.get("installation") or {}
    if installation.get("account"):
        return installation["account"]
    owner = (payload.get("repository") or {}).get("owner") or {}
    return owner if owner.get("id") else {}


def route(event: str, payload: dict, env_app: bool) -> tuple[tenancy.OrgRef | None, str]:
    """Where a verified delivery runs: ``(org, reason)``, org None when it is dropped (reason says why)."""
    installation_id = (payload.get("installation") or {}).get("id")
    if installation_id is None:
        return None, "no_installation"
    current = get(installation_id)
    action = payload.get("action") or ""
    if current is not None:
        if current["status"] == ACTIVE:
            ref = orgs.get_org_ref(current["org"]) if current["org"] else None
            return (ref, "bound") if ref is not None else (None, "org_gone")
        if event == "installation" and action == "deleted" and current["status"] == DISCONNECTED:
            forget(installation_id)
            return None, "tombstone_forgotten"
        return None, current["status"]
    if not env_app:
        return None, "unbound"
    if event == "installation" and action == "deleted":
        return None, "unbound"
    if connect_in_flight():
        return None, "connect_in_flight"
    try:
        bind(installation_id, tenancy.LEGACY_ORG_ID, _account_of(payload), connected_by="env-app")
    except Claimed:
        return None, "claimed"
    return tenancy.legacy_org(), "legacy"
