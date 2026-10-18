"""What to read before touching a service: ``reading_list``, the context pack for a change.

It resolves a service of any kind, walks what it depends on the way ``blast_radius`` walks
forward, and collects, nearest first:

- the documents that describe it and each thing it depends on, with who wrote them;
- the standing decisions about any of them, each followed forward through ``SUPERSEDES``
  to the one this reader can see standing (``decisions.current_decision``, never a bare
  "nothing supersedes it", which a private replacement would hide), and what each is
  based on. A document a decision rests on joins the list at that decision's hop;
- what nothing documents, with who owns it, so the reader knows whom to ask.

The path cites the edges behind every line: who wrote each document and what it
describes, one shortest ``DEPENDS_ON`` route to each thing named (ties broken by the
names along it), each decision's ``ABOUT`` edges and sources, and the owner of each gap.
Over MCP it is the context a coding agent pulls before it edits a service.
"""

import re
from dataclasses import dataclass

from src.db.falkordb_client import connect
from src.domain.knowledge import ask_service, decisions, kinds, resolve
from src.domain.knowledge.algorithms import MAX_DEPTH
from src.domain.knowledge.dto import PathStep, ToolResult
from src.domain.knowledge.resolve import SCOPED, Entity, join_names

TOOL = "reading_list"
# What joins the routed phrase to the name, stripped once right after it: "what should I read
# before touching the payments gateway" names the payments gateway. Only here, never in the
# resolver's stopwords, so "Change management" or "New hire onboarding" still resolve elsewhere.
_LEAD_IN = re.compile(
    r"^\W*(?:before (?:i |we )?(?:touch|touching|edit|editing|change|changing|work on|working on)|to understand|about|on|for|of)\b",
    re.IGNORECASE,
)
# Documents told in full; the rest are named in one closing sentence.
MAX_DOCUMENTS = 5
MAX_DECISIONS = 2


@dataclass(frozen=True)
class Walked:
    """The target, or one thing it depends on, with the one route the path cites to it."""

    id: str
    name: str
    kind: str | None
    hop: int
    ids: tuple[str, ...]
    names: tuple[str, ...]

    @property
    def subject(self) -> str:
        return kinds.subject(self.name, self.kind)


@dataclass(frozen=True)
class Reading:
    """One document on the list: what it describes, or the decision that rests on it."""

    id: str
    name: str
    hop: int
    authors: tuple[tuple[str, str], ...]
    describes: Walked | None = None
    behind: str | None = None


@dataclass
class Standing:
    """A decision that stands today about something walked, with what it is based on."""

    decision: decisions.DecisionRow
    hop: int
    about: list[Walked]
    # (relationship, id, name, label, kind) of each thing it is based on.
    sources: list[tuple[str, str, str, str, str | None]]


def reading_list(question: str, scopes: list[str]) -> ToolResult:
    """The documents, standing decisions and gaps around a service and everything it depends on."""
    _, graph = connect()
    resolution = resolve.resolve_name(named(question), ("Service",), scopes, graph=graph)
    if not resolution.found:
        return ToolResult(tool=TOOL, answer=resolve.describe_miss(resolution, "service, system, or process"), found=False, candidates=resolution.candidate_names())
    target = resolution.entity
    walked = walk(graph, target, scopes)
    readings = _documents(graph, walked, scopes)
    described = {reading.describes.id for reading in readings if reading.describes}
    standing = _standing(graph, walked, scopes)[:MAX_DECISIONS]
    listed = {reading.id for reading in readings}
    for item in standing:
        joining = [(source_id, name) for _rel, source_id, name, label, _kind in item.sources if label == "Document" and source_id not in listed]
        authors = _authors(graph, [source_id for source_id, _name in joining])
        for source_id, name in joining:
            readings.append(Reading(source_id, name, item.hop, authors.get(source_id, ()), behind=item.decision.name))
            listed.add(source_id)
    readings.sort(key=lambda reading: (reading.hop, reading.name))
    gaps = [thing for thing in walked if thing.id not in described]
    owners = _owners(graph, [thing.id for thing in gaps], scopes)

    result = ToolResult(tool=TOOL, answer="", candidates=resolution.candidate_names())
    shown, rest = readings[:MAX_DOCUMENTS], readings[MAX_DOCUMENTS:]
    for reading in shown:
        result.extend(_reading_steps(reading))
    for reading in rest:
        result.extend(_reading_steps(reading)[-1:])
    for item in standing:
        result.extend(_standing_steps(item))
    for thing in gaps:
        result.extend(_route(thing))
        result.extend(PathStep(name, "OWNS", thing.name, from_id=person_id, to_id=thing.id, from_label="Person", to_label="Service") for person_id, name in owners.get(thing.id, ()))

    subject = kinds.subject(target.name, target.kind)
    parts = []
    asked = list(dict.fromkeys(name for thing in gaps for _id, name in owners.get(thing.id, ())))
    ask = f"; ask {join_names(asked)}" if asked else ""
    if readings:
        count = f"{len(readings)} document{'s' if len(readings) != 1 else ''}"
        nearest = ", nearest first" if len(readings) > 1 else ""
        parts.append(f"Before touching {subject}, read {count}{nearest}: {join_names([_reading_line(reading) for reading in shown])}.")
        if rest:
            parts.append(f"And {len(rest)} more: {join_names([reading.name for reading in rest])}.")
    if standing:
        lead = "Standing decision" if len(standing) == 1 else "Standing decisions"
        parts.append(f"{lead}: {'; '.join(_standing_line(item) for item in standing)}.")
    if not readings:
        beyond = " or what it depends on" if len(walked) > 1 else ""
        parts.insert(0, f"Nothing documents {subject}{beyond}{ask}.")
    elif gaps:
        parts.append(f"Nothing documents {_either([thing.subject for thing in gaps])}{ask}.")
    result.answer = kinds.capitalized(" ".join(parts))
    return result


