"""A team's page: who is in it, what it runs grouped by kind, the decisions that stand, and what was said.

Read-only and public, like the ask page: it reads what a public channel reads (``org_shared``).
Every fact on the page is an edge, and the page carries those edges as a path so it can draw
them: ``Person -[MEMBER_OF]-> Team``, ``Team -[RUNS]-> thing``, ``Person -[OWNS]-> thing``.

What a team runs is a ``Service`` node with a ``kind`` (``kinds.SERVICE_KINDS``). A node
written before ``kind`` existed counts as a service, so engineering teams and any other team
share one model and one set of queries; ``kinds`` supplies the wording ("run by" for a
process, "stalls" rather than "goes down").

A decision stands when no decision the reader can see supersedes it. Each standing decision
names the one it replaced, with both dates, so the page shows that knowledge stays current.

A team is tied to others by what it runs: ``DEPENDS_ON`` edges that cross into a thing another
team ``RUNS``. The page lists those ties both ways (what this team depends on, and who depends
on it) and draws the edges. It also carries the team's rows of the risk map, from the same
query and scoring as the ``risk_map`` tool, so the team page and the tool agree row for row.

A section with nothing in it is not shown as an empty box: the payload names the empty ones
(``empty``), and the page says in one line what the graph does not hold yet for this team.
"""

from dataclasses import dataclass, field

from src.db import falkordb_client
from src.db.falkordb_client import connect
from src.domain.knowledge import kinds
from src.domain.knowledge.ask_api import ANSWERS, graph_of
from src.domain.knowledge.decisions import format_date, to_millis
from src.domain.knowledge.dto import PathStep, day_from
from src.domain.knowledge.resolve import ORG_SHARED, SCOPED, join_names

DEFAULT_SCOPES = (ORG_SHARED,)
KINDS = kinds.SERVICE_KINDS
KIND_TITLES = {
    "service": "Services",
    "system": "Systems",
    "process": "Processes",
    "product": "Products",
    "account": "Accounts",
    "policy": "Policies",
    "playbook": "Playbooks",
}
# Ids are slugs; anything longer is not one, and is not looked up or cached.
MAX_TEAM_ID = 80
DECISION_LIMIT = 8
TICKET_LIMIT = 10
MEMORY_LIMIT = 5
# The sections a team can have nothing in, in page order, with the words the page uses for them.
OPTIONAL_SECTIONS = (("links", "links to other teams"), ("decisions", "decisions"), ("tickets", "open tickets"), ("memories", "chat"))


@dataclass(frozen=True)
class TeamSummary:
    id: str
    name: str
    members: int
    owns: int
    kinds: list[str]

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "members": self.members, "owns": self.owns, "kinds": list(self.kinds)}


@dataclass(frozen=True)
class Member:
    id: str
    name: str
    role: str
    owns: list[str]

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "role": self.role, "owns": list(self.owns)}


@dataclass(frozen=True)
class OwnedThing:
    id: str
    name: str
    kind: str
    summary: str
    owners: list[str]
    # How many services depend on it directly: the outage question asks about the one most depended on.
    dependents: int = 0

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind, "summary": self.summary, "owners": list(self.owners), "dependents": self.dependents}


@dataclass(frozen=True)
class KindGroup:
    kind: str
    title: str
    items: list[OwnedThing]

    @property
    def owner_label(self) -> str:
        """"Run by" for processes, "Owned by" for everything else."""
        return kinds.capitalized(kinds.owned(self.kind))

    def to_dict(self) -> dict:
        return {"kind": self.kind, "title": self.title, "owner_label": self.owner_label, "items": [item.to_dict() for item in self.items]}


@dataclass(frozen=True)
class StandingDecision:
    id: str
    name: str
    summary: str
    decided_on: str
    about: list[str]
    proposed_by: list[str]
    replaced: str | None = None
    replaced_on: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "summary": self.summary,
            "decided_on": self.decided_on,
            "about": list(self.about),
            "proposed_by": list(self.proposed_by),
            "replaced": self.replaced,
            "replaced_on": self.replaced_on,
        }


@dataclass(frozen=True)
class OpenTicket:
    id: str
    title: str
    about: str

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "about": self.about}


@dataclass(frozen=True)
class TeamMemory:
    text: str
    learned_at: str
    author: str
    about: str

    def to_dict(self) -> dict:
        return {"text": self.text, "learned_at": self.learned_at, "author": self.author, "about": self.about}


