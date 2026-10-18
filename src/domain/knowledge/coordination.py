"""Who should know about a change but wasn't involved: the owners a change reaches.

``who_should_know`` takes a pull request (``notebook#45``, ``#45``, ``PR 45``) or a service.
For a pull request, what changed is every service it ``TOUCHES`` and the people in the loop
are its author, its reviewers, and anyone asked to review it. For a service, what changed is the service and the people in
the loop are its owners. The owners of what changed come first (0 hops); then the walk
follows ``DEPENDS_ON`` back from what changed for up to two hops, with every node readable
and accounts kept off every route as in ``blast_radius``, and names the owners of each
dependent who were not in the loop.

Each one is tagged by whether they have worked with the author (for a service, its owner)
before: pull requests both wrote or reviewed under any GitHub account linked to them,
tickets both worked on, decisions both proposed or approved, counted only where the asker
may read them. Someone who owns a dependent and has never worked with the author is the
gap Cataldo & Herbsleb measured: coordination the dependencies call for that nobody does
(IEEE TSE 2013).

A pull request is looked up by its number and repo, never matched loosely: ``docs#1234``
is not ``docs#12345``. A document or ticket is not a change and is refused as one. Hops and
shared work are counted from the edges the reply cites, so the answer is exact and the
same on every run. Nothing is ranked by a graph algorithm.
"""

import re
from dataclasses import dataclass, field

from src.db.falkordb_client import connect
from src.domain.knowledge import kinds, resolve
from src.domain.knowledge.dto import PathStep, ToolResult
from src.domain.knowledge.resolve import SCOPED, Candidate, Entity, Resolution, join_names

TOOL = "who_should_know"
MAX_HOPS = 2
# People told in full; the rest are named in one closing sentence.
MAX_LISTED = 3
# Shared pull requests, tickets or decisions named for one person.
MAX_EXAMPLES = 2
# What a question that names no pull request is resolved against. Pull requests are looked up
# by number instead, so a pull request's title never hides a service. A document or ticket
# resolves only to be refused: it is asked about, not changed.
LABELS = ("Service", "Document", "Ticket")
WHAT = "pull request or service"

# "notebook#45" or "acme/notebook#45": a pull request named with its repo.
_IN_REPO = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)?[\w.-]+)#(\d+)\b")
# "#45", "PR 45", "PR#45", "pull request 45", or the bare number a model may fill in.
_NUMBER = re.compile(r"(?:\b(?:pr|pull request)\s*#?\s*|(?<![\w#])#)(\d+)\b|^\s*(\d+)\s*$", re.IGNORECASE)
# Words that sit before "#45" without naming a repo; after "issue" the number is not a pull request's.
_NOT_A_REPO = frozenset({"pr", "pull", "issue", "issues"})
_ISSUE_BEFORE = re.compile(r"\bissues?\s*$", re.IGNORECASE)
# Past work in this order when a person shares several: pull requests, tickets, decisions.
_WORK_ORDER = {"PullRequest": 0, "Ticket": 1, "Decision": 2}

_SHARED_WORK = f"""
MATCH (mine:GitHubUser)-[m:AUTHORED|REVIEWED]->(work:PullRequest)<-[t:AUTHORED|REVIEWED]-(theirs:GitHubUser)
WHERE mine.id IN $owner_github AND theirs.id IN $anchor_github AND mine.id <> theirs.id AND NOT work.id IN $skip
  AND {SCOPED.format(var="mine")} AND {SCOPED.format(var="work")} AND {SCOPED.format(var="theirs")}
RETURN 'PullRequest' AS label, mine.id AS mine, theirs.id AS theirs, work.id AS id, work.name AS name, type(m) AS mine_rel, type(t) AS their_rel
UNION ALL
MATCH (mine:Person)-[m:WORKED_ON]->(work:Ticket)<-[t:WORKED_ON]-(theirs:Person)
WHERE mine.id IN $owners AND theirs.id IN $anchor_people AND mine.id <> theirs.id
  AND {SCOPED.format(var="mine")} AND {SCOPED.format(var="work")} AND {SCOPED.format(var="theirs")}
RETURN 'Ticket' AS label, mine.id AS mine, theirs.id AS theirs, work.id AS id, work.name AS name, type(m) AS mine_rel, type(t) AS their_rel
UNION ALL
MATCH (mine:Person)-[m:PROPOSED|APPROVED]->(work:Decision)<-[t:PROPOSED|APPROVED]-(theirs:Person)
WHERE mine.id IN $owners AND theirs.id IN $anchor_people AND mine.id <> theirs.id
  AND {SCOPED.format(var="mine")} AND {SCOPED.format(var="work")} AND {SCOPED.format(var="theirs")}
RETURN 'Decision' AS label, mine.id AS mine, theirs.id AS theirs, work.id AS id, work.name AS name, type(m) AS mine_rel, type(t) AS their_rel
"""


