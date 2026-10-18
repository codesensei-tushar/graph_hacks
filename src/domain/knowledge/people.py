"""Who a question names: a person the company knows, or someone a connected tool knows.

A connected tool (Jira, HubSpot) writes its users as ``ExternalUser`` nodes. One whose email
matches a person is linked to them (``IS``) and its work moves onto the person; one that is not
stays a user of its own. ``resolve_person`` resolves a ``Person`` first, as every person tool
always has, and only then an ``ExternalUser``, always through the asker's scopes: connector
users are ``org_internal``, so the company-wide reader never resolves one.

- A user linked to a person resolves to that person.
- Unlinked users of different tools with the same email are one identity: the Jira "Yash Patil"
  and the HubSpot "Yash Patil" are named "Yash Patil (Jira, HubSpot)", and ``identity_ids`` lists
  every user of it, so the person tools read the work of both.
- A first name resolves an unlinked user when exactly one identity has it ("who is yash"), and
  only when no person the company knows matches.

Every answer about an unlinked user carries ``unlinked_hint``: how an admin links them.
"""

from dataclasses import dataclass

from src.domain.knowledge import resolve
from src.domain.knowledge.resolve import SCOPED, Candidate, Entity, Resolution

ACCOUNT = "ExternalUser"
# Mirrors ask_service.SOURCE_NAMES; that module imports the person tools, so it is not imported here.
SOURCE_NAMES = {"jira": "Jira", "hubspot": "HubSpot"}
# How an admin tells a connector which email a user goes by.
PEOPLE_SETTINGS = {"jira": ("CONNECTOR_JIRA_PEOPLE", "accountId=email"), "hubspot": ("CONNECTOR_HUBSPOT_PEOPLE", "ownerId=email")}
MIN_FIRST_NAME = 2


def tool_name(source: str | None) -> str:
    return SOURCE_NAMES.get(source or "", (source or "external").capitalize())


def account_label(name: str | None, source: str | None) -> str:
    """"Yash Patil (HubSpot)". A name that is an email shows its local part only."""
    return f"{_bare(name)} ({tool_name(source)})"


def _bare(name: str | None) -> str:
    text = (name or "").strip()
    if "@" in text:
        text = text.split("@", 1)[0].strip()
    return text or "unnamed user"


def _tools(entity: Entity) -> list[str]:
    """The tools an unlinked identity is a user of, carried in ``kind``: "Jira, HubSpot"."""
    return [tool for tool in (entity.kind or "").split(", ") if tool]


def describe(entity: Entity) -> str:
    """"a HubSpot user", "a Jira and HubSpot user"."""
    tools = _tools(entity) or ["connected tool"]
    named = " and ".join(tools) if len(tools) <= 2 else ", ".join(tools[:-1]) + " and " + tools[-1]
    return f"a {named} user"


def unlinked_hint(account: Entity) -> str:
    """The sentence every answer about an unlinked user ends with: how an admin links them."""
    sources = [source for source, tool in SOURCE_NAMES.items() if tool in _tools(account)] or ["hubspot"]
    settings = " or ".join(f"{PEOPLE_SETTINGS[source][0]} ({PEOPLE_SETTINGS[source][1]})" for source in sources if source in PEOPLE_SETTINGS)
    return f"{account.name} is not linked to a person yet; an admin can link them in {settings}."


@dataclass(frozen=True)
class _User:
    id: str
    name: str
    source: str
    email: str
    person_id: str | None
    person_name: str | None


def _users(graph, scopes: list[str], ids: list[str] | None = None) -> list[_User]:
    """Connector users the asker can read (all, or these ids), each with the person it is linked to."""
    rows = graph.ro_query(
        f"""
        MATCH (a:ExternalUser)
        WHERE {SCOPED.format(var="a")} AND ($ids IS NULL OR a.id IN $ids)
        OPTIONAL MATCH (a)-[:IS]->(p:Person)
        WHERE {SCOPED.format(var="p")}
        RETURN a.id, coalesce(a.name, ''), coalesce(a.source, ''), toLower(coalesce(a.email, '')), p.id, p.name
        """,
        {"scopes": list(scopes), "ids": ids},
    ).result_set
    return [_User(*row) for row in rows]


