"""Which connectors exist, and which are configured.

A connector module calls ``register(TheConnector())`` when imported. ``load()`` imports the
known modules whose source has ``CONNECTOR_<SOURCE>_*`` variables set, and configures an
install for each one whose ``required`` fields are all present. v1 has one install per
source, ``default`` unless ``CONNECTOR_<SOURCE>_INSTALL_ID`` names another.
"""

import importlib
import logging
import re
from collections.abc import Mapping

from src.connectors.base import Connector, Install

log = logging.getLogger(__name__)

PREFIX = "CONNECTOR_"
# Source name -> the module that registers it.
KNOWN_MODULES = {"jira": "src.connectors.jira", "hubspot": "src.connectors.hubspot"}
_SOURCE = re.compile(r"^[a-z][a-z0-9]*$")
_INSTALL_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_connectors: dict[str, Connector] = {}
_installs: dict[str, Install] = {}


def register(connector: Connector) -> Connector:
    if not _SOURCE.match(connector.source or ""):
        raise ValueError(f"connector source must be lowercase letters and digits: {connector.source!r}")
    _connectors[connector.source] = connector
    return connector


def get(source: str) -> Connector | None:
    return _connectors.get(source)


def configure(install: Install) -> Install:
    """Make ``install`` the install of its source (``load`` does this from env; tests call it directly)."""
    if install.source not in _connectors:
        raise ValueError(f"no connector registered for {install.source!r}")
    if not _INSTALL_ID.match(install.id):
        raise ValueError(f"install id must be letters, digits, '.', '_' or '-': {install.id!r}")
    _installs[install.source] = install
    return install


def unconfigure(source: str) -> None:
    _installs.pop(source, None)


def install_for(source: str) -> Install | None:
    return _installs.get(source)


def installs() -> list[tuple[Connector, Install]]:
    return [(_connectors[source], install) for source, install in sorted(_installs.items()) if source in _connectors]


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
    sources = {key[len(PREFIX) :].split("_", 1)[0].lower() for key in env if key.startswith(PREFIX)}
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