def named(question: str) -> str:
    """The question without the words that asked for a reading list, so only the thing named is resolved and quoted."""
    match = ask_service.READING_LIST.search(question)
    if match is None:
        return question
    rest = f"{question[: match.start()]} {_LEAD_IN.sub('', question[match.end():], count=1)}"
    return " ".join(rest.split())


def walk(graph, target: Entity, scopes: list[str], depth: int = MAX_DEPTH) -> list[Walked]:
    """The target, then every visible thing it depends on within ``depth`` hops, nearest first; one route each, the first by name on a tie.

    ``depth`` is a module constant of the caller, written into the query as a literal bound.
    """
    rows = graph.ro_query(
        f"""
        MATCH path = (:Service {{id: $id}})-[:DEPENDS_ON*1..{int(depth)}]->(d:Service)
        WHERE all(n IN nodes(path) WHERE {SCOPED.format(var="n")} AND NOT {kinds.KIND_OF.format(var="n")} IN $excluded)
        RETURN d.id, d.name, {kinds.KIND_OF.format(var="d")}, length(path), [n IN nodes(path) | n.id], [n IN nodes(path) | n.name]
        """,
        {"id": target.id, "scopes": scopes, "excluded": sorted(kinds.NOT_DEPENDABLE)},
    ).result_set
    best: dict[str, Walked] = {}
    for thing_id, name, kind, hops, ids, names in rows:
        if thing_id == target.id:
            continue
        route = Walked(thing_id, name, kind, hops, tuple(ids), tuple(names))
        known = best.get(thing_id)
        if known is None or (route.hop, route.names) < (known.hop, known.names):
            best[thing_id] = route
    start = Walked(target.id, target.name, target.kind, 0, (target.id,), (target.name,))
    return [start, *sorted(best.values(), key=lambda thing: (thing.hop, thing.name))]


def _documents(graph, walked: list[Walked], scopes: list[str]) -> list[Reading]:
    """Each visible document that describes a walked thing, once, at the nearest thing it describes."""
    by_id = {thing.id: thing for thing in walked}
    rows = graph.ro_query(
        f"""
        MATCH (doc:Document)-[:DESCRIBES]->(t:Service)
        WHERE t.id IN $ids AND {SCOPED.format(var="doc")}
        RETURN doc.id, doc.name, t.id
        """,
        {"ids": list(by_id), "scopes": scopes},
    ).result_set
    nearest: dict[str, tuple[str, Walked]] = {}
    for doc_id, name, thing_id in rows:
        thing = by_id[thing_id]
        known = nearest.get(doc_id)
        if known is None or (thing.hop, thing.name) < (known[1].hop, known[1].name):
            nearest[doc_id] = (name, thing)
    authors = _authors(graph, list(nearest))
    return [Reading(doc_id, name, thing.hop, authors.get(doc_id, ()), describes=thing) for doc_id, (name, thing) in nearest.items()]


def _authors(graph, doc_ids: list[str]) -> dict[str, tuple[tuple[str, str], ...]]:
    if not doc_ids:
        return {}
    rows = graph.ro_query(
        "MATCH (p:Person)-[:AUTHORED]->(doc:Document) WHERE doc.id IN $ids RETURN doc.id, p.id, p.name ORDER BY p.name",
        {"ids": doc_ids},
    ).result_set
    authors: dict[str, list[tuple[str, str]]] = {}
    for doc_id, person_id, name in rows:
        authors.setdefault(doc_id, []).append((person_id, name))
    return {doc_id: tuple(people) for doc_id, people in authors.items()}


