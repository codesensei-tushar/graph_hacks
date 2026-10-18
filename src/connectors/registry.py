"""Which connectors exist, and which are configured.

A connector module calls ``register(TheConnector())`` when imported. ``load()`` imports the
known modules whose source has ``CONNECTOR_<SOURCE>_*`` variables set, and configures an
install for each one whose ``required`` fields are all present. Env installs belong to the
legacy org, one per source, ``default`` unless ``CONNECTOR_<SOURCE>_INSTALL_ID`` names another.

Installs are keyed ``(org, source, install)``. ``load_from_graph(org)`` adds the installs
stored in an org's graph (src/connectors/store.py), ``put`` adds or replaces one, ``drop``
removes one. ``install_for(source)``, ``configure`` and ``unconfigure`` are the legacy org's
one-install-per-source view, as before.
"""

import importlib
import logging
import re
from collections.abc import Mapping

from src.connectors.base import Connector, Install
from src.core.tenancy import LEGACY_ORG_ID

log = logging.getLogger(__name__)

PREFIX = "CONNECTOR_"
# Source name -> the module that registers it.
KNOWN_MODULES = {"jira": "src.connectors.jira", "hubspot": "src.connectors.hubspot"}
_SOURCE = re.compile(r"^[a-z][a-z0-9]*$")
_INSTALL_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_connectors: dict[str, Connector] = {}
# (org, source, install id) -> Install.
_installs: dict[tuple[str, str, str], Install] = {}


def register(connector: Connector) -> Connector:
    if not _SOURCE.match(connector.source or ""):
        raise ValueError(f"connector source must be lowercase letters and digits: {connector.source!r}")
    _connectors[connector.source] = connector
    return connector


def get(source: str) -> Connector | None:
    return _connectors.get(source)


def _check(install: Install) -> None:
    if install.source not in _connectors:
        raise ValueError(f"no connector registered for {install.source!r}")
    if not _INSTALL_ID.match(install.id):
        raise ValueError(f"install id must be letters, digits, '.', '_' or '-': {install.id!r}")


def put(install: Install) -> Install:
    """Add or replace ``install`` under ``(install.org, install.source, install.id)``."""
    _check(install)
    _installs[(install.org, install.source, install.id)] = install
    return install


def drop(org: str, source: str, install_id: str) -> Install | None:
    return _installs.pop((org, source, install_id), None)


def get_install(org: str, source: str, install_id: str) -> Install | None:
    return _installs.get((org, source, install_id))


def installs_of(org: str) -> list[tuple[Connector, Install]]:
    return [(_connectors[key[1]], install) for key, install in sorted(_installs.items()) if key[0] == org and key[1] in _connectors]


def all_installs() -> list[tuple[Connector, Install]]:
    """Every install of every org."""
    return [(_connectors[key[1]], install) for key, install in sorted(_installs.items()) if key[1] in _connectors]


def load_from_graph(org: str) -> list[Install]:
    """Put every active or connecting install stored in ``org``'s graph; drop the org's stored
    installs that are gone. Env installs are left alone. Never logs a secret."""
    from src.connectors import store
    from src.core.tenancy import org_context

    loaded = []
    with org_context(org):
        stored = store.list_installs()
        for row in stored:
            if row.status == "removing" or row.auth == "env":
                continue
            if row.source not in _connectors and row.source in KNOWN_MODULES:
                try:
                    importlib.import_module(KNOWN_MODULES[row.source])
                except Exception:
                    log.exception("[connectors] %s failed to import", row.source)
            if row.source not in _connectors:
                continue
            try:
                loaded.append(put(store.to_install(row)))
            except Exception as error:
                log.warning("[connectors] install %s of org %s not loaded: %s", row.id, org, type(error).__name__)
        keep = {(org, install.source, install.id) for install in loaded}
    for key, install in list(_installs.items()):
        if key[0] == org and install.auth != "env" and key not in keep:
            _installs.pop(key, None)
    return loaded


# --- the legacy org's one install per source ----------------------------------------------


def configure(install: Install) -> Install:
    """Make ``install`` the legacy org's install of its source (``load`` does this from env; tests call it directly)."""
    _check(install)
    for key in [key for key in _installs if key[0] == install.org and key[1] == install.source and _installs[key].auth == "env"]:
        _installs.pop(key)
    return put(install)


def unconfigure(source: str) -> None:
    for key in [key for key in _installs if key[0] == LEGACY_ORG_ID and key[1] == source and _installs[key].auth == "env"]:
        _installs.pop(key)


def install_for(source: str) -> Install | None:
    """The legacy org's env install of ``source``."""
    return next((install for key, install in sorted(_installs.items()) if key[0] == LEGACY_ORG_ID and key[1] == source and install.auth == "env"), None)


def installs() -> list[tuple[Connector, Install]]:
    """The legacy org's env installs, one per source."""
    return [(_connectors[install.source], install) for install in (install_for(source) for source in sorted(_connectors)) if install is not None]


def env_fields(source: str, env: Mapping[str, str]) -> dict[str, str]:
    """``CONNECTOR_JIRA_API_TOKEN=...`` -> ``{"api_token": "..."}`` for source ``jira``. Empty values are skipped."""
    prefix = f"{PREFIX}{source.upper()}_"
    return {key[len(prefix) :].lower(): value for key, value in env.items() if key.startswith(prefix) and value.strip()}


def install_from_env(connector: Connector, env: Mapping[str, str]) -> Install | None:
    fields = env_fields(connector.source, env)
    if not fields:
        return None
    missing = [name for name in connector.required if not fields.get(name, "").strip()]
    if missing:
        # Names only: a value never reaches the log.
        log.warning("[connectors] %s is not configured; missing %s", connector.source, ", ".join(f"{PREFIX}{connector.source.upper()}_{name.upper()}" for name in missing))
        return None
    return Install(source=connector.source, id=fields.pop("install_id", "default").strip(), config=fields)


def load(env: Mapping[str, str]) -> list[Install]:
    """Import the connectors that have variables set and configure each complete install."""
    sources = {key[len(PREFIX) :].split("_", 1)[0].lower() for key, value in env.items() if key.startswith(PREFIX) and value.strip()}
    for source in sorted(sources & KNOWN_MODULES.keys()):
        if source in _connectors:
            continue
        try:
            importlib.import_module(KNOWN_MODULES[source])
        except ModuleNotFoundError as error:
            if error.name != KNOWN_MODULES[source]:
                log.exception("[connectors] %s failed to import", source)
            else:
                log.warning("[connectors] %s variables are set but its connector is not in this build", source)
        except Exception:
            log.exception("[connectors] %s failed to import", source)
    configured = []
    for source in sorted(sources):
        connector = _connectors.get(source)
        if connector is None:
            continue
        install = install_from_env(connector, env)
        if install is not None:
            configured.append(configure(install))
            log.info("[connectors] %s configured install=%s", source, install.id)
    return configured