@dataclass(frozen=True)
class Who:
    """Someone in the loop: an author or reviewer, or an owner of the service that changes."""

    name: str
    rel: str
    person_id: str | None
    # Every GitHub account linked to them, the one on this pull request first.
    github_ids: tuple[str, ...]

    @property
    def node(self) -> tuple[str, str]:
        """The id and label a cited edge starts from: the Person when linked, else the GitHub account."""
        return (self.person_id, "Person") if self.person_id else (self.github_ids[0], "GitHubUser")


@dataclass(frozen=True)
class Dependent:
    """Something a person owns that the change reaches: 0 hops when it is what changed."""

    id: str
    name: str
    kind: str
    hops: int
    # Every shortest route as (ids, names), from the dependent to what changed; none at 0 hops.
    routes: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = ()


@dataclass(frozen=True)
class Shared:
    """One pull request, ticket or decision an owner and someone in the loop both worked on."""

    label: str
    id: str
    name: str
    anchor: str
    steps: tuple[PathStep, PathStep]

    @property
    def named(self) -> str:
        return f"*{self.name}*" if self.label == "Decision" else self.name


@dataclass
class Owner:
    id: str
    name: str
    github_ids: tuple[str, ...]
    things: list[Dependent] = field(default_factory=list)
    shared: list[Shared] = field(default_factory=list)


@dataclass(frozen=True)
class Covered:
    """An owner of a dependent who was in the loop, and how."""

    thing: Dependent
    person_id: str
    name: str
    who: Who


@dataclass
class Change:
    """What changed, who was in the loop, and whose past work the rest are measured against."""

    subject: Entity
    changed: list[Entity]
    in_loop: list[Who]
    # The author of a pull request, or the owners of a service.
    anchors: list[Who]
    # The edges that say what changed: TOUCHES from a pull request, or OWNS of the service.
    lead: list[PathStep]
    # What to do about someone who has never worked with the author, given where the change stands.
    introduce: str

    @property
    def is_pull_request(self) -> bool:
        return self.subject.label == "PullRequest"


def who_should_know(question: str, scopes: list[str]) -> ToolResult:
    """Owners of what a change touches and of what depends on it, up to two hops, who were not in the loop."""
    _, graph = connect()
    named = _resolve(graph, question, scopes)
    if isinstance(named, ToolResult):
        return named
    entity, candidates = named
    return _report(graph, entity, candidates, scopes)[0]


def pull_request_report(pr_id: str, scopes: list[str]) -> tuple[ToolResult, list[str]] | None:
    """``who_should_know`` for one pull request by id, and the names of the people it flags. None when it is not readable.

    For callers that act on the answer rather than show it, such as the Slack heads-up on a
    new pull request (``src/domain/github/alerts.py``).
    """
    _, graph = connect()
    rows = graph.ro_query(
        f"MATCH (pr:PullRequest {{id: $id}}) WHERE {SCOPED.format(var='pr')} RETURN pr.name",
        {"id": pr_id, "scopes": scopes},
    ).result_set
    if not rows:
        return None
    result, missing = _report(graph, Entity(pr_id, rows[0][0], "PullRequest"), [], scopes)
    return result, [owner.name for owner in missing]


