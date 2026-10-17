"""Who connects two teams, or one team to the rest: people whose own edges cross the boundary.

A person touches a team by being a member (``MEMBER_OF``) or by owning or having worked on
something the team runs (``OWNS`` or ``WORKED_ON`` a thing the team ``RUNS``). Knowing a
thing is left out, as in ``working_groups``: knowing is not shared work. A bridge between
two teams touches both; between one team and the rest, it touches that team and another.

A bridge is ranked by its cross-team edges: the work edges into a team the person is not
on (plus one for being on both teams). It is exact and the same on every run, because it is
counted from edges the reply prints. No graph algorithm runs here: sampled betweenness was
tried for the blast radius connector and named a different top node per process.
"""

from dataclasses import dataclass, field

from src.db.falkordb_client import connect
from src.domain.knowledge import kinds, resolve
from src.domain.knowledge.dto import PathStep, ToolResult
from src.domain.knowledge.resolve import SCOPED, Entity, join_names

TOOL = "connectors"
MAX_LISTED = 3
_VERBS = {"OWNS": "owns", "WORKED_ON": "worked on"}


@dataclass(frozen=True)
class Work:
    """One work edge into something a team runs."""

    rel: str
    thing_id: str
    thing: str
    kind: str
    team_id: str
    team: str


@dataclass
class Person:
    id: str
    name: str
    homes: dict[str, str] = field(default_factory=dict)  # team id -> team name
    work: list[Work] = field(default_factory=list)

    def touches(self, team_id: str) -> bool:
        return team_id in self.homes or any(edge.team_id == team_id for edge in self.work)


@dataclass(frozen=True)
class Bridge:
    person: Person
    crossing: list[Work]
    # On both of the two teams (or on the one team and another).
    dual: bool

    @property
    def score(self) -> int:
        return len({(edge.rel, edge.thing_id) for edge in self.crossing}) + self.dual


def connectors(source: str, target: str, scopes: list[str]) -> ToolResult:
    """Who connects the teams named, most cross-team work first, each with the edges that make the bridge."""
    _, graph = connect()
    named = _teams(graph, source, target, scopes)
    if isinstance(named, ToolResult):
        return named
    teams, unmatched = named
    people = _people(graph, scopes)
    if len(teams) == 2:
        first, second = teams
        if first == second:
            return ToolResult(tool=TOOL, answer=f"{first.name} is one team; name two teams, or one team to see who connects it to the rest.")
        bridges = [bridge for person in people if (bridge := _between(person, first.id, second.id)) is not None]
        between = f"{first.name} and {second.name}"
    else:
        (first,) = teams
        bridges = [bridge for person in people if (bridge := _outward(person, first.id)) is not None]
        between = f"{first.name} to other teams"
    bridges.sort(key=lambda bridge: (-bridge.score, bridge.person.name))

    result = ToolResult(tool=TOOL, answer="")
    # "who connects Sales and Engineering" where no team is called Engineering: say so, then answer for Sales.
    note = f"No team in the graph is called “{unmatched}”. " if unmatched else ""
    if not bridges:
        result.answer = f"{note}No one connects {between}: no one on one side is on the other or works on what it runs."
        return result
    listed, rest = bridges[:MAX_LISTED], bridges[MAX_LISTED:]
    for bridge in listed:
        result.extend(_steps(bridge))
    count = len(bridges)
    lead = f"{count} {'person connects' if count == 1 else 'people connect'} {between}"
    parts = [lead + ("." if count == 1 else ", most cross-team work first.")]
    parts.extend(_sentence(bridge) for bridge in listed)
    if rest:
        parts.append(f"And {len(rest)} more: {join_names([bridge.person.name for bridge in rest])}.")
    result.answer = note + " ".join(parts)
    return result


def _unmatched(question: str, team: Entity) -> str:
    """What the question names besides the one team it resolved to, after question words: "" when nothing."""
    named = resolve.tokens(team.name)
    return " ".join(word for word in resolve.core(question).split() if not any(resolve.same_word(word, key) for key in named))


def _teams(graph, source: str, target: str, scopes: list[str]):
    """One or two teams and what else was named that is no team, or a refusal.

    The keyword router passes the whole question as ``source``; the model fills ``source`` and
    ``target`` apart. Either way, when one team resolves and the other name matches nothing at
    all, the answer is for that one team and says what it did not find. A name with near misses
    is refused instead, so the reply can offer them.
    """
    if target and target.strip().lower() != source.strip().lower():
        resolutions = [resolve.resolve_name(text, ("Team",), scopes, graph=graph) for text in (source, target)]
        found = [resolution.entity for resolution in resolutions if resolution.found]
        missed = [resolution for resolution in resolutions if not resolution.found]
        if not found or any(resolution.candidates for resolution in missed):
            return _miss(next((resolution for resolution in missed if resolution.candidates), missed[0]))
        return found, " ".join(resolve.core(resolution.query) for resolution in missed)
    named = resolve.resolve_all(source, ("Team",), scopes, graph=graph)
    if len(named) >= 2:
        return named[:2], ""
    if named:
        return named, _unmatched(source, named[0])
    return _miss(resolve.resolve_name(source, ("Team",), scopes, graph=graph))