@dataclass(frozen=True)
class TeamLink:
    """Another team this one is tied to by ``DEPENDS_ON``, with the pairs of things that tie them.

    Each pair reads in the edge's direction: (the thing that depends, the thing it depends on),
    each end as (id, name).
    """

    team_id: str
    team: str
    pairs: tuple[tuple[tuple[str, str], tuple[str, str]], ...]

    def to_dict(self) -> dict:
        return {"id": self.team_id, "name": self.team, "pairs": [[dependent[1], dependency[1]] for dependent, dependency in self.pairs]}


@dataclass
class TeamPage:
    id: str
    name: str
    summary: str
    company: str | None
    members: list[Member] = field(default_factory=list)
    owns: list[KindGroup] = field(default_factory=list)
    decisions: list[StandingDecision] = field(default_factory=list)
    tickets: list[OpenTicket] = field(default_factory=list)
    memories: list[TeamMemory] = field(default_factory=list)
    # Teams whose things this team's things depend on, and teams whose things depend on this team's.
    depends_on: list[TeamLink] = field(default_factory=list)
    depended_on_by: list[TeamLink] = field(default_factory=list)
    # The team's rows of the risk map, as the grid the page draws; None when it runs nothing.
    risk: dict | None = None
    path: list[PathStep] = field(default_factory=list)
    # Questions the page offers; each is a read the ask box answers with a path.
    questions: list[str] = field(default_factory=list)
    # One line a reader takes in first: who the team leans on, who leans on it, what rests on one person.
    glance: str = ""
    scopes: list[str] = field(default_factory=lambda: list(DEFAULT_SCOPES))

    @property
    def links(self) -> list[TeamLink]:
        return [*self.depends_on, *self.depended_on_by]

    def empty_sections(self) -> list[str]:
        """The optional sections this team has nothing in, as the page names them, in page order."""
        return [label for section, label in OPTIONAL_SECTIONS if not getattr(self, section)]

    def to_dict(self) -> dict:
        nodes, edges = graph_of(self.path, {thing.id: thing.kind for group in self.owns for thing in group.items})
        return {
            "id": self.id,
            "name": self.name,
            "summary": self.summary,
            "company": self.company,
            "members": [member.to_dict() for member in self.members],
            "owns": [group.to_dict() for group in self.owns],
            "decisions": [decision.to_dict() for decision in self.decisions],
            "tickets": [ticket.to_dict() for ticket in self.tickets],
            "memories": [memory.to_dict() for memory in self.memories],
            "depends_on": [link.to_dict() for link in self.depends_on],
            "depended_on_by": [link.to_dict() for link in self.depended_on_by],
            "risk": self.risk,
            "path": [step.to_dict() for step in self.path],
            "nodes": [node.to_dict() for node in nodes],
            "edges": [edge.to_dict() for edge in edges],
            "questions": list(self.questions),
            "glance": self.glance,
            "empty": self.empty_sections(),
            "scopes": list(self.scopes),
        }


def public_teams() -> list[TeamSummary]:
    """Every team a public channel can see, computed at most once per cache period."""
    return ANSWERS.get_or_compute(("teams", falkordb_client.graph_name()), list_teams)


def public_team_page(team_id: str) -> TeamPage | None:
    """One team's page for a public reader, computed at most once per cache period."""
    return ANSWERS.get_or_compute(("team", falkordb_client.graph_name(), team_id), lambda: team_page(team_id))


def list_teams(scopes=DEFAULT_SCOPES) -> list[TeamSummary]:
    _, graph = connect()
    rows = graph.ro_query(
        f"""
        MATCH (t:Team)
        WHERE {SCOPED.format(var="t")}
        OPTIONAL MATCH (p:Person)-[:MEMBER_OF]->(t)
        WITH t, count(DISTINCT p) AS members
        OPTIONAL MATCH (t)-[:RUNS]->(s:Service)
        WHERE {SCOPED.format(var="s")}
        RETURN t.id, t.name, members, count(DISTINCT s), collect(DISTINCT {kinds.KIND_OF.format(var="s")})
        ORDER BY t.name
        """,
        {"scopes": list(scopes)},
    ).result_set
    return [TeamSummary(team_id, name, members, owns, _kind_order([kinds.kind_or_default(kind) for kind in found]) if owns else []) for team_id, name, members, owns, found in rows]