def _report(graph, entity: Entity, candidates: list[str], scopes: list[str]) -> tuple[ToolResult, list[Owner]]:
    """The answer for a pull request or service already resolved, and the owners it flags."""
    if entity.label == "Service" and kinds.kind_or_default(entity.kind) in kinds.NOT_DEPENDABLE:
        answer = f"{entity.name} is an {entity.kind}: nothing depends on it. Ask who owns it or who knows it instead."
        return ToolResult(tool=TOOL, answer=answer, candidates=candidates), []
    change = _pull_request(graph, entity, scopes) if entity.label == "PullRequest" else _service(graph, entity, scopes)
    result = ToolResult(tool=TOOL, answer="", candidates=candidates)
    result.extend(change.lead)
    if not change.changed:
        result.answer = f"{entity.name} touches no service in the graph, so nothing depends on what it changes."
        return result, []

    result.depth = MAX_HOPS
    dependents = _dependents(graph, [thing.id for thing in change.changed], scopes)
    # A service's own owners are its anchors, already in the loop; a pull request's are not.
    changed = [Dependent(thing.id, thing.name, thing.kind, 0) for thing in change.changed] if change.is_pull_request else []
    owners = _owners(graph, [thing.id for thing in changed] + list(dependents), scopes)
    missing, covered, unowned = _split(change, [*changed, *dependents.values()], owners)
    # Past work is only ever named for the people told in full.
    _shared_work(graph, change, missing[:MAX_LISTED], scopes)
    result.answer = _answer(change, dependents, missing, covered, unowned)
    _cite(result, change, missing, covered, unowned)
    return result, missing


def pr_reference(text: str) -> tuple[str | None, int] | None:
    """The pull request a text names, as (repo, number); the repo is None for "#45" or "PR 45". None when it names none.

    "PR#45" names no repo, and "issue #12" names an issue, not pull request 12.
    """
    text = text or ""
    for match in _IN_REPO.finditer(text):
        if match.group(1).lower() not in _NOT_A_REPO:
            return match.group(1), int(match.group(2))
    for match in _NUMBER.finditer(text):
        if not _ISSUE_BEFORE.search(text[: match.start()]):
            return None, int(match.group(1) or match.group(2))
    return None


def pull_request_in(text: str, scopes: list[str], graph=None) -> Entity | None:
    """The one readable pull request a text names, or None. For a thread follow-up to find its subject."""
    reference = pr_reference(text)
    if reference is None:
        return None
    if graph is None:
        _, graph = connect()
    named = _pull_request_named(graph, text, reference, scopes)
    return named[0] if isinstance(named, tuple) else None


# --- resolving -----------------------------------------------------------------------------


def _resolve(graph, question: str, scopes: list[str]):
    """The pull request or service named, with the resolver's other candidates, or a refusal."""
    reference = pr_reference(question)
    if reference is not None:
        return _pull_request_named(graph, question, reference, scopes)
    resolution = resolve.resolve_name(question, LABELS, scopes, graph=graph)
    if not resolution.found:
        return _miss(resolution)
    entity = resolution.entity
    if entity.label != "Service":
        return _not_a_change(entity)
    return entity, resolution.candidate_names()


def _pull_request_named(graph, question: str, reference: tuple[str | None, int], scopes: list[str]):
    """Look a pull request up by number, then by repo when one is named, among those the asker can read.

    A private pull request the asker cannot read is refused exactly like one that does not
    exist. A number two repos share is narrowed by a repo the question names anywhere
    ("notebook #45", "PR 45 in notebook"), else both are offered.
    """
    repo, number = reference
    rows = graph.ro_query(
        f"""
        MATCH (pr:PullRequest {{number: $number}})
        WHERE {SCOPED.format(var="pr")}
        OPTIONAL MATCH (pr)-[:IN_REPO]->(r:Repo)
        WHERE {SCOPED.format(var="r")}
        RETURN pr.id, pr.name, coalesce(r.full_name, '')
        ORDER BY pr.name, coalesce(r.full_name, '')
        """,
        {"number": number, "scopes": scopes},
    ).result_set
    if repo is not None:
        rows = [row for row in rows if _in_repo(row, repo)]
        if not rows:
            return _near_miss(graph, question, f"{repo}#{number}", scopes)
    elif len(rows) > 1:
        words = set(resolve.tokens(question))
        rows = [row for row in rows if set(resolve.tokens(_repo_of(row))) <= words] or rows
    if len(rows) == 1:
        return Entity(rows[0][0], rows[0][1], "PullRequest"), []
    if rows:
        names = [name for _id, name, _full in rows]
        if len(set(names)) < len(names):
            names = [f"{full}#{number}" if full else name for _id, name, full in rows]
        names = names[: resolve.MAX_CANDIDATES]
        answer = f"More than one pull request is #{number}: {join_names(names)}. Name the repo, as in {names[0]}."
        return ToolResult(tool=TOOL, answer=answer, found=False, candidates=names)
    return _miss(Resolution(query=question, reason="no_match"))


