"""Which org a piece of work belongs to, and so which graph it reads and writes.

Every org has its own FalkorDB graph; the control graph (``settings.control_graph``,
``brain_control`` in production) holds the orgs themselves. The legacy org (Quillfern,
graph ``settings.falkordb_graph``, ``company`` in production) serves the public site,
the public ask page and MCP, exactly as before there were orgs.

    with org_context(org):
        connect()            # -> org.graph
        scheduler.schedule() # the job carries org.id and runs inside org_context(org)

``current_org()`` is the id of the org in context, or None. ``current_org_ref()`` is the
org itself (its graph): with none set it falls back to the legacy org:
production logs where that happened (rate-limited), and with ``TENANCY_STRICT`` set (the
tenancy tests) it raises ``NoOrgContext`` instead, so a code path that forgot to set the
org fails loudly rather than reading another org's graph.

Entry points set the context explicitly: the scheduler (per job), the Slack routes (per
team), the console API (per session), background threads and BackgroundTasks (they copy
the context at creation, so set it before ``add_task``/``Thread``, or inside the task).
"""

import functools
import logging
import re
import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token, copy_context
from dataclasses import dataclass

from src.core.config import settings

log = logging.getLogger(__name__)

LEGACY_ORG_ID = "quillfern"
LEGACY_ORG_SLUG = "quillfern"
LEGACY_ORG_NAME = "Quillfern"
# A new org's graph: ``org_<ulid>``, lowercase. Anything else is refused before it reaches FalkorDB.
ORG_GRAPH = re.compile(r"^org_[0-9a-z]{26}$")
ORG_ID = re.compile(r"^[0-9a-z_]{1,64}$")


class NoOrgContext(RuntimeError):
    """A graph call ran with no org in context while ``TENANCY_STRICT`` is set."""


@dataclass(frozen=True)
class OrgRef:
    """An org as the request or job carries it. ``graph`` is its FalkorDB graph."""

    id: str
    graph: str
    slug: str = ""
    name: str = ""
    legacy: bool = False


def legacy_org() -> OrgRef:
    """The legacy org. Its graph is always ``settings.falkordb_graph`` (``company`` in production;
    the test graph in tests), whatever the control graph's node says."""
    return OrgRef(id=LEGACY_ORG_ID, graph=settings.falkordb_graph, slug=LEGACY_ORG_SLUG, name=LEGACY_ORG_NAME, legacy=True)


_current: ContextVar[OrgRef | None] = ContextVar("brain_org", default=None)
_last_warned = 0.0
_WARN_EVERY_SECONDS = 60.0

CONTROL_GRAPH = settings.control_graph


def control_graph():
    """The control graph (orgs, routes, external accounts, invite codes, audit). Tests point it at
    ``brain_control_test<TEST_GRAPH_SUFFIX>`` through ``BRAIN_CONTROL_GRAPH``."""
    from src.db.falkordb_client import client

    return client().select_graph(CONTROL_GRAPH)


def current_org_or_none() -> OrgRef | None:
    """The org in context, or None (no fallback, no log)."""
    return _current.get()


def current_org() -> str | None:
    """The id of the org in context, or None when no org is set."""
    org = _current.get()
    return org.id if org is not None else None


def current_org_ref() -> OrgRef:
    """The org in context; with none, the legacy org (logged), or ``NoOrgContext`` when strict."""
    org = _current.get()
    if org is not None:
        return org
    if settings.tenancy_strict:
        raise NoOrgContext("no org in context: wrap the call in tenancy.org_context(org)")
    global _last_warned
    now = time.monotonic()
    if now - _last_warned > _WARN_EVERY_SECONDS:
        _last_warned = now
        log.info("[tenancy] graph call with no org in context; using the legacy org", stack_info=log.isEnabledFor(logging.DEBUG))
    return legacy_org()


def is_legacy(org: "OrgRef | str | None" = None) -> bool:
    if org is None:
        org = current_org_ref()
    return (org if isinstance(org, str) else org.id) == LEGACY_ORG_ID or (isinstance(org, OrgRef) and org.legacy)


def resolve(org: "OrgRef | str") -> OrgRef:
    """An OrgRef for an org id (looked up in the control graph) or an OrgRef as given.
    Raises ``LookupError`` for an unknown id."""
    if isinstance(org, OrgRef):
        return org
    if not isinstance(org, str) or not org:
        raise TypeError("org_context takes an org id or an OrgRef")
    if org == LEGACY_ORG_ID:
        return legacy_org()
    from src.domain.orgs import get_org_ref

    ref = get_org_ref(org)
    if ref is None:
        raise LookupError(f"no such org: {org!r}")
    return ref


@contextmanager
def org_context(org: "OrgRef | str") -> Iterator[OrgRef]:
    """Run the block as ``org`` (an id or an OrgRef). Nests; the previous org comes back on exit."""
    token = set_org(org)
    try:
        yield _current.get()
    finally:
        reset_org(token)


def set_org(org: "OrgRef | str") -> Token:
    """For a FastAPI dependency or middleware that cannot use a ``with`` block. Pair with ``reset_org``."""
    org = resolve(org)
    if not org.legacy and not ORG_GRAPH.match(org.graph):
        raise ValueError(f"refusing org graph name {org.graph!r}")
    return _current.set(org)


def reset_org(token: Token) -> None:
    _current.reset(token)


def bind(fn: Callable) -> Callable:
    """``fn`` to run later in this context (and so this org): for ``threading.Thread(target=...)``,
    which, unlike asyncio and Starlette's thread pool, does not carry context variables over."""
    context = copy_context()
    return functools.partial(context.run, fn)


def in_org(org: "OrgRef | str", fn: Callable, *args, **kwargs):
    """Call ``fn`` inside ``org_context(org)``: a BackgroundTask runs after the request's context
    is gone, so pass ``background.add_task(in_org, org, fn, ...)``."""
    with org_context(org):
        return fn(*args, **kwargs)


_CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"


def new_ulid() -> str:
    """A lowercase ULID: 48 bits of milliseconds then 80 random bits, Crockford base32 (26 chars)."""
    value = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    return "".join(_CROCKFORD[(value >> (5 * index)) & 31] for index in reversed(range(26)))


def new_org_graph() -> str:
    return f"org_{new_ulid()}"