def team_page(team_id: str, scopes=DEFAULT_SCOPES) -> TeamPage | None:
    """The team's page, or None when no team with that id is visible."""
    _, graph = connect()
    params = {"id": team_id, "scopes": list(scopes)}
    found = graph.ro_query(
        f"""
        MATCH (t:Team {{id: $id}})
        WHERE {SCOPED.format(var="t")}
        OPTIONAL MATCH (t)-[:PART_OF]->(c:Company)
        RETURN t.id, t.name, coalesce(t.summary, t.description, ''), c.name
        LIMIT 1
        """,
        params,
    ).result_set
    if not found:
        return None
    _, name, summary, company = found[0]
    page = TeamPage(team_id, name, summary, company, scopes=list(scopes))

    owners: dict[str, list[tuple[str, str]]] = {}
    for service_id, owner_id, owner_name in graph.ro_query(
        f"""
        MATCH (:Team {{id: $id}})-[:RUNS]->(s:Service)<-[:OWNS]-(o:Person)
        WHERE {SCOPED.format(var="s")} AND {SCOPED.format(var="o")}
        RETURN s.id, o.id, o.name ORDER BY o.name
        """,
        params,
    ).result_set:
        owners.setdefault(service_id, []).append((owner_id, owner_name))

    things = []
    for service_id, service_name, kind, service_summary, dependents in graph.ro_query(
        f"""
        MATCH (:Team {{id: $id}})-[:RUNS]->(s:Service)
        WHERE {SCOPED.format(var="s")}
        OPTIONAL MATCH (d:Service)-[:DEPENDS_ON]->(s)
        WHERE {SCOPED.format(var="d")}
        RETURN s.id, s.name, {kinds.KIND_OF.format(var="s")}, coalesce(s.summary, ''), count(DISTINCT d)
        ORDER BY s.name
        """,
        params,
    ).result_set:
        things.append(OwnedThing(service_id, service_name, kinds.kind_or_default(kind), service_summary, [owner for _, owner in owners.get(service_id, [])], dependents))
    page.owns = group_by_kind(things)

    owned_by: dict[str, list[str]] = {}
    for thing in things:
        for owner_id, _ in owners.get(thing.id, []):
            owned_by.setdefault(owner_id, []).append(thing.name)
    for person_id, person_name, role in graph.ro_query(
        f"""
        MATCH (p:Person)-[:MEMBER_OF]->(:Team {{id: $id}})
        WHERE {SCOPED.format(var="p")}
        RETURN p.id, p.name, coalesce(p.role, '') ORDER BY p.name
        """,
        params,
    ).result_set:
        page.members.append(Member(person_id, person_name, role, owned_by.get(person_id, [])))

    page.decisions = _standing_decisions(graph, params)
    page.tickets = [
        OpenTicket(ticket_id, title, about)
        for ticket_id, title, about in graph.ro_query(
            f"""
            MATCH (:Team {{id: $id}})-[:RUNS]->(s:Service)<-[:ABOUT]-(k:Ticket)
            WHERE {SCOPED.format(var="s")} AND {SCOPED.format(var="k")} AND coalesce(k.state, 'open') <> 'closed'
            RETURN k.id, coalesce(k.title, k.name), s.name ORDER BY k.name LIMIT {TICKET_LIMIT}
            """,
            params,
        ).result_set
    ]
    page.memories = _recent_memories(graph, params)
    page.depends_on, page.depended_on_by = _team_links(graph, params)
    if things:
        from src.domain.knowledge import risk_map  # risk_map reaches the catalogue, which reaches this module

        page.risk = risk_map.grid(risk_map.rows(graph, list(scopes), [thing.id for thing in things]))
    page.path = team_path(page)
    page.questions = team_questions(page)
    page.glance = team_glance(page)
    return page


def group_by_kind(things: list[OwnedThing]) -> list[KindGroup]:
    """Owned things grouped by kind, in the order of ``KINDS``; an unknown kind follows, alphabetically."""
    groups: dict[str, list[OwnedThing]] = {}
    for thing in things:
        groups.setdefault(thing.kind, []).append(thing)
    return [KindGroup(kind, KIND_TITLES.get(kind, kind.capitalize()), groups[kind]) for kind in _kind_order(list(groups))]


