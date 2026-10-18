"""What a person leaving takes with them: the bus-factor answer, for any team.

``if_person_leaves`` resolves a person (or, through ``people.resolve_person``, a Jira or HubSpot
account no person is linked to yet, named with its tool), then finds what rests on them alone:

- things of any kind they are the only owner of, or that no one else knows. "Knows" is
  ``who_knows`` itself (``ask_service.knowers``): the same edges and weights, so the people
  named next in line are the ones ``who_knows`` would rank first once this person is gone;
- standing decisions no one else proposed or approved;
- open tickets no one else worked on.

Each item is ranked by how many things depend on what it is about, following
``DEPENDS_ON`` the way ``blast_radius`` does, so the gap that reaches furthest comes
first. Accounts stay in: an account losing its only owner is exactly the risk, even
though nothing depends on an account. The reply lists the first few in full and names
the rest, so it reads in seconds; it cites the person's own edge to each item, the edges
that put each successor next in line, and the ``ABOUT`` edge from a decision or ticket.
"""

from dataclasses import dataclass

from src.db.falkordb_client import connect
from src.domain.knowledge import ask_service, kinds, people, resolve
from src.domain.knowledge.algorithms import MAX_DEPTH
from src.domain.knowledge.dto import PathStep, ToolResult
from src.domain.knowledge.resolve import SCOPED, Entity, join_names

TOOL = "if_person_leaves"
# Items told in full; the rest are named in one closing sentence.
MAX_LISTED = 3
# People named next in line for one thing.
MAX_SUCCESSORS = 2

# The person's own edge to cite when they hold a thing more than one way.
_HELD_BY = {"OWNS": 0, "WORKED_ON": 1, "KNOWS": 2}
_ROLES = ("PROPOSED", "APPROVED")


@dataclass(frozen=True)
class AtRisk:
    """One thing that rests on the person alone, with how much depends on what it is about."""

    name: str
    sentence: str
    dependents: int
    # Things first, then decisions, then tickets, when two reach equally far.
    order: int
    # The person's own edge first; then the edges that back the rest of the sentence.
    steps: tuple[PathStep, ...]


def if_person_leaves(question: str, scopes: list[str]) -> ToolResult:
    """What only this person owns, knows, decided, or is working on, the furthest-reaching first."""
    _, graph = connect()
    resolution = people.resolve_person(question, scopes, graph)
    if not resolution.found:
        return ToolResult(tool=TOOL, answer=resolve.describe_miss(resolution, "person"), found=False, candidates=resolution.candidate_names())
    person = resolution.entity
    items, linked = _gather(graph, person, scopes)
    hint = f" {people.unlinked_hint(person)}" if people.is_account(person) else ""

    result = ToolResult(tool=TOOL, answer="", candidates=resolution.candidate_names())
    if not items:
        if linked:
            result.answer = f"If {person.name} leaves, nothing rests on them alone: everything they own, work on, or decided has someone else on it.{hint}"
        else:
            result.answer = f"{person.name} is in the graph, and nothing is linked to them yet.{hint}"
        return result

    listed, rest = items[:MAX_LISTED], items[MAX_LISTED:]
    for item in listed:
        result.extend(item.steps)
    for item in rest:
        result.add(item.steps[0])
    if len(items) == 1:
        parts = [f"If {person.name} leaves, 1 thing rests on them alone."]
    else:
        parts = [f"If {person.name} leaves, {len(items)} things rest on them alone, ranked by how much depends on each."]
    parts.extend(item.sentence for item in listed)
    if rest:
        parts.append(f"And {len(rest)} more: {join_names([item.name for item in rest])}.")
    result.answer = " ".join(parts) + hint
    return result


def at_risk(graph, person: Entity, scopes: list[str]) -> list[AtRisk]:
    """What rests on this person alone, the furthest-reaching first: what ``if_person_leaves`` tells."""
    items, _linked = _gather(graph, person, scopes)
    return items


def _gather(graph, person: Entity, scopes: list[str]) -> tuple[list[AtRisk], bool]:
    """The items resting on the person alone, and whether anything at all is linked to them."""
    params = {"id": person.id, "ids": people.identity_ids(graph, person, scopes), "scopes": scopes}
    reach: dict[str, int] = {}
    things, held = _things(graph, person, params, reach)
    decisions, decided = _decisions(graph, person, params, reach)
    tickets, worked = _tickets(graph, person, params, reach)
    items = sorted([*things, *decisions, *tickets], key=lambda item: (-item.dependents, item.order, item.name))
    return items, bool(held or decided or worked)


