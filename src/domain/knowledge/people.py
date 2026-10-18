"""Who a question names: a person the company knows, or someone a connected tool knows.

A connected tool (Jira, HubSpot) writes its users as ``ExternalUser`` nodes. One whose email
matches a person is linked to them (``IS``) and its work moves onto the person; one that is not
stays an account of its own. ``resolve_person`` resolves a ``Person`` first, as every person
tool always has, and only then an ``ExternalUser``, always through the asker's scopes: connector
accounts are ``org_internal``, so the company-wide reader never resolves one.

An account linked to a person resolves to that person. An unlinked one resolves to itself,
named with its tool ("Yash Patil (HubSpot)") so it is never taken for a namesake, and every
answer about it carries ``unlinked_hint``.
"""

from src.domain.knowledge import resolve
from src.domain.knowledge.resolve import SCOPED, Candidate, Entity, Resolution

ACCOUNT = "ExternalUser"
# Mirrors ask_service.SOURCE_NAMES; that module imports the person tools, so it is not imported here.
SOURCE_NAMES = {"jira": "Jira", "hubspot": "HubSpot"}


def tool_name(source: str | None) -> str:
    return SOURCE_NAMES.get(source or "", (source or "external").capitalize())


def account_label(name: str | None, source: str | None) -> str:
    """"Yash Patil (HubSpot)". A name that is an email shows its local part only."""
    text = (name or "").strip()
    if "@" in text:
        text = text.split("@", 1)[0].strip()
    return f"{text or 'unnamed account'} ({tool_name(source)})"


def unlinked_hint(account: Entity) -> str:
    """The sentence every answer about an unlinked account ends with."""
    return f"{account.name} is not linked to a person yet; an admin can link the account by giving a person the same email."


def resolve_person(text: str, scopes: list[str], graph) -> Resolution:
    """A ``Person`` the text names, else a connector account it names (linked: its person), else the misses of both."""
    person = resolve.resolve_name(text, ("Person",), scopes, graph=graph)
    if person.found:
        return person
    account = resolve.resolve_name(text, (ACCOUNT,), scopes, graph=graph)
    if account.found:
        return Resolution(query=account.query, entity=shown(graph, account.entity, scopes), candidates=account.candidates)
    # The person refusal, with any account named nearby as a candidate too.
    known = {candidate.entity.id for candidate in person.candidates}
    for candidate in account.candidates:
        entity = shown(graph, candidate.entity, scopes)
        if entity.id not in known and len(person.candidates) < resolve.MAX_CANDIDATES:
            known.add(entity.id)
            person.candidates.append(Candidate(entity, candidate.via))
    return person


def shown(graph, account: Entity, scopes: list[str]) -> Entity:
    """The person a linked account belongs to, or the account named with its tool."""
    rows = graph.ro_query(
        f"""
        MATCH (a:ExternalUser {{id: $id}})
        OPTIONAL MATCH (a)-[:IS]->(p:Person)
        WHERE {SCOPED.format(var="p")}
        RETURN a.name, a.source, p.id, p.name
        LIMIT 1
        """,
        {"id": account.id, "scopes": scopes},
    ).result_set
    name, source, person_id, person_name = rows[0] if rows else (account.name, None, None, None)
    if person_id is not None:
        return Entity(person_id, person_name, "Person")
    # The tool rides in ``kind``, which is not part of identity: "an account in HubSpot".
    return Entity(account.id, account_label(name, source), ACCOUNT, tool_name(source))


def is_account(entity: Entity | None) -> bool:
    return entity is not None and entity.label == ACCOUNT