def _in_repo(row: tuple, repo: str) -> bool:
    wanted, short, full = repo.lower(), _repo_of(row).lower(), row[2].lower()
    return wanted in (short, full) or (bool(full) and full.endswith("/" + wanted))


def _repo_of(row: tuple) -> str:
    """The repo part of a pull request's name: "notebook" for notebook#45."""
    return row[1].rsplit("#", 1)[0]


def _near_miss(graph, question: str, named: str, scopes: list[str]) -> ToolResult:
    """A repo#number that is no readable pull request: an issue by that name is refused as one; else the nearest names."""
    near = resolve.resolve_name(question, ("PullRequest", "Ticket"), scopes, graph=graph)
    if near.found and near.entity.label == "Ticket" and near.entity.name.lower() == named.lower():
        return _not_a_change(near.entity)
    # A loose match is never taken: "docs#1234" is not docs#12345, it is the nearest name.
    candidates = ([Candidate(near.entity, near.entity.name)] if near.found else []) + near.candidates
    return _miss(Resolution(query=question, reason="no_match", candidates=candidates[: resolve.MAX_CANDIDATES]))


def _not_a_change(entity: Entity) -> ToolResult:
    what = "document" if entity.label == "Document" else "ticket"
    answer = f"{entity.name} is a {what}, not a change. Name the pull request or service that changes, or ask who knows {entity.name}."
    return ToolResult(tool=TOOL, answer=answer, found=False, candidates=[entity.name])


def _miss(resolution: Resolution) -> ToolResult:
    return ToolResult(tool=TOOL, answer=resolve.describe_miss(resolution, WHAT), found=False, candidates=resolution.candidate_names())


# --- what changed and who was in the loop -------------------------------------------------


def _pull_request(graph, pr: Entity, scopes: list[str]) -> Change:
    params = {"id": pr.id, "scopes": scopes}
    rows = graph.ro_query(
        f"""
        MATCH (pr:PullRequest {{id: $id}})
        OPTIONAL MATCH (pr)-[:TOUCHES]->(s:Service)
        WHERE {SCOPED.format(var="s")}
        RETURN coalesce(pr.state, ''), coalesce(pr.merged, false), s.id, s.name, {kinds.KIND_OF.format(var="s")}
        ORDER BY s.name
        """,
        params,
    ).result_set
    changed = [Entity(service_id, name, "Service", kind) for _state, _merged, service_id, name, kind in rows if service_id is not None]
    state, merged = (rows[0][0], rows[0][1]) if rows else ("", False)
    people = graph.ro_query(
        f"""
        MATCH (gh:GitHubUser)-[r:AUTHORED|REVIEWED|REVIEW_REQUESTED]->(:PullRequest {{id: $id}})
        WHERE {SCOPED.format(var="gh")}
        OPTIONAL MATCH (gh)-[:IS]->(p:Person)
        WHERE {SCOPED.format(var="p")}
        OPTIONAL MATCH (p)<-[:IS]-(other:GitHubUser)
        WHERE {SCOPED.format(var="other")}
        RETURN type(r), gh.id, gh.login, p.id, p.name, collect(DISTINCT other.id)
        ORDER BY type(r), gh.login
        """,
        params,
    ).result_set
    in_loop = [
        Who(name or f"@{login}", rel, person_id, (github_id, *sorted(set(others) - {github_id})))
        for rel, github_id, login, person_id, name, others in people
    ]
    if merged:
        introduce = "introduce them now, since it has already merged"
    elif state == "closed":
        introduce = "introduce them if it is reopened"
    else:
        introduce = "introduce them before this merges"
    lead = [PathStep(pr.name, "TOUCHES", thing.name, from_id=pr.id, to_id=thing.id, from_label="PullRequest", to_label="Service") for thing in changed]
    return Change(pr, changed, in_loop, [who for who in in_loop if who.rel == "AUTHORED"], lead, introduce)