def _things(graph, person: Entity, params: dict, reach: dict[str, int]) -> tuple[list[AtRisk], int]:
    """Things only this person owns, or that no one else knows. Also how many things they hold at all."""
    # A person's co-owners are people; an unlinked account's are anyone else who owns it.
    others = "other:Person" if person.label == "Person" else "other"
    rows = graph.ro_query(
        f"""
        MATCH {_me(person)}-[mine:OWNS|WORKED_ON|KNOWS]->(s:Service)
        WHERE me.id IN $ids AND {SCOPED.format(var="s")}
        WITH s, collect(DISTINCT type(mine)) AS mine
        OPTIONAL MATCH ({others})-[:OWNS]->(s)
        WHERE NOT other.id IN $ids AND labels(other)[0] IN ['Person', 'ExternalUser'] AND {SCOPED.format(var="other")}
        RETURN s.id, s.name, {kinds.KIND_OF.format(var="s")}, mine, count(other)
        ORDER BY s.name
        """,
        params,
    ).result_set
    items = []
    for service_id, name, kind, mine, other_owners in rows:
        thing = Entity(service_id, name, "Service", kind)
        known = ask_service.knowers(graph, thing, params["scopes"])
        # The person leaving is told apart by id: an unlinked account with their name is someone else.
        ranked = [name for who, name, _score, _reasons in ask_service.rank_people_keyed(known.path, thing) if who not in {(person.label, node_id) for node_id in params["ids"]}]
        owns = "OWNS" in mine
        alone = not ranked
        if not alone and not (owns and not other_owners):
            continue
        listed = kinds.capitalized(kinds.listed(name, kind))
        if owns:
            holds = "the only one running it" if kinds.kind_or_default(kind) == "process" else "its only owner"
            sentence = f"{listed}: {person.name} is {holds}" + (" and the only one who knows it" if alone else "")
        else:
            sentence = f"{listed}: {person.name} is the only one who knows it"
        successors = ranked[:MAX_SUCCESSORS]
        if successors:
            sentence += f"; next in line: {successors[0]}" + (f", then {join_names(successors[1:])}" if successors[1:] else "")
        dependents = _dependents(graph, service_id, params, reach)
        own = PathStep(person.name, min(mine, key=_HELD_BY.get), name, from_id=person.id, to_id=service_id, from_label=person.label, to_label="Service")
        steps = (own, *ask_service.edges_of(known.path, successors))
        items.append(AtRisk(kinds.listed(name, kind), sentence + _reach_clause(dependents, "it") + ".", dependents, 0, steps))
    return items, len(rows)


def _decisions(graph, person: Entity, params: dict, reach: dict[str, int]) -> tuple[list[AtRisk], int]:
    """Standing decisions no one else proposed or approved. Also how many standing decisions they are on."""
    rows = graph.ro_query(
        f"""
        MATCH {_me(person)}-[mine:PROPOSED|APPROVED]->(d:Decision)
        WHERE me.id IN $ids AND {SCOPED.format(var="d")}
        OPTIONAL MATCH (newer:Decision)-[:SUPERSEDES]->(d)
        WHERE {SCOPED.format(var="newer")}
        WITH d, collect(DISTINCT type(mine)) AS mine, count(newer) AS newer
        WHERE newer = 0
        OPTIONAL MATCH (other:Person)-[:PROPOSED|APPROVED]->(d)
        WHERE other.id <> $id
        WITH d, mine, count(other) AS others
        OPTIONAL MATCH (d)-[:ABOUT]->(s:Service)
        WHERE {SCOPED.format(var="s")}
        RETURN d.id, d.name, mine, others, collect(DISTINCT [s.id, s.name, {kinds.KIND_OF.format(var="s")}])
        ORDER BY d.name
        """,
        params,
    ).result_set
    items = []
    for decision_id, name, mine, others, about in rows:
        if others:
            continue
        roles = [role for role in _ROLES if role in mine]
        steps = [PathStep(person.name, role, name, from_id=person.id, to_id=decision_id, from_label="Person", to_label="Decision") for role in roles]
        about_clause, dependents, about_steps = _about(graph, name, decision_id, "Decision", about, params, reach)
        sentence = f"The standing decision *{name}*{about_clause}: only {person.name} {' and '.join(role.lower() for role in roles)} it"
        items.append(AtRisk(f"*{name}*", sentence + _reach_clause(dependents, _subject_of(about)) + ".", dependents, 1, (*steps, *about_steps)))
    return items, len(rows)