def _standing(graph, walked: list[Walked], scopes: list[str]) -> list[Standing]:
    """The decisions that stand about walked things, each once, nearest first, with their visible sources."""
    by_id = {thing.id: thing for thing in walked}
    rows = graph.ro_query(
        f"""
        MATCH (d:Decision)-[:ABOUT]->(t:Service)
        WHERE t.id IN $ids AND {SCOPED.format(var="d")}
        RETURN d.id, t.id
        """,
        {"ids": list(by_id), "scopes": scopes},
    ).result_set
    current: dict[str, decisions.DecisionRow | None] = {}
    standing: dict[str, Standing] = {}
    for decision_id, thing_id in rows:
        if decision_id not in current:
            current[decision_id] = decisions.current_decision(graph, decision_id, scopes)
        row = current[decision_id]
        if row is None:
            continue
        thing = by_id[thing_id]
        item = standing.setdefault(row.id, Standing(row, thing.hop, [], []))
        item.hop = min(item.hop, thing.hop)
        if thing not in item.about:
            item.about.append(thing)
    for item in standing.values():
        item.about.sort(key=lambda thing: (thing.hop, thing.name))
        item.sources = _sources(graph, item.decision.id, scopes)
    return sorted(standing.values(), key=lambda item: (item.hop, item.decision.name))


def _sources(graph, decision_id: str, scopes: list[str]) -> list[tuple[str, str, str, str, str | None]]:
    """What a decision is based on: anything but a document first, then documents, each by name."""
    rows = graph.ro_query(
        f"""
        MATCH (:Decision {{id: $id}})-[r:BASED_ON|CITES]->(source)
        WHERE {SCOPED.format(var="source")}
        RETURN type(r), source.id, source.name, labels(source)[0], source.kind
        """,
        {"id": decision_id, "scopes": scopes},
    ).result_set
    sources: dict[str, tuple[str, str, str, str, str | None]] = {}
    for rel, source_id, name, label, kind in rows:
        if source_id not in sources or rel == "BASED_ON":
            sources[source_id] = (rel, source_id, name, label, kind)
    return sorted(sources.values(), key=lambda source: (source[3] == "Document", source[2]))


def _owners(graph, thing_ids: list[str], scopes: list[str]) -> dict[str, list[tuple[str, str]]]:
    if not thing_ids:
        return {}
    rows = graph.ro_query(
        f"MATCH (p:Person)-[:OWNS]->(t:Service) WHERE t.id IN $ids AND {SCOPED.format(var='p')} RETURN t.id, p.id, p.name ORDER BY p.name",
        {"ids": thing_ids, "scopes": scopes},
    ).result_set
    owners: dict[str, list[tuple[str, str]]] = {}
    for thing_id, person_id, name in rows:
        owners.setdefault(thing_id, []).append((person_id, name))
    return owners


def _reading_line(reading: Reading) -> str:
    by = f" by {join_names([name for _id, name in reading.authors])}" if reading.authors else ""
    if reading.describes is None:
        return f"{reading.name}{by} (which {reading.behind} rests on)"
    thing = reading.describes
    if thing.hop == 0:
        where = f"{thing.subject} itself"
    elif thing.hop == 1:
        where = f"{thing.subject}, which it depends on"
    else:
        where = f"{thing.subject}, {thing.hop} hops out"
    return f"{reading.name}{by} ({where})"


def _standing_line(item: Standing) -> str:
    decided = f", {decisions.format_date(item.decision.decided_at)}" if item.decision.decided_at is not None else ""
    line = f"{item.decision.name} ({join_names([thing.subject for thing in item.about])}{decided})"
    named = [_source_name(name, label, kind) for _rel, _id, name, label, kind in item.sources]
    return line + (f", based on {join_names(named)}" if named else "")


def _source_name(name: str, label: str, kind: str | None) -> str:
    """"the Billing runbook", "the Pricing playbook", "TKT-102"."""
    if label == "Document":
        return f"the {name}"
    return kinds.subject(name, kind) if label == "Service" else name


def _either(names: list[str]) -> str:
    """A, B or C."""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def _route(thing: Walked) -> list[PathStep]:
    return [
        PathStep(thing.names[index], "DEPENDS_ON", thing.names[index + 1], from_id=thing.ids[index], to_id=thing.ids[index + 1], from_label="Service", to_label="Service")
        for index in range(len(thing.ids) - 1)
    ]


def _reading_steps(reading: Reading) -> list[PathStep]:
    """Who wrote it, then what it describes and the route there. The last step stands for the document alone."""
    steps = [PathStep(name, "AUTHORED", reading.name, from_id=person_id, to_id=reading.id, from_label="Person", to_label="Document") for person_id, name in reading.authors]
    if reading.describes is not None:
        thing = reading.describes
        steps += _route(thing)
        steps.append(PathStep(reading.name, "DESCRIBES", thing.name, from_id=reading.id, to_id=thing.id, from_label="Document", to_label="Service"))
    return steps


def _standing_steps(item: Standing) -> list[PathStep]:
    decision = item.decision
    steps = [step for thing in item.about for step in _route(thing)]
    steps += [PathStep(decision.name, "ABOUT", thing.name, from_id=decision.id, to_id=thing.id, from_label="Decision", to_label="Service") for thing in item.about]
    steps += [PathStep(decision.name, rel, name, from_id=decision.id, to_id=source_id, from_label="Decision", to_label=label) for rel, source_id, name, label, _kind in item.sources]
    return steps
