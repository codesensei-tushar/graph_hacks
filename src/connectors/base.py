"""The contract every external data source (Jira, HubSpot, ...) plugs into.

A connector only reads its source and emits the normalised records below. It never
writes the graph, never formats Cypher, and never creates a ``Person``: ``graph.py``
is the one writer, ``sync.py`` drives it one page per scheduler step, and the webhook
route hands it what a signed delivery carried.

    (:Container {id, name, scope})                          a Jira project, a HubSpot portal
    (item:Ticket|Service|Document)-[:IN_CONTAINER]->(:Container)
    (item)-[:ABOUT]->(:Service)                             from ``Item.about`` hints, resolved by graph.py
    (:ExternalUser)-[:IS]->(:Person)                        only when exactly one Person has the email
    (:ExternalUser)-[:OPENED|ASSIGNED]->(item)
    (:Person or :ExternalUser)-[:OWNS|WORKED_ON]->(item)    from the linked Person when there is one
    (item)-[:BLOCKS]->(:Ticket)                             a path edge, never part of DEPENDS_ON
    (:ExternalUser)-[:CAN_READ]->(:Container)               ``readers()``, restricted containers only
    (:PullRequest)-[:REFERENCES]->(:Ticket)                 ``graph.link_references``

Everything a connector writes is ``org_internal``: every Slack asker reads it, and the
public ask page, the team pages and MCP never do. A ``restricted`` container gets its
own scope instead, readable only in the DMs of people whose linked account is one of
its ``readers()``.

Ids are built by the ``Install`` helpers, ``<source>:<install>:<external id>`` for an
item, and ``graph.py`` refuses any id outside its install, so a connector can never
touch a seeded or team-imported node. Additive changes only from here on.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

ORG_INTERNAL = "org_internal"

# Labels an item may take. A Jira issue is a Ticket; a CRM company is a Service with kind 'account'.
ITEM_LABELS = frozenset({"Ticket", "Service", "Document"})
# Relationship types a connector may emit as ``Edge``. ABOUT to a Service outside the install goes
# through ``Item.about``; IN_CONTAINER, CAN_READ and REFERENCES are written by graph.py itself.
EDGE_RELS = frozenset({"OPENED", "ASSIGNED", "OWNS", "WORKED_ON", "ABOUT", "BLOCKS"})
# OWNS and WORKED_ON from an ExternalUser are written from its linked Person when it has one,
# so who_owns, if_person_leaves and the team pages see them like any other ownership.
PERSON_RELS = frozenset({"OWNS", "WORKED_ON"})
STATES = ("open", "closed")


class ConnectorError(RuntimeError):
    """A source call failed. The message is logged and shown on the status route, so it never holds a credential."""


class RetryLater(ConnectorError):
    """The source asked to wait (429, Retry-After, a long 503). The step is retried after ``seconds``; nothing advances."""

    def __init__(self, seconds: float, message: str = "rate limited"):
        super().__init__(f"{message}; retry in {int(seconds)}s")
        self.seconds = float(seconds)


class CredentialsRejected(ConnectorError):
    """401: the token is wrong, revoked or expired. Sync backs off for an hour and the status route says so."""


@dataclass(frozen=True)
class Install:
    """One configured connection to a source. v1 builds it from ``CONNECTOR_<SOURCE>_<FIELD>`` env vars.

    ``config`` holds every such field, lowercased (``CONNECTOR_JIRA_API_TOKEN`` -> ``api_token``),
    secrets included. It lives in memory only: it is never written to the graph, logged, or
    returned by a route, and it is left out of ``repr``.
    """

    source: str
    id: str = "default"
    config: Mapping[str, str] = field(default_factory=dict, repr=False, compare=False, hash=False)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"

    def get(self, name: str, default: str = "") -> str:
        return (self.config.get(name) or default).strip()

    def get_list(self, name: str) -> list[str]:
        """A comma-separated field as a list: ``CONNECTOR_JIRA_PROJECTS=PAY,OPS`` -> ``["PAY", "OPS"]``."""
        return [part.strip() for part in self.get(name).split(",") if part.strip()]

    def item_id(self, external_id: str | int) -> str:
        """A node id for an item. ``external_id`` must be unique in the install across item types:
        prefix it with the type when the source reuses numbers (HubSpot: ``company:123``)."""
        return f"{self.key}:{external_id}"

    def user_id(self, external_id: str | int) -> str:
        return f"{self.key}:user:{external_id}"

    def container_id(self, external_id: str | int) -> str:
        return f"{self.key}:container:{external_id}"


@dataclass(frozen=True)
class InstallInfo:
    """What ``check_credentials`` learned. Stored on the install's state node, so nothing secret."""

    account: str
    site: str = ""


@dataclass(frozen=True)
class Container:
    """A project, board or portal that items live in. Only containers the connector returns are synced;
    an item whose container is not one of them is dropped, so the configured list is an allow-list."""

    id: str  # install.container_id(...)
    name: str
    external_id: str
    url: str = ""
    kind: str = ""  # "project", "portal": wording only
    # False: the container and its items are org_internal. True: their scope is the container's own,
    # and only people whose linked account ``readers()`` returns may read it, in a DM.
    restricted: bool = False