def _tickets(graph, person: Entity, params: dict, reach: dict[str, int]) -> tuple[list[AtRisk], int]:
    """Open tickets no one else worked on or is assigned. Also how many open tickets they are on.

    Theirs are the ones they worked on and the ones assigned to an account linked to them (Jira,
    GitHub). Anyone else on it is another person who worked on it, or any other account assigned
    to it, linked or not.
    """
    accounts = {**params, "accounts": list(ask_service.ACCOUNT_LABELS)}
    mine: dict[str, str] = {}
    # An unlinked account's tickets are its own: it is assigned them, or worked on them.
    assigned = (
        f"""
        MATCH {_me(person)}-[:ASSIGNED]->(k:Ticket)
        WHERE me.id IN $ids AND {SCOPED.format(var="k")} AND coalesce(k.state, 'open') <> 'closed'
        RETURN k.id AS ticket, 'ASSIGNED' AS rel
        """
        if people.is_account(person)
        else f"""
        MATCH (:Person {{id: $id}})<-[:IS]-(account)-[:ASSIGNED]->(k:Ticket)
        WHERE labels(account)[0] IN $accounts AND {SCOPED.format(var="account")} AND {SCOPED.format(var="k")}
          AND coalesce(k.state, 'open') <> 'closed'
        RETURN k.id AS ticket, 'ASSIGNED' AS rel
        """
    )
    for ticket_id, rel in graph.ro_query(
        f"""
        MATCH {_me(person)}-[:WORKED_ON]->(k:Ticket)
        WHERE me.id IN $ids AND {SCOPED.format(var="k")} AND coalesce(k.state, 'open') <> 'closed'
        RETURN k.id AS ticket, 'WORKED_ON' AS rel
        UNION
        {assigned}
        """,
        accounts,
    ).result_set:
        mine.setdefault(ticket_id, rel)  # worked on it, if both
    if not mine:
        return [], 0
    rows = graph.ro_query(
        f"""
        MATCH (k:Ticket)
        WHERE k.id IN $tickets
        OPTIONAL MATCH (other)-[:WORKED_ON|ASSIGNED]->(k)
        WHERE {SCOPED.format(var="other")}
          AND ((labels(other)[0] = 'Person' AND other.id <> $id) OR (labels(other)[0] IN $accounts AND NOT other.id IN $ids AND NOT (other)-[:IS]->(:Person {{id: $id}})))
        WITH k, count(DISTINCT other) AS others
        OPTIONAL MATCH (k)-[:ABOUT]->(s:Service)
        WHERE {SCOPED.format(var="s")}
        RETURN k.id, k.name, coalesce(k.title, k.name), others, collect(DISTINCT [s.id, s.name, {kinds.KIND_OF.format(var="s")}])
        ORDER BY k.name
        """,
        {**accounts, "tickets": sorted(mine)},
    ).result_set
    items = []
    for ticket_id, name, title, others, about in rows:
        if others:
            continue
        rel = mine[ticket_id]
        step = PathStep(person.name, rel, name, from_id=person.id, to_id=ticket_id, from_label=person.label, to_label="Ticket")
        about_clause, dependents, about_steps = _about(graph, name, ticket_id, "Ticket", about, params, reach)
        titled = f" (“{title}”)" if title and title != name else ""
        holds = "worked on it" if rel == "WORKED_ON" else "is assigned it"
        sentence = f"Open ticket {name}{titled}{about_clause}: only {person.name} {holds}"
        items.append(AtRisk(name, sentence + _reach_clause(dependents, _subject_of(about)) + ".", dependents, 2, (step, *about_steps)))
    return items, len(rows)


def _about(graph, name: str, node_id: str, label: str, about: list, params: dict, reach: dict[str, int]) -> tuple[str, int, list[PathStep]]:
    """The "about X" clause, how far the furthest-reaching thing it is about reaches, and the ABOUT edges."""
    about = [(service_id, service, kind) for service_id, service, kind in about if service_id is not None]
    if not about:
        return "", 0, []
    steps = [PathStep(name, "ABOUT", service, from_id=node_id, to_id=service_id, from_label=label, to_label="Service") for service_id, service, _kind in about]
    dependents = max(_dependents(graph, service_id, params, reach) for service_id, _service, _kind in about)
    return f", about {join_names([kinds.listed(service, kind) for _id, service, kind in about])}", dependents, steps


def _subject_of(about: list) -> str:
    """What the reach clause of a decision or ticket names: the one thing it is about, else "it"."""
    about = [(service, kind) for service_id, service, kind in about if service_id is not None]
    return kinds.listed(*about[0]) if len(about) == 1 else "it"


def _me(person: Entity) -> str:
    """The node leaving, by its label: a person, or an unlinked connector account. Never text from a question."""
    label = "ExternalUser" if people.is_account(person) else "Person"
    return f"(me:{label})"


def _reach_clause(dependents: int, subject: str) -> str:
    if not dependents:
        return ""
    return f"; {dependents} other{'s' if dependents != 1 else ''} depend{'s' if dependents == 1 else ''} on {subject}"


def _dependents(graph, service_id: str, params: dict, reach: dict[str, int]) -> int:
    """How many visible things depend on this one, directly or through others. Counted once per call."""
    if service_id not in reach:
        reach[service_id] = graph.ro_query(
            f"""
            MATCH path = (dependent:Service)-[:DEPENDS_ON*1..{MAX_DEPTH}]->(:Service {{id: $service}})
            WHERE all(n IN nodes(path) WHERE {SCOPED.format(var="n")}) AND NOT {kinds.KIND_OF.format(var="dependent")} IN $excluded
            RETURN count(DISTINCT dependent)
            """,
            {"service": service_id, "scopes": params["scopes"], "excluded": sorted(kinds.NOT_DEPENDABLE)},
        ).result_set[0][0]
    return reach[service_id]