def team_path(page: TeamPage) -> list[PathStep]:
    """The edges the page shows: membership, what the team runs, and who owns each thing."""
    steps: list[PathStep] = []
    for member in page.members:
        steps.append(PathStep(member.name, "MEMBER_OF", page.name, from_id=member.id, to_id=page.id, from_label="Person", to_label="Team"))
    owner_ids = {member.name: member.id for member in page.members}
    for group in page.owns:
        for thing in group.items:
            steps.append(PathStep(page.name, "RUNS", thing.name, from_id=page.id, to_id=thing.id, from_label="Team", to_label="Service"))
    for group in page.owns:
        for thing in group.items:
            for owner in thing.owners:
                steps.append(PathStep(owner, "OWNS", thing.name, from_id=owner_ids.get(owner), to_id=thing.id, from_label="Person", to_label="Service"))
    # Then the ties to other teams: each DEPENDS_ON edge, and the other team running its end of it.
    for link, ours_first in [*((link, True) for link in page.depends_on), *((link, False) for link in page.depended_on_by)]:
        for (dependent_id, dependent), (dependency_id, dependency) in link.pairs:
            steps.append(PathStep(dependent, "DEPENDS_ON", dependency, from_id=dependent_id, to_id=dependency_id, from_label="Service", to_label="Service"))
            theirs_id, theirs = (dependency_id, dependency) if ours_first else (dependent_id, dependent)
            steps.append(PathStep(link.team, "RUNS", theirs, from_id=link.team_id, to_id=theirs_id, from_label="Team", to_label="Service"))
    return _unique(steps)


def _unique(steps: list[PathStep]) -> list[PathStep]:
    """Each edge once, in order of first appearance."""
    seen: set[tuple] = set()
    kept = []
    for step in steps:
        key = (step.from_name, step.rel, step.to_name)
        if key not in seen:
            seen.add(key)
            kept.append(step)
    return kept


def team_questions(page: TeamPage) -> list[str]:
    """Reads worth asking about this team, in the words the keyword router answers.

    Each question must have an answer with a path: who owns the first thing that has an owner;
    what breaks if the thing most depended on fails, worded by kind (an account never has a
    blast radius, and nothing is asked when nothing depends on what the team runs); why the
    newest standing decision was made; and how the first member is connected to the last thing.
    """
    things = [thing for group in page.owns for thing in group.items]
    questions = []
    owned = [thing for thing in things if thing.owners]
    if owned:
        questions.append(f"who owns {owned[0].name}, and who else has worked on it")
    dependable = [thing for thing in things if thing.kind not in kinds.NOT_DEPENDABLE and thing.dependents > 0]
    if dependable:
        most = max(dependable, key=lambda thing: thing.dependents)
        questions.append(f"what breaks if {most.name} {kinds.down(most.kind)}")
    if page.decisions:
        questions.append(f"why {page.decisions[0].name[:1].lower()}{page.decisions[0].name[1:]}")
    if page.members and len(things) > 1:
        questions.append(f"how is {page.members[0].name} connected to {things[-1].name}")
    # Then the two things only this page knows: who the riskiest thing rests on, and what it leans on elsewhere.
    alone = [row for row in _risk_rows(page) if row["flags"].get("one_person") and row["owners"]]
    if alone:
        questions.append(f"what if {alone[0]['owners'][0]} leaves")
    if page.depends_on:
        dependent = page.depends_on[0].pairs[0][0][1]
        questions.append(f"what does {dependent} depend on")
    return questions


def team_glance(page: TeamPage) -> str:
    """The team in one line, from the page's own rows: the teams it depends on and that depend on it,
    each with the number of DEPENDS_ON edges between them, and how much of what it runs rests on
    one person. Every number is a count of rows the page lists below it."""
    edges = lambda link: f"{len(link.pairs)} edge{'s' if len(link.pairs) != 1 else ''}"  # noqa: E731
    parts = []
    if page.depends_on:
        parts.append(f"{page.name} depends on {join_names([f'{link.team} ({edges(link)})' for link in page.depends_on])}")
    if page.depended_on_by:
        others = [f"{link.team} ({edges(link)})" for link in page.depended_on_by]
        parts.append(f"{join_names(others)} depend{'s' if len(others) == 1 else ''} on {page.name}")
    rows = _risk_rows(page)
    if rows:
        alone = [row["name"] for row in rows if row["flags"].get("one_person")]
        if alone:
            verb = "rests" if len(alone) == 1 else "rest"
            parts.append(f"{len(alone)} of the {len(rows)} things it runs {verb} on one person: {join_names(alone)}")
        else:
            parts.append(f"none of the {len(rows)} things it runs rests on one person")
    return " ".join(f"{part[:1].upper()}{part[1:]}." for part in parts)


def _risk_rows(page: TeamPage) -> list[dict]:
    return list((page.risk or {}).get("rows") or [])