def _key(user: _User) -> str:
    """One identity per verified email; a user without an email is its own identity."""
    return f"email:{user.email}" if user.email and "@" in user.email else f"id:{user.id}"


def _identity(graph, account_id: str, scopes: list[str]) -> list[_User]:
    """The readable users that are the same identity as this one: the same email, in any tool."""
    users = _users(graph, scopes)
    mine = next((user for user in users if user.id == account_id), None)
    if mine is None:
        return []
    key = _key(mine)
    return sorted([user for user in users if _key(user) == key], key=lambda user: (user.source, user.id))


def shown(graph, account: Entity, scopes: list[str]) -> Entity:
    """The person a user (or a user of the same identity) is linked to, or the identity named with its tools."""
    same = _identity(graph, account.id, scopes)
    linked = next((user for user in same if user.person_id), None)
    if linked is not None:
        return Entity(linked.person_id, linked.person_name, "Person")
    if not same:
        return Entity(account.id, account_label(account.name, None), ACCOUNT, "Connected tool")
    first = next((user for user in same if user.id == account.id), same[0])
    tools = list(dict.fromkeys(tool_name(user.source) for user in same))
    # The tools ride in ``kind``, which is not part of identity: "a Jira and HubSpot user".
    return Entity(first.id, f"{_bare(first.name)} ({', '.join(tools)})", ACCOUNT, ", ".join(tools))


def identity_ids(graph, entity: Entity, scopes: list[str]) -> list[str]:
    """Every node id that is this person or identity: the user and its same-email users, or the person."""
    if entity.label != ACCOUNT:
        return [entity.id]
    return [user.id for user in _identity(graph, entity.id, scopes)] or [entity.id]


def resolve_person(text: str, scopes: list[str], graph) -> Resolution:
    """A ``Person`` the text names, else a connector user it names (in full, or by a unique first
    name), shown as its person or its identity, else the misses of both."""
    person = resolve.resolve_name(text, ("Person",), scopes, graph=graph)
    if person.found:
        return person
    account = resolve.resolve_name(text, (ACCOUNT,), scopes, graph=graph)
    if account.found:
        return Resolution(query=account.query, entity=shown(graph, account.entity, scopes), candidates=_others(graph, account.candidates, scopes))
    by_first = _by_first_name(graph, text, scopes) if not person.candidates else None
    if by_first is not None:
        return Resolution(query=text, entity=by_first)
    # The person refusal, with any user named nearby as a candidate too.
    known = {candidate.entity.id for candidate in person.candidates}
    for candidate in account.candidates:
        entity = shown(graph, candidate.entity, scopes)
        if entity.id not in known and len(person.candidates) < resolve.MAX_CANDIDATES:
            known.add(entity.id)
            person.candidates.append(Candidate(entity, candidate.via))
    return person


def _others(graph, candidates: list[Candidate], scopes: list[str]) -> list[Candidate]:
    return [Candidate(shown(graph, candidate.entity, scopes), candidate.via) for candidate in candidates]


def _by_first_name(graph, text: str, scopes: list[str]) -> Entity | None:
    """The one identity whose first name the text uses ("who is yash"), when exactly one has it."""
    words = set(resolve.tokens(text)) - resolve.STOPWORDS
    if not words:
        return None
    identities: dict[str, _User] = {}
    for user in _users(graph, scopes):
        first = next(iter(resolve.tokens(user.name)), "")
        if len(first) >= MIN_FIRST_NAME and first in words:
            identities.setdefault(_key(user), user)
    if len(identities) != 1:
        return None
    user = next(iter(identities.values()))
    return shown(graph, Entity(user.id, user.name, ACCOUNT), scopes)


def is_account(entity: Entity | None) -> bool:
    return entity is not None and entity.label == ACCOUNT