def _service(graph, service: Entity, scopes: list[str]) -> Change:
    owners = [Who(name, "OWNS", person_id, github_ids) for person_id, name, github_ids in _owners(graph, [service.id], scopes).get(service.id, [])]
    lead = [PathStep(who.name, "OWNS", service.name, from_id=who.person_id, to_id=service.id, from_label="Person", to_label="Service") for who in owners]
    return Change(service, [service], owners, owners, lead, "introduce them before it changes")


# --- the walk ------------------------------------------------------------------------------


def _dependents(graph, changed: list[str], scopes: list[str]) -> dict[str, Dependent]:
    """Everything that depends on what changed within two hops, at its shortest hop count, with every shortest route."""
    rows = graph.ro_query(
        f"""
        MATCH path = (dep:Service)-[:DEPENDS_ON*1..{MAX_HOPS}]->(changed:Service)
        WHERE changed.id IN $changed AND NOT dep.id IN $changed
          AND all(n IN nodes(path) WHERE {SCOPED.format(var="n")} AND NOT {kinds.KIND_OF.format(var="n")} IN $excluded)
        RETURN dep.id, dep.name, {kinds.KIND_OF.format(var="dep")}, length(path), [n IN nodes(path) | n.id], [n IN nodes(path) | n.name]
        ORDER BY length(path), dep.name
        """,
        {"changed": changed, "scopes": scopes, "excluded": sorted(kinds.NOT_DEPENDABLE)},
    ).result_set
    shortest: dict[str, Dependent] = {}
    for dep_id, name, kind, hops, ids, names in rows:
        route = (tuple(ids), tuple(names))
        known = shortest.get(dep_id)
        if known is None or hops < known.hops:
            shortest[dep_id] = Dependent(dep_id, name, kind, hops, (route,))
        elif hops == known.hops and route not in known.routes:
            shortest[dep_id] = Dependent(dep_id, name, kind, hops, (*known.routes, route))
    return shortest


def _owners(graph, ids: list[str], scopes: list[str]) -> dict[str, list[tuple[str, str, tuple[str, ...]]]]:
    """Owners of each thing, by name, with the GitHub accounts linked to each."""
    owners: dict[str, list[tuple[str, str, tuple[str, ...]]]] = {}
    if not ids:
        return owners
    for thing_id, person_id, name, github_ids in graph.ro_query(
        f"""
        MATCH (owner:Person)-[:OWNS]->(s:Service)
        WHERE s.id IN $ids AND {SCOPED.format(var="s")} AND {SCOPED.format(var="owner")}
        OPTIONAL MATCH (gh:GitHubUser)-[:IS]->(owner)
        WHERE {SCOPED.format(var="gh")}
        RETURN s.id, owner.id, owner.name, collect(DISTINCT gh.id)
        ORDER BY owner.name
        """,
        {"ids": ids, "scopes": scopes},
    ).result_set:
        owners.setdefault(thing_id, []).append((person_id, name, tuple(sorted(github_ids))))
    return owners


def _split(change: Change, things: list[Dependent], owners: dict[str, list]) -> tuple[list[Owner], list[Covered], list[Dependent]]:
    """Owners not in the loop, grouped by person in the order of their nearest thing; the dependents' owners who were; and the unowned dependents.

    Someone is in the loop when they are the same Person as an author or reviewer, or one
    of the GitHub accounts linked to them is. An owner with no linked account stays out of
    the loop: nothing ties them to the pull request.
    """
    rows: list[tuple[int, str, str, Dependent, str, tuple[str, ...]]] = []
    covered: list[Covered] = []
    unowned: list[Dependent] = []
    for thing in sorted(things, key=lambda thing: (thing.hops, thing.name)):
        if not owners.get(thing.id):
            if thing.hops:
                unowned.append(thing)
            continue
        for person_id, name, github_ids in owners[thing.id]:
            who = next((who for who in change.in_loop if who.person_id == person_id or set(who.github_ids) & set(github_ids)), None)
            if who is None:
                rows.append((thing.hops, thing.name, name, thing, person_id, github_ids))
            elif thing.hops:
                covered.append(Covered(thing, person_id, name, who))
    missing: dict[str, Owner] = {}
    for _hops, _thing, name, thing, person_id, github_ids in sorted(rows, key=lambda row: row[:3]):
        missing.setdefault(person_id, Owner(person_id, name, github_ids)).things.append(thing)
    return list(missing.values()), covered, unowned