def _miss(resolution: resolve.Resolution) -> ToolResult:
    return ToolResult(tool=TOOL, answer=resolve.describe_miss(resolution, "team"), found=False, candidates=resolution.candidate_names())


def _people(graph, scopes: list[str]) -> list[Person]:
    people: dict[str, Person] = {}
    for person_id, name, team_id, team in graph.ro_query(
        f"""
        MATCH (p:Person)-[:MEMBER_OF]->(t:Team)
        WHERE {SCOPED.format(var="p")} AND {SCOPED.format(var="t")}
        RETURN p.id, p.name, t.id, t.name
        """,
        {"scopes": scopes},
    ).result_set:
        people.setdefault(person_id, Person(person_id, name)).homes[team_id] = team
    for person_id, name, rel, thing_id, thing, kind, team_id, team in graph.ro_query(
        f"""
        MATCH (p:Person)-[r:OWNS|WORKED_ON]->(s:Service)<-[:RUNS]-(t:Team)
        WHERE {SCOPED.format(var="p")} AND {SCOPED.format(var="s")} AND {SCOPED.format(var="t")}
        RETURN p.id, p.name, type(r), s.id, s.name, {kinds.KIND_OF.format(var="s")}, t.id, t.name
        ORDER BY s.name, t.name
        """,
        {"scopes": scopes},
    ).result_set:
        people.setdefault(person_id, Person(person_id, name)).work.append(Work(rel, thing_id, thing, kind, team_id, team))
    return list(people.values())


def _between(person: Person, first: str, second: str) -> Bridge | None:
    if not (person.touches(first) and person.touches(second)):
        return None
    crossing = [edge for edge in person.work if edge.team_id in (first, second) and edge.team_id not in person.homes]
    return Bridge(person, crossing, first in person.homes and second in person.homes)


def _outward(person: Person, team: str) -> Bridge | None:
    if team in person.homes:
        crossing = [edge for edge in person.work if edge.team_id != team and edge.team_id not in person.homes]
        dual = len(person.homes) > 1
    else:
        crossing = [edge for edge in person.work if edge.team_id == team]
        dual = False
    bridge = Bridge(person, crossing, dual)
    return bridge if bridge.score else None


def _sentence(bridge: Bridge) -> str:
    person = bridge.person
    homes = join_names(sorted(person.homes.values())) or "no team"
    if not bridge.crossing:
        return f"{person.name} is on {homes}."
    # "worked on Billing Service (Payments) and the Pricing playbook (Sales), owns ...": one clause per verb, as who_knows words it.
    teams_of: dict[tuple[str, str], list[str]] = {}
    for edge in bridge.crossing:
        teams_of.setdefault((edge.rel, edge.thing_id), []).append(edge.team)
    by_verb: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    for edge in bridge.crossing:
        key = (edge.rel, edge.thing_id)
        if key in seen:
            continue
        seen.add(key)
        verb = "runs" if edge.rel == "OWNS" and kinds.kind_or_default(edge.kind) == "process" else _VERBS[edge.rel]
        by_verb.setdefault(verb, []).append(f"{kinds.subject(edge.thing, edge.kind)} ({join_names(teams_of[key])})")
    clauses = [f"{verb} {join_names(things)}" for verb, things in by_verb.items()]
    return f"{person.name}, on {homes}, {', '.join(clauses)}."


def _steps(bridge: Bridge) -> list[PathStep]:
    """Membership, then each crossing edge with the team that runs what it reaches: the path through the person."""
    person = bridge.person
    entity = Entity(person.id, person.name, "Person")
    steps = [PathStep(entity.name, "MEMBER_OF", team, from_id=entity.id, to_id=team_id, from_label="Person", to_label="Team") for team_id, team in sorted(person.homes.items(), key=lambda item: item[1])]
    for edge in bridge.crossing:
        steps.append(PathStep(entity.name, edge.rel, edge.thing, from_id=entity.id, to_id=edge.thing_id, from_label="Person", to_label="Service"))
        steps.append(PathStep(edge.team, "RUNS", edge.thing, from_id=edge.team_id, to_id=edge.thing_id, from_label="Team", to_label="Service"))
    return steps