def _team_links(graph, params: dict) -> tuple[list[TeamLink], list[TeamLink]]:
    """The other teams this team depends on, and the ones that depend on it, by ``DEPENDS_ON``."""
    depends_on = graph.ro_query(
        f"""
        MATCH (:Team {{id: $id}})-[:RUNS]->(s:Service)-[:DEPENDS_ON]->(u:Service)<-[:RUNS]-(other:Team)
        WHERE other.id <> $id AND {SCOPED.format(var="s")} AND {SCOPED.format(var="u")} AND {SCOPED.format(var="other")}
        RETURN DISTINCT other.id, other.name, s.id, s.name, u.id, u.name
        ORDER BY other.name, s.name, u.name
        """,
        params,
    ).result_set
    depended_on_by = graph.ro_query(
        f"""
        MATCH (:Team {{id: $id}})-[:RUNS]->(s:Service)<-[:DEPENDS_ON]-(d:Service)<-[:RUNS]-(other:Team)
        WHERE other.id <> $id AND {SCOPED.format(var="s")} AND {SCOPED.format(var="d")} AND {SCOPED.format(var="other")}
        RETURN DISTINCT other.id, other.name, d.id, d.name, s.id, s.name
        ORDER BY other.name, d.name, s.name
        """,
        params,
    ).result_set
    return group_links(depends_on), group_links(depended_on_by)


def group_links(rows: list) -> list[TeamLink]:
    """Rows of (team id, team name, dependent id, dependent, dependency id, dependency) as one link
    per team, in row order."""
    teams: dict[str, tuple[str, list]] = {}
    for team_id, team, dependent_id, dependent, dependency_id, dependency in rows:
        pairs = teams.setdefault(team_id, (team, []))[1]
        pair = ((dependent_id, dependent), (dependency_id, dependency))
        if pair not in pairs:
            pairs.append(pair)
    return [TeamLink(team_id, team, tuple(pairs)) for team_id, (team, pairs) in teams.items()]


def _standing_decisions(graph, params: dict) -> list[StandingDecision]:
    rows = graph.ro_query(
        f"""
        MATCH (:Team {{id: $id}})-[:RUNS]->(s:Service)<-[:ABOUT]-(d:Decision)
        WHERE {SCOPED.format(var="s")} AND {SCOPED.format(var="d")}
        OPTIONAL MATCH (newer:Decision)-[:SUPERSEDES]->(d)
        WHERE {SCOPED.format(var="newer")}
        WITH d, collect(DISTINCT s.name) AS about, count(newer) AS newer
        WHERE newer = 0
        OPTIONAL MATCH (p:Person)-[:PROPOSED]->(d)
        WITH d, about, collect(DISTINCT p.name) AS proposers
        OPTIONAL MATCH (d)-[:SUPERSEDES]->(old:Decision)
        WHERE {SCOPED.format(var="old")}
        RETURN d.id, d.name, coalesce(d.summary, ''), d.decided_at, about, proposers, old.name, old.decided_at
        ORDER BY coalesce(d.decided_at, 0) DESC, d.name
        """,
        params,
    ).result_set
    decisions: dict[str, StandingDecision] = {}
    for decision_id, name, summary, decided_at, about, proposers, old_name, old_decided_at in rows:
        if decision_id in decisions:
            continue  # superseded more than one: the newest replacement row comes first
        decisions[decision_id] = StandingDecision(
            decision_id,
            name,
            summary,
            _day(decided_at),
            sorted(about),
            sorted(name for name in proposers if name),
            old_name,
            _day(old_decided_at) if old_name else None,
        )
    return list(decisions.values())[:DECISION_LIMIT]


def _recent_memories(graph, params: dict) -> list[TeamMemory]:
    rows = graph.ro_query(
        f"""
        MATCH (:Team {{id: $id}})-[:RUNS]->(s:Service)<-[:MENTIONS]-(m:Memory)
        WHERE {SCOPED.format(var="s")} AND m.scope IN $scopes AND NOT coalesce(m.is_forgotten, false)
        OPTIONAL MATCH (a:SlackUser)-[:SAID]->(m)
        RETURN m.id, m.text, m.created_at, m.ts, s.name, coalesce(a.name, a.id, 'someone')
        ORDER BY coalesce(m.created_at, 0) DESC, s.name LIMIT {MEMORY_LIMIT * 4}
        """,
        params,
    ).result_set
    seen: set[str] = set()
    memories: list[TeamMemory] = []
    for memory_id, text, created_at, ts, about, author in rows:
        if memory_id in seen or not text:
            continue
        seen.add(memory_id)
        memories.append(TeamMemory(text, day_from(created_at, ts), author, about))
    return memories[:MEMORY_LIMIT]


def _kind_order(kinds: list[str]) -> list[str]:
    known = [kind for kind in KINDS if kind in kinds]
    return known + sorted(kind for kind in set(kinds) if kind not in KINDS)


def _day(value) -> str:
    millis = to_millis(value)
    return format_date(millis) if millis is not None else ""