def _shared_work(graph, change: Change, owners: list[Owner], scopes: list[str]) -> None:
    """Fill each owner's past work with the anchors: pull requests, then tickets, then decisions, by name.

    When co-owners of a service share the same work with someone, the one named and cited
    is the first by name, so the answer does not depend on the order rows come back in.
    """
    if not owners or not change.anchors:
        return
    anchors_by_github = {github_id: who for who in change.anchors for github_id in who.github_ids}
    anchors_by_person = {who.person_id: who for who in change.anchors if who.person_id}
    owners_by_github = {github_id: owner for owner in owners for github_id in owner.github_ids}
    owners_by_person = {owner.id: owner for owner in owners}
    rows = graph.ro_query(
        _SHARED_WORK,
        {
            "owner_github": list(owners_by_github),
            "anchor_github": list(anchors_by_github),
            "owners": list(owners_by_person),
            "anchor_people": list(anchors_by_person),
            "skip": [change.subject.id] if change.is_pull_request else [],
            "scopes": scopes,
        },
    ).result_set
    found = []
    for label, mine, theirs, work_id, name, mine_rel, their_rel in rows:
        owner = owners_by_github.get(mine) if label == "PullRequest" else owners_by_person.get(mine)
        anchor = anchors_by_github.get(theirs) if label == "PullRequest" else anchors_by_person.get(theirs)
        if owner is not None and anchor is not None:
            found.append((_WORK_ORDER[label], name, anchor.name, mine_rel, their_rel, work_id, label, owner, anchor))
    for _order, name, _anchor, mine_rel, their_rel, work_id, label, owner, anchor in sorted(found, key=lambda row: row[:6]):
        if any(shared.id == work_id for shared in owner.shared):
            continue
        anchor_id, anchor_label = anchor.node
        steps = (
            PathStep(owner.name, mine_rel, name, from_id=owner.id, to_id=work_id, from_label="Person", to_label=label),
            PathStep(anchor.name, their_rel, name, from_id=anchor_id, to_id=work_id, from_label=anchor_label, to_label=label),
        )
        owner.shared.append(Shared(label, work_id, name, anchor.name, steps))


# --- the reply -----------------------------------------------------------------------------


def _answer(change: Change, dependents: dict, missing: list[Owner], covered: list[Covered], unowned: list[Dependent]) -> str:
    subject = change.subject
    named = [kinds.subject(thing.name, thing.kind) for thing in change.changed]
    it = "it" if len(named) == 1 else "them"
    if change.is_pull_request:
        lead = f"{subject.name} changes {join_names(named)}."
        if missing:
            count = len(missing)
            owns_changed = any(thing.hops == 0 for owner in missing for thing in owner.things)
            what = f"what it changes or something that depends on {it}" if owns_changed else f"something that depends on {it}"
            lead += f" {count} {'person who owns' if count == 1 else 'people who own'} {what} {'wasn' if count == 1 else 'weren'}'t on the pull request."
        elif not dependents:
            return f"{lead} Nothing in the graph depends on {it}, so no one else needs to hear about it."
        elif covered:
            return _with_unowned(f"Everyone who owns something that depends on {_either(named)} was on {subject.name}.", unowned)
        else:
            return f"{lead} Nothing that depends on {it} has a named owner: {_unowned_list(unowned)}."
    else:
        thing = kinds.capitalized(named[0])
        if change.anchors:
            lead = f"{thing} is {kinds.owned(subject.kind)} {join_names([who.name for who in change.anchors])}."
        else:
            lead = f"{thing} has no named owner."
        if not dependents:
            return f"{lead} Nothing in the graph depends on it, so no one else needs to know when it changes."
        if not missing and covered:
            return _with_unowned(f"{lead} No one else owns anything that depends on it.", unowned)
        if not missing:
            return f"{lead} Nothing that depends on it has a named owner: {_unowned_list(unowned)}."
        count = len(missing)
        other = "other " if change.anchors else ""
        lead += f" If it changes, {count} {other}{'person owns' if count == 1 else 'people own'} something that depends on it."
    listed, rest = missing[:MAX_LISTED], missing[MAX_LISTED:]
    parts = [lead, *(_sentence(owner, change) for owner in listed)]
    if rest:
        parts.append(f"And {len(rest)} more: {join_names([owner.name for owner in rest])}.")
    return _with_unowned(" ".join(parts), unowned)


