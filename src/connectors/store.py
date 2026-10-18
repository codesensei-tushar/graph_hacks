"""Connector installs made in the console, stored in the org's own graph.

    (:ConnectorInstall {id: '<source>:<install>', source, install, org, auth: 'oauth'|'token'|'env',
                        status: 'connecting'|'active'|'error'|'removing', external_account, site,
                        config_json, secret_enc, key_id, last_error, created_by, created_at,
                        updated_at, scope: 'system'})

Every call works on the graph of the org in context (``tenancy.org_context``). ``config_json``
holds the fields that are not secret (the selected projects, a site URL); ``secret_enc`` holds
the credentials as MultiFernet over JSON ``{org, install, ...tokens}``. ``org`` and ``install``
are checked on decrypt, so a value copied onto another install or into another org's graph is
refused. Secrets are never stored unencrypted (``allow_plaintext=False``), never returned by
``get``/``list_installs``, and never logged: only ``load_secrets`` and ``to_install`` read them.

Env installs (``CONNECTOR_*``) are not stored here; they stay the legacy org's ``default``.
"""

import json
import logging
import re
import secrets as _secrets
import time
from dataclasses import dataclass, field

from src.connectors.base import Install
from src.core import tenancy
from src.db.falkordb_client import connect
from src.domain.slack.token_crypto import current_key_id, decrypt_token, encrypt_token

log = logging.getLogger(__name__)

AUTHS = ("oauth", "token", "env")
STATUSES = ("connecting", "active", "error", "removing")
STATE_SCOPE = "system"
_INSTALL_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_FIELDS = (
    "id", "source", "install", "org", "auth", "status", "external_account", "site",
    "config_json", "key_id", "last_error", "created_by", "created_at", "updated_at",
)


@dataclass(frozen=True)
class ConnectorInstall:
    """A stored install, without its secrets."""

    id: str  # '<source>:<install>', the same as Install.key
    source: str
    install: str
    org: str
    auth: str
    status: str
    external_account: str | None = None  # 'jira:<cloudId>', 'hubspot:<hubId>'
    site: str | None = None
    config: dict = field(default_factory=dict)
    key_id: str | None = None
    last_error: str | None = None
    created_by: str | None = None
    created_at: int | None = None
    updated_at: int | None = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def new_install_id() -> str:
    return _secrets.token_hex(6)


def _row(values) -> ConnectorInstall:
    row = dict(zip(_FIELDS, values, strict=True))
    try:
        config = json.loads(row.pop("config_json") or "{}")
    except ValueError:
        config = {}
    return ConnectorInstall(config=config if isinstance(config, dict) else {}, **row)


_RETURN = ", ".join(f"c.{name}" for name in _FIELDS)


def ensure_indexes() -> None:
    _, graph = connect()
    try:
        graph.query("CREATE INDEX FOR (n:ConnectorInstall) ON (n.id)")
    except Exception as error:
        if "already" not in str(error).lower():
            raise


def create(
    source: str,
    *,
    auth: str,
    created_by: str | None,
    install: str | None = None,
    config: dict | None = None,
    secrets: dict | None = None,
    status: str = "connecting",
    external_account: str | None = None,
    site: str | None = None,
) -> ConnectorInstall:
    """A new install of ``source`` in the org in context. Raises ``ValueError`` if the id is taken."""
    if auth not in AUTHS or status not in STATUSES:
        raise ValueError("bad auth or status")
    install = install or new_install_id()
    if not _INSTALL_ID.match(install) or not re.fullmatch(r"[a-z][a-z0-9]*", source or ""):
        raise ValueError("bad source or install id")
    org = tenancy.current_org_ref().id
    key = f"{source}:{install}"
    secret_enc = _encrypt(org, key, secrets) if secrets else None
    _, graph = connect()
    created = graph.query(
        """
        MERGE (c:ConnectorInstall {id: $id})
        ON CREATE SET c.source = $source, c.install = $install, c.org = $org, c.auth = $auth, c.status = $status,
            c.external_account = $external_account, c.site = $site, c.config_json = $config,
            c.secret_enc = $secret, c.key_id = $key_id, c.created_by = $created_by,
            c.created_at = $now, c.updated_at = $now, c.scope = $scope, c.fresh = $nonce
        WITH c
        RETURN c.fresh = $nonce
        """,
        {
            "id": key, "source": source, "install": install, "org": org, "auth": auth, "status": status,
            "external_account": external_account, "site": site, "config": json.dumps(config or {}),
            "secret": secret_enc, "key_id": current_key_id() if secret_enc else None, "created_by": created_by,
            "now": _now_ms(), "scope": STATE_SCOPE, "nonce": _secrets.token_hex(8),
        },
    ).result_set
    if not created or not created[0][0]:
        raise ValueError(f"install {key} exists")
    graph.query("MATCH (c:ConnectorInstall {id: $id}) REMOVE c.fresh", {"id": key})
    return get(key)


def get(install_id: str) -> ConnectorInstall | None:
    """The install ``'<source>:<install>'`` in the org in context, without its secrets."""
    _, graph = connect()
    rows = graph.query(f"MATCH (c:ConnectorInstall {{id: $id}}) RETURN {_RETURN}", {"id": install_id}).result_set
    return _row(rows[0]) if rows else None


def list_installs(source: str | None = None) -> list[ConnectorInstall]:
    _, graph = connect()
    rows = graph.query(
        f"MATCH (c:ConnectorInstall) WHERE $source IS NULL OR c.source = $source RETURN {_RETURN} ORDER BY c.created_at, c.id",
        {"source": source},
    ).result_set
    return [_row(row) for row in rows]