@dataclass(frozen=True)
class ExternalUser:
    """A person's account in the source. Linked ``-[:IS]->(:Person)`` when exactly one Person has
    ``email`` (case-insensitive). Connectors never create Person nodes."""

    id: str  # install.user_id(...)
    source: str
    external_id: str
    name: str
    email: str = ""


@dataclass(frozen=True)
class Item:
    """One record from the source, written as a node with label ``label``.

    graph.py writes ``id``, ``name``, ``title``, ``summary`` (clipped), ``url``, ``state``,
    ``status``, ``source_updated_at`` (``updated_at``), ``external_id``, ``kind`` (Service),
    plus ``source``, ``install``, ``scope`` (the container's) and ``synced_at`` (ms), then
    ``props`` under any name not already taken.
    """

    label: str  # one of ITEM_LABELS
    id: str  # install.item_id(external_id)
    external_id: str
    container: str  # Container.id
    name: str  # the short handle answers cite: "PAY-12", "Acme Corp"
    title: str = ""  # the full text: "Billing retries time out"
    url: str = ""
    state: str = "open"  # one of STATES; the source's own value goes in status
    status: str = ""
    updated_at: str = ""  # ISO 8601 from the source
    summary: str = ""
    kind: str | None = None  # Service only: 'account', ... (kinds.SERVICE_KINDS)
    # Names that may name a Service in the graph: Jira components and labels, a company's domain.
    # Each is resolved strictly; if none matches, the best guess over all of them, as for GitHub issues.
    about: tuple[str, ...] = ()
    # Extra plain values (str, int, float, bool, or a list of str): priority, issue type, amount.
    props: Mapping[str, object] = field(default_factory=dict, compare=False, hash=False)


@dataclass(frozen=True)
class Edge:
    """``(from_id)-[rel]->(to_id)``, both ids of this install (items or users).

    An edge belongs to its item: ``from_id`` when that is an item, else ``to_id``. Writing an
    item replaces every edge that belongs to it, so a page carries all of an item's edges, and
    an edge between two items is emitted once, from the item it points out of (PAY-13 BLOCKS
    PAY-12 comes with PAY-13). Edges in a page whose item is not in that page are only added.
    """

    rel: str  # one of EDGE_RELS
    from_id: str
    to_id: str
    props: Mapping[str, object] = field(default_factory=dict, compare=False, hash=False)


@dataclass
class Page:
    """What one fetch (or one webhook delivery) returns.

    ``next_cursor`` is persisted as-is for the container and passed to the next ``fetch``; None
    means the next fetch starts over. ``has_more`` asks for the next page right away, otherwise the
    next fetch waits ``poll_seconds``. ``retry_after`` (seconds) is the least wait before the next
    fetch either way. Ids in ``deleted_ids`` are removed with their edges.
    """

    items: list[Item] = field(default_factory=list)
    users: list[ExternalUser] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    deleted_ids: list[str] = field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False
    retry_after: float | None = None


@dataclass(frozen=True)
class WebhookRequest:
    """A delivery, as the sender made it. ``url`` is the public URL (``PUBLIC_URL`` + path + query),
    which signature schemes such as HubSpot's v3 sign; header names are lowercased."""

    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes


class Connector:
    """Subclass it, set ``source`` and ``required``, and call ``registry.register(YourConnector())`` at import.

    Every method runs on the scheduler thread or a webhook's background task and may call the
    source (through ``src.connectors.http.JsonClient``). Raise ``RetryLater`` to wait,
    ``CredentialsRejected`` on a 401, any ``ConnectorError`` otherwise; never put a credential in
    a message.
    """

    source: str = ""
    # Seconds between incremental fetches once a container is caught up.
    poll_seconds: int = 300
    # Fields that must all be set (CONNECTOR_<SOURCE>_<FIELD>) for the install to exist.
    required: tuple[str, ...] = ()
    # True when the source can push deliveries to POST /connectors/<source>/webhook.
    accepts_webhooks: bool = False

    def check_credentials(self, install: Install) -> InstallInfo:
        """One cheap call that proves the credentials work (Jira: /myself)."""
        raise NotImplementedError

    def containers(self, install: Install) -> list[Container]:
        """The containers to sync now. One that disappears from this list is removed with its items."""
        raise NotImplementedError

    def fetch(self, install: Install, container: Container, cursor: str | None) -> Page:
        """One page of items changed since ``cursor`` (None: the backfill). Newest first on a backfill,
        because a container keeps at most ``sync.MAX_ITEMS_PER_CONTAINER`` items."""
        raise NotImplementedError

    def readers(self, install: Install, container: Container) -> list[ExternalUser] | None:
        """Who may read a restricted container. None means unknown, and nobody may (fails closed)."""
        return None

    def verify_webhook(self, install: Install, request: WebhookRequest) -> bool:
        """Whether the delivery is signed by the source. False is answered 401 and nothing runs."""
        return False

    def parse_webhook(self, install: Install, request: WebhookRequest) -> Page | None:
        """The records a verified delivery changes, or None to ignore it. May call the source for the full
        record (HubSpot sends ids only). Cursors are not touched: the next poll catches anything missed."""
        return None