def _sentence(owner: Owner, change: Change) -> str:
    by_verb: dict[str, list[str]] = {}
    for thing in owner.things:
        verb = "runs" if kinds.kind_or_default(thing.kind) == "process" else "owns"
        where = "which it changes" if thing.hops == 0 else _hops(thing.hops)
        by_verb.setdefault(verb, []).append(f"{kinds.subject(thing.name, thing.kind)} ({where})")
    clauses = [f"{verb} {join_names(things)}" for verb, things in by_verb.items()]
    if not change.anchors:
        return f"{owner.name} {join_names(clauses)}."
    # "runs the Renewals process (1 hop), owns the Pricing playbook (2 hops) and worked with ...": one "and", before the past work.
    sentence = f"{owner.name} {', '.join(clauses)}"
    if not owner.shared:
        anchors = _either([who.name for who in change.anchors])
        return f"{sentence} and has never worked with {anchors} on a pull request, ticket or decision: {change.introduce}."
    shown = owner.shared[:MAX_EXAMPLES]
    by_anchor: dict[str, list[str]] = {}
    for shared in shown:
        by_anchor.setdefault(shared.anchor, []).append(shared.named)
    together = " and with ".join(f"{anchor} on {join_names(works)}" for anchor, works in by_anchor.items())
    total = f", {len(owner.shared)} times in all" if len(owner.shared) > len(shown) else ""
    return f"{sentence} and worked with {together}{total}, so a quick ping is enough."


def _with_unowned(text: str, unowned: list[Dependent]) -> str:
    if not unowned:
        return text
    return f"{text} {kinds.capitalized(_unowned_list(unowned))} {'has' if len(unowned) == 1 else 'have'} no named owner."


def _unowned_list(unowned: list[Dependent]) -> str:
    return join_names([f"{kinds.subject(dep.name, dep.kind)} ({_hops(dep.hops)})" for dep in unowned])


def _hops(count: int) -> str:
    return f"{count} hop{'s' if count != 1 else ''}"


def _either(names: list[str]) -> str:
    """A, B or C."""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


# --- the path ------------------------------------------------------------------------------


def _cite(result: ToolResult, change: Change, missing: list[Owner], covered: list[Covered], unowned: list[Dependent]) -> None:
    """For each person told in full: the route to each thing they own, their OWNS edge, then the shared work named.

    When nobody is missing, the dependents' owners who were in the loop and the edge that
    put them there. Things with no owner get their route, so every name in the reply has an edge.
    """
    listed, rest = missing[:MAX_LISTED], missing[MAX_LISTED:]
    for owner in listed:
        for thing in owner.things:
            _cite_route(result, thing)
            result.add(_owns(owner.id, owner.name, thing))
        for shared in owner.shared[:MAX_EXAMPLES]:
            result.extend(shared.steps)
    for owner in rest:
        for thing in owner.things:
            _cite_route(result, thing)
            result.add(_owns(owner.id, owner.name, thing))
    if not missing:
        subject = change.subject
        for entry in covered:
            _cite_route(result, entry.thing)
            result.add(_owns(entry.person_id, entry.name, entry.thing))
            if change.is_pull_request:
                who_id, who_label = entry.who.node
                result.add(PathStep(entry.who.name, entry.who.rel, subject.name, from_id=who_id, to_id=subject.id, from_label=who_label, to_label="PullRequest"))
    for thing in unowned:
        _cite_route(result, thing)


def _cite_route(result: ToolResult, thing: Dependent) -> None:
    """One shortest route, preferring one through things the path already cites, so the drawing stays small."""
    if not thing.routes:
        return
    cited = {node for step in result.path for node in (step.from_id, step.to_id)}
    ids, names = min(thing.routes, key=lambda route: (-sum(node in cited for node in route[0][1:-1]), route[1]))
    for index in range(len(ids) - 1):
        result.add(PathStep(names[index], "DEPENDS_ON", names[index + 1], from_id=ids[index], to_id=ids[index + 1], from_label="Service", to_label="Service"))


def _owns(person_id: str, name: str, thing: Dependent) -> PathStep:
    return PathStep(name, "OWNS", thing.name, from_id=person_id, to_id=thing.id, from_label="Person", to_label="Service")