def update_config(install_id: str, config: dict, *, site: str | None = None, external_account: str | None = None) -> ConnectorInstall | None:
    """Merge ``config`` (non-secret fields; None removes one) into the install's config."""
    current = get(install_id)
    if current is None:
        return None
    merged = {**current.config, **config}
    merged = {name: value for name, value in merged.items() if value is not None}
    _, graph = connect()
    graph.query(
        """
        MATCH (c:ConnectorInstall {id: $id})
        SET c.config_json = $config, c.updated_at = $now,
            c.site = coalesce($site, c.site), c.external_account = coalesce($external_account, c.external_account)
        """,
        {"id": install_id, "config": json.dumps(merged), "now": _now_ms(), "site": site, "external_account": external_account},
    )
    return get(install_id)


def set_secret(install_id: str, secrets: dict) -> bool:
    """Replace the install's credentials (all of them). Encrypted; refused when no key is set."""
    current = get(install_id)
    if current is None:
        return False
    _, graph = connect()
    graph.query(
        "MATCH (c:ConnectorInstall {id: $id}) SET c.secret_enc = $secret, c.key_id = $key_id, c.updated_at = $now",
        {"id": install_id, "secret": _encrypt(current.org, install_id, secrets), "key_id": current_key_id(), "now": _now_ms()},
    )
    return True


def set_status(install_id: str, status: str, error: str | None = None) -> bool:
    """``error`` is shown in the console: never put a credential or a vendor's raw text in it."""
    if status not in STATUSES:
        raise ValueError(f"bad status {status!r}")
    _, graph = connect()
    rows = graph.query(
        "MATCH (c:ConnectorInstall {id: $id}) SET c.status = $status, c.last_error = $error, c.updated_at = $now RETURN c.id",
        {"id": install_id, "status": status, "error": (error or None) and error[:300], "now": _now_ms()},
    ).result_set
    return bool(rows)


def delete(install_id: str) -> bool:
    _, graph = connect()
    rows = graph.query("MATCH (c:ConnectorInstall {id: $id}) WITH c, c.id AS id DELETE c RETURN id", {"id": install_id}).result_set
    return bool(rows)


# --- secrets -----------------------------------------------------------------------------


def _encrypt(org: str, install_id: str, secrets: dict) -> str:
    body = {**{name: value for name, value in secrets.items() if name not in ("org", "install")}, "org": org, "install": install_id}
    return encrypt_token(json.dumps(body, sort_keys=True), allow_plaintext=False)


def _secret_enc(install_id: str) -> str | None:
    _, graph = connect()
    rows = graph.query("MATCH (c:ConnectorInstall {id: $id}) RETURN c.secret_enc", {"id": install_id}).result_set
    return rows[0][0] if rows else None


def load_secrets(stored: ConnectorInstall) -> dict:
    """The install's credentials. Raises ``ValueError`` when they belong to another org or install."""
    value = _secret_enc(stored.id)
    if not value:
        return {}
    body = json.loads(decrypt_token(value, allow_plaintext=False))
    if body.get("org") != stored.org or body.get("install") != stored.id or stored.org != tenancy.current_org_ref().id:
        raise ValueError(f"credentials of {stored.id} do not belong to this install")
    return {name: value for name, value in body.items() if name not in ("org", "install")}


def to_install(stored: ConnectorInstall) -> Install:
    """The ``Install`` the connector and sync see: config and decrypted secrets, as strings."""
    fields = {name: _text(value) for name, value in stored.config.items()}
    fields.update({name: _text(value) for name, value in load_secrets(stored).items()})
    return Install(source=stored.source, id=stored.install, config=fields, org=stored.org, auth=stored.auth)


def _text(value) -> str:
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return "" if value is None else str(value)


def rewrap_all() -> int:
    """Re-encrypt every install's secrets under the current first key (key rotation). Returns how many changed."""
    from src.domain.slack.token_crypto import rewrap_token

    _, graph = connect()
    changed = 0
    for install_id, value in graph.query("MATCH (c:ConnectorInstall) WHERE c.secret_enc IS NOT NULL RETURN c.id, c.secret_enc").result_set:
        rewrapped = rewrap_token(value, allow_plaintext=False)
        if rewrapped:
            graph.query(
                "MATCH (c:ConnectorInstall {id: $id}) WHERE c.secret_enc = $old SET c.secret_enc = $new, c.key_id = $key_id",
                {"id": install_id, "old": value, "new": rewrapped, "key_id": current_key_id()},
            )
            changed += 1
    return changed


# --- the same, for a caller that names the org (the OAuth callbacks, src/connectors/oauth.py) ---


def get_install(org: str, key: str) -> Install | None:
    """The stored install ``key`` (``'<source>:<install>'``) of ``org``, secrets decrypted, or None."""
    with tenancy.org_context(org):
        stored = get(key)
        return to_install(stored) if stored else None


def read_secret(org: str, key: str) -> dict:
    with tenancy.org_context(org):
        stored = get(key)
        return load_secrets(stored) if stored else {}


def write_secret(org: str, key: str, secret: dict) -> bool:
    with tenancy.org_context(org):
        return set_secret(key, secret)


def create_install(
    org: str,
    source: str,
    install: str | None,
    auth: str,
    status: str = "connecting",
    external_account: str | None = None,
    site: str | None = None,
    config: dict | None = None,
    secret: dict | None = None,
    created_by: str | None = None,
) -> Install:
    with tenancy.org_context(org):
        stored = create(
            source, auth=auth, created_by=created_by, install=install, config=config, secrets=secret,
            status=status, external_account=external_account, site=site,
        )
        return to_install(stored)


def claim_external_account(key: str, org: str) -> bool:
    """Claim ``'jira:<cloudId>'``/``'hubspot:<hubId>'`` for ``org``; False when another org holds it."""
    from src.domain import orgs

    return orgs.claim_external_account(key, org)
