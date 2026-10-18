"""Org health: how teams depend on each other, and whether the decisions that stand still hold up.

Two company-level reads over the same graph the other tools walk:

``team_dependencies`` aggregates ``DEPENDS_ON`` across team boundaries:
``(t1:Team)-[:RUNS]->(a)-[:DEPENDS_ON]->(b)<-[:RUNS]-(t2:Team)`` with ``t1 <> t2``. For a named
team it tells who depends on what it runs (or, asked the other way, what it needs from other
teams), each team with the links that tie them, the teams that reach it only through other
things, and the pairs that depend on each other. With no team named it gives the company view:
every pair of teams, the busiest first. Accounts are not in the dependency graph
(``kinds.NOT_DEPENDABLE``), as in ``blast_radius`` and ``risk_map``.

``decision_health`` audits every standing decision, a decision no decision the reader can see
supersedes, against its own edges: no evidence (``BASED_ON``/``CITES``), no approver, a second
standing decision about the same thing, tickets or pull requests about what it is about that it
does not cite (``decisions.uncited_work``, what ``decision_pressure`` tells for one decision),
and age. It also tells the history, each replaced chain oldest first.

Both return a ``grid`` for a page to draw. A team or thing that is not in the graph gets the
refusal every tool shares, after ``fallback.before_refusing`` has had its turn.
"""

from dataclasses import dataclass, field

from src.db.falkordb_client import connect
from src.domain.knowledge import decisions, fallback, kinds, resolve
from src.domain.knowledge.algorithms import MAX_DEPTH
from src.domain.knowledge.dto import PathStep, ToolResult
from src.domain.knowledge.resolve import SCOPED, Entity, join_names

TEAM_TOOL = "team_dependencies"
DECISION_TOOL = "decision_health"
DEPENDENTS = "dependents"
DEPENDENCIES = "dependencies"
DIRECTIONS = (DEPENDENTS, DEPENDENCIES)
# Pairs of teams told in the company view; the rest are counted.
MAX_PAIRS = 6
# Decisions that need attention told in full; the rest are counted.
MAX_FLAGGED = 4
OLD_DAYS = 180
_DAY_MS = 24 * 60 * 60 * 1000

# Words that ask about dependencies without naming a team: "cross-team dependencies",
# "which teams depend on each other". A question left with nothing else asks for the company view.
_COMPANY_WORDS = frozenset(
    {
        "cross", "team", "teams", "level", "inter", "dependency", "dependencies", "depend", "depends", "dependent",
        "dependents", "between", "each", "other", "map", "overview", "company", "wide", "whole", "all", "our",
        "rely", "relies", "need", "needs", "use", "uses", "show", "list", "give",
    }
)


def now_millis() -> int:
    """The clock ``decision_health`` judges age by; tests patch it."""
    return decisions.now_millis()


# --- team dependencies ---------------------------------------------------------------------


@dataclass(frozen=True)
class TeamLink:
    """One DEPENDS_ON edge across teams: ``from_team``'s ``dependent`` depends on ``to_team``'s ``dependency``."""

    from_team_id: str
    from_team: str
    dependent_id: str
    dependent: str
    dependency_id: str
    dependency: str
    to_team_id: str
    to_team: str

    @property
    def edge(self) -> str:
        return f"{self.dependent} → {self.dependency}"

    def steps(self) -> list[PathStep]:
        return [
            PathStep(self.from_team, "RUNS", self.dependent, from_id=self.from_team_id, to_id=self.dependent_id, from_label="Team", to_label="Service"),
            PathStep(self.dependent, "DEPENDS_ON", self.dependency, from_id=self.dependent_id, to_id=self.dependency_id, from_label="Service", to_label="Service"),
            PathStep(self.to_team, "RUNS", self.dependency, from_id=self.to_team_id, to_id=self.dependency_id, from_label="Team", to_label="Service"),
        ]


@dataclass
class TeamPair:
    """Every link by which ``from_team`` depends on ``to_team``."""

    from_team: str
    to_team: str
    links: list[TeamLink] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "from": self.from_team,
            "to": self.to_team,
            "count": len(self.links),
            "links": [{"from": link.dependent, "to": link.dependency} for link in self.links],
        }


def team_dependencies(name: str | None, scopes: list[str], direction: str | None = None, question: str | None = None) -> ToolResult:
    """Which teams depend on which, for one team or, with none named, the whole company."""
    direction = direction if direction in DIRECTIONS else DEPENDENTS
    question = question or name or ""
    _, graph = connect()
    links = _cross_team_links(graph, scopes)
    if not _names_something(name):
        return _company_view(links)
    resolution = resolve.resolve_name(name, ("Team",), scopes, graph=graph)
    if not resolution.found:
        miss = ToolResult(tool=TEAM_TOOL, answer=resolve.describe_miss(resolution, "team"), found=False, candidates=resolution.candidate_names())
        return fallback.before_refusing(question, scopes, miss)
    team = resolution.entity
    if direction == DEPENDENCIES:
        result = _dependencies_of(team, links)
    else:
        result = _dependents_of(graph, team, links, scopes)
    result.candidates = resolution.candidate_names()
    return result


def _names_something(name: str | None) -> bool:
    words = set(resolve.core(name or "").split())
    return bool(words - _COMPANY_WORDS)


def _cross_team_links(graph, scopes: list[str]) -> list[TeamLink]:
    rows = graph.ro_query(
        f"""
        MATCH (t1:Team)-[:RUNS]->(a:Service)-[:DEPENDS_ON]->(b:Service)<-[:RUNS]-(t2:Team)
        WHERE t1.id <> t2.id AND {SCOPED.format(var="t1")} AND {SCOPED.format(var="a")}
          AND {SCOPED.format(var="b")} AND {SCOPED.format(var="t2")}
          AND NOT {kinds.KIND_OF.format(var="a")} IN $excluded AND NOT {kinds.KIND_OF.format(var="b")} IN $excluded
        RETURN DISTINCT t1.id, t1.name, a.id, a.name, b.id, b.name, t2.id, t2.name
        ORDER BY t1.name, t2.name, a.name, b.name
        """,
        {"scopes": scopes, "excluded": sorted(kinds.NOT_DEPENDABLE)},
    ).result_set
    return [TeamLink(*row) for row in rows]


def _pairs(links: list[TeamLink]) -> list[TeamPair]:
    """Links grouped by (depending team, depended-on team), the busiest pair first."""
    pairs: dict[tuple[str, str], TeamPair] = {}
    for link in links:
        pairs.setdefault((link.from_team_id, link.to_team_id), TeamPair(link.from_team, link.to_team)).links.append(link)
    return sorted(pairs.values(), key=lambda pair: (-len(pair.links), pair.from_team, pair.to_team))


def _through(pair: TeamPair, first: bool) -> str:
    count = len(pair.links)
    noun = f" link{'s' if count != 1 else ''}" if first else ""
    return f"through {count}{noun} ({', '.join(link.edge for link in pair.links)})"


def _mutual(pairs: list[TeamPair]) -> list[tuple[str, str]]:
    """Pairs of teams that depend on each other, each once, by name."""
    keys = {(pair.from_team, pair.to_team) for pair in pairs}
    return sorted({tuple(sorted((a, b))) for a, b in keys if (b, a) in keys})


def _grid(pairs: list[TeamPair], mutual: list[tuple[str, str]]) -> dict:
    teams = sorted({team for pair in pairs for team in (pair.from_team, pair.to_team)})
    return {"kind": TEAM_TOOL, "teams": teams, "rows": [pair.to_dict() for pair in pairs], "mutual": [list(pair) for pair in mutual]}


def _dependents_of(graph, team: Entity, links: list[TeamLink], scopes: list[str]) -> ToolResult:
    """The teams whose things depend on what this team runs, directly and through others."""
    result = ToolResult(tool=TEAM_TOOL, answer="")
    into = _pairs([link for link in links if link.to_team_id == team.id])
    out_of = {pair.to_team: pair for pair in _pairs([link for link in links if link.from_team_id == team.id])}
    indirect = _reached_through_others(graph, team, {pair.links[0].from_team_id for pair in into}, scopes)
    if not into and not indirect:
        result.answer = f"No other team depends on what {team.name} runs."
        result.grid = _grid([], [])
        return result

    parts = []
    if into:
        told = [f"{pair.from_team} {_through(pair, index == 0)}" for index, pair in enumerate(into)]
        teams = "1 team depends" if len(into) == 1 else f"{len(into)} teams depend"
        parts.append(f"{teams} on {team.name}: {join_names(told)}.")
        for pair in into:
            for link in pair.links:
                result.extend(link.steps())
    for other, route in indirect:
        parts.append(f"{other} reaches {team.name} only through other things ({' → '.join(name for _id, name in route)}).")
        for (from_id, source), (to_id, target) in zip(route, route[1:]):
            result.add(PathStep(source, "DEPENDS_ON", target, from_id=from_id, to_id=to_id, from_label="Service", to_label="Service"))
    mutual = []
    for pair in into:
        back = out_of.get(pair.from_team)
        if back is None:
            continue
        mutual.append(tuple(sorted((team.name, pair.from_team))))
        parts.append(f"{team.name} also depends on {pair.from_team} ({', '.join(link.edge for link in back.links)}), so the two depend on each other.")
        for link in back.links:
            result.extend(link.steps())
    result.answer = " ".join(parts)
    result.grid = _grid(into + [out_of[other] for other in out_of if any(other == pair.from_team for pair in into)], mutual)
    return result


def _reached_through_others(graph, team: Entity, direct: set[str], scopes: list[str]) -> list[tuple[str, list[tuple[str, str]]]]:
    """Teams whose things reach this team's only through two or more DEPENDS_ON hops, each with
    its shortest route as (id, name) steps."""
    rows = graph.ro_query(
        f"""
        MATCH (:Team {{id: $id}})-[:RUNS]->(b:Service)
        WHERE {SCOPED.format(var="b")}
        MATCH route = (a:Service)-[:DEPENDS_ON*2..{MAX_DEPTH}]->(b)
        MATCH (other:Team)-[:RUNS]->(a)
        WHERE other.id <> $id AND NOT other.id IN $direct AND {SCOPED.format(var="a")} AND {SCOPED.format(var="other")}
        RETURN other.id, other.name, [n IN nodes(route) | [n.id, n.name, coalesce(n.scope, 'org_shared')]], length(route)
        ORDER BY length(route), other.name
        """,
        {"id": team.id, "direct": sorted(direct), "scopes": scopes},
    ).result_set
    found: dict[str, tuple[str, list[tuple[str, str]]]] = {}
    for other_id, other, nodes, _length in rows:
        # Every thing on the route must be one the reader can see.
        if other_id in found or any(scope not in scopes for _id, _name, scope in nodes):
            continue
        found[other_id] = (other, [(node_id, name) for node_id, name, _scope in nodes])
    return sorted(found.values())


def _dependencies_of(team: Entity, links: list[TeamLink]) -> ToolResult:
    """The teams this team's things depend on, and for what."""
    result = ToolResult(tool=TEAM_TOOL, answer="")
    out_of = _pairs([link for link in links if link.from_team_id == team.id])
    if not out_of:
        result.answer = f"{team.name} depends on no other team's things."
        result.grid = _grid([], [])
        return result
    told = []
    for pair in out_of:
        needed = list(dict.fromkeys(link.dependency for link in pair.links))
        told.append(f"on {pair.to_team} for {join_names(needed)} ({', '.join(link.edge for link in pair.links)})")
        for link in pair.links:
            result.extend(link.steps())
    result.answer = f"{team.name} depends {' and '.join(told)}."
    result.grid = _grid(out_of, [])
    return result


def _company_view(links: list[TeamLink]) -> ToolResult:
    """Every pair of teams tied by DEPENDS_ON, the busiest first, and the pairs that depend on each other."""
    result = ToolResult(tool=TEAM_TOOL, answer="")
    pairs = _pairs(links)
    if not pairs:
        result.answer = "No team's things depend on another team's in the graph yet."
        result.grid = _grid([], [])
        return result
    mutual = _mutual(pairs)
    shown, rest = pairs[:MAX_PAIRS], pairs[MAX_PAIRS:]
    edges = sum(len(pair.links) for pair in pairs)
    parts = [
        f"Across teams, {len(pairs)} pair{'s' if len(pairs) != 1 else ''} of teams {'are' if len(pairs) != 1 else 'is'} tied by "
        f"{edges} DEPENDS_ON edge{'s' if edges != 1 else ''}, the busiest first."
    ]
    parts.append("; ".join(f"{pair.from_team} depends on {pair.to_team} {_through(pair, index == 0)}" for index, pair in enumerate(shown)) + ".")
    if rest:
        parts.append(f"And {len(rest)} more pair{'s' if len(rest) != 1 else ''}.")
    for a, b in mutual:
        parts.append(f"{a} and {b} depend on each other.")
    for pair in shown:
        for link in pair.links:
            result.extend(link.steps())
    result.answer = " ".join(parts)
    result.grid = _grid(pairs, mutual)
    return result


# --- decision health -----------------------------------------------------------------------


@dataclass
class DecisionAudit:
    """One decision as the reader sees it: whether it stands, and what it is missing."""

    id: str
    name: str
    decided_at: int | None
    standing: bool
    about: list[tuple[str, str]]
    sources: list[tuple[str, str, str, str]]
    people: list[tuple[str, str, str]]
    replaced_by: str | None = None
    conflicts_with: list[str] = field(default_factory=list)
    uncited: list[tuple[str, str, str, str, str]] = field(default_factory=list)
    # How many uncited tickets and pull requests there are; ``uncited`` holds the first few.
    uncited_total: int = 0
    flags: dict[str, bool] = field(default_factory=dict)

    @property
    def needs_attention(self) -> bool:
        return self.standing and any(self.flags.values())

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "decided_at": decisions.format_date(self.decided_at) if self.decided_at is not None else None,
            "standing": self.standing,
            "replaced_by": self.replaced_by,
            "about": [name for _id, name in self.about],
            "flags": dict(self.flags),
            "conflicts_with": list(self.conflicts_with),
            "uncited": [name for _id, name, _sid, _service, _label in self.uncited],
        }


def decision_health(name: str | None, scopes: list[str], question: str | None = None) -> ToolResult:
    """Every standing decision checked against its own edges, narrowed to a service or a team when one is named."""
    question = question or name or ""
    _, graph = connect()
    target, things = None, None
    if _names_something_else(name):
        resolution = resolve.resolve_name(name, ("Service", "Team"), scopes, graph=graph)
        if not resolution.found:
            miss = ToolResult(tool=DECISION_TOOL, answer=resolve.describe_miss(resolution, "service or team"), found=False, candidates=resolution.candidate_names())
            return fallback.before_refusing(question, scopes, miss)
        target = resolution.entity
        things = _things_of(graph, target, scopes)

    audits = _audits(graph, scopes)
    if things is not None:
        audits = [audit for audit in audits if any(thing_id in things for thing_id, _name in audit.about)]
    standing = [audit for audit in audits if audit.standing]
    result = ToolResult(tool=DECISION_TOOL, answer="")
    about = ""
    if target is not None:
        about = f" about {kinds.subject(target.name, target.kind)}" if target.label == "Service" else f" about what {target.name} runs"
    if not audits:
        result.answer = f"No decision is{about} yet." if about else "No decisions are in the graph yet."
        result.grid = {"kind": DECISION_TOOL, "rows": []}
        return result

    _check(graph, standing, scopes)
    replaced = [audit for audit in audits if not audit.standing]
    flagged = [audit for audit in standing if audit.needs_attention]
    counts = f"{len(standing)} standing decision{'s' if len(standing) != 1 else ''}{about}, {len(replaced)} replaced"
    if flagged:
        parts = [f"{counts}. Needs attention: {_attention(flagged)}."]
    else:
        parts = [f"{counts}, and none needs attention: each cites its evidence, has an approver, and nothing it does not cite has landed on what it is about."]
    history, superseded = _history(graph, audits, scopes)
    if history:
        parts.append(f"History: {'; '.join(history)}.")
    result.answer = " ".join(parts)
    for audit in standing:
        result.extend(_audit_steps(audit))
    result.extend(superseded)
    result.grid = {"kind": DECISION_TOOL, "rows": [audit.to_dict() for audit in audits]}
    return result


def _names_something_else(name: str | None) -> bool:
    """Whether the question names a thing to narrow to, beyond asking about decisions at all."""
    words = set(resolve.core(name or "").split())
    return bool(words - _DECISION_WORDS)


# Words that ask about decision health without naming what to narrow to.
_DECISION_WORDS = frozenset(
    {
        "decision", "decisions", "stale", "conflicting", "contradictory", "contradicting", "outdated", "orphaned",
        "unsupported", "conflict", "contradict", "clash", "lack", "evidence", "need", "needs", "attention", "review",
        "health", "healthy", "any", "our", "audit", "standing", "still", "current", "have", "made", "there",
    }
)


def _things_of(graph, target: Entity, scopes: list[str]) -> set[str]:
    """The ids a narrowed audit keeps: the service itself, or everything the team runs."""
    if target.label == "Service":
        return {target.id}
    rows = graph.ro_query(
        f"""
        MATCH (:Team {{id: $id}})-[:RUNS]->(s:Service)
        WHERE {SCOPED.format(var="s")}
        RETURN s.id
        """,
        {"id": target.id, "scopes": scopes},
    ).result_set
    return {row[0] for row in rows}


def _audits(graph, scopes: list[str]) -> list[DecisionAudit]:
    """Every visible decision, with what it is about, its sources and its people. It stands when
    no decision the reader can see supersedes it."""
    rows = graph.ro_query(
        f"""
        MATCH (d:Decision)
        WHERE {SCOPED.format(var="d")}
        OPTIONAL MATCH (newer:Decision)-[:SUPERSEDES]->(d)
        WHERE {SCOPED.format(var="newer")}
        WITH d, count(newer) AS replaced
        OPTIONAL MATCH (d)-[:ABOUT]->(s:Service)
        WHERE {SCOPED.format(var="s")}
        WITH d, replaced, collect(DISTINCT [s.id, s.name]) AS about
        OPTIONAL MATCH (d)-[cited:BASED_ON|CITES]->(source)
        WHERE {SCOPED.format(var="source")}
        WITH d, replaced, about, collect(DISTINCT [source.id, source.name, labels(source)[0], type(cited)]) AS sources
        OPTIONAL MATCH (person:Person)-[role:PROPOSED|APPROVED]->(d)
        RETURN d.id, d.name, d.decided_at, replaced, about, sources, collect(DISTINCT [person.id, person.name, type(role)])
        ORDER BY coalesce(d.decided_at, 0) DESC, d.name
        """,
        {"scopes": scopes},
    ).result_set
    return [
        DecisionAudit(
            id=decision_id,
            name=name,
            decided_at=decided_at,
            standing=replaced == 0,
            about=sorted((row[0], row[1]) for row in about if row[0] is not None),
            sources=[tuple(row) for row in sources if row[0] is not None],
            people=sorted(tuple(row) for row in people if row[0] is not None),
        )
        for decision_id, name, decided_at, replaced, about, sources, people in rows
    ]


def _check(graph, standing: list[DecisionAudit], scopes: list[str]) -> None:
    """Raise each standing decision's flags in place."""
    cutoff = now_millis() - OLD_DAYS * _DAY_MS
    by_thing: dict[str, list[DecisionAudit]] = {}
    for audit in standing:
        for thing_id, _name in audit.about:
            by_thing.setdefault(thing_id, []).append(audit)
    for audit in standing:
        audit.conflicts_with = sorted({other.id for thing_id, _name in audit.about for other in by_thing[thing_id] if other.id != audit.id})
        if audit.about:
            tickets, pull_requests, audit.uncited_total = decisions.uncited_work(graph, audit.id, [thing_id for thing_id, _name in audit.about], scopes)
            audit.uncited = [(*row, "Ticket") for row in tickets] + [(*row, "PullRequest") for row in pull_requests]
        audit.flags = {
            "no_evidence": not audit.sources,
            "no_approver": not any(role == "APPROVED" for _id, _name, role in audit.people),
            "conflict": bool(audit.conflicts_with),
            "may_be_stale": bool(audit.uncited),
            "old": audit.decided_at is not None and audit.decided_at < cutoff,
        }


def _attention(flagged: list[DecisionAudit]) -> str:
    """"*X*: cites no evidence and has no approver; *Y* and *Z* are both standing decisions about Billing Service"."""
    by_id = {audit.id: audit for audit in flagged}
    items: list[str] = []
    told_pairs: set[tuple[str, str]] = set()
    for audit in flagged:
        clauses = []
        if audit.flags["no_evidence"]:
            clauses.append("cites no evidence")
        if audit.flags["no_approver"]:
            clauses.append("has no approver")
        if audit.flags["may_be_stale"]:
            names = [f"{name}" for _id, name, _sid, _service, _label in audit.uncited]
            things = join_names(list(dict.fromkeys(service for _id, _name, _sid, service, _label in audit.uncited)))
            many = audit.uncited_total > 1
            if audit.uncited_total > len(names):
                names.append(f"{audit.uncited_total - len(names)} more")
            clauses.append(f"{join_names(names)} landed on {things} and {'they are' if many else 'it is'} not cited")
        if audit.flags["old"]:
            clauses.append(f"was decided {decisions.format_date(audit.decided_at)}, over {OLD_DAYS} days ago")
        if clauses:
            items.append(f"*{audit.name}*: {join_names(clauses)}")
        for other_id in audit.conflicts_with:
            pair = tuple(sorted((audit.id, other_id)))
            if pair in told_pairs:
                continue
            told_pairs.add(pair)
            other = by_id.get(other_id)
            shared = [name for thing_id, name in audit.about if other is not None and any(thing_id == theirs for theirs, _n in other.about)]
            other_name = other.name if other is not None else other_id
            items.append(f"*{audit.name}* and *{other_name}* are both standing decisions about {join_names(shared)}")
    shown, rest = items[:MAX_FLAGGED], items[MAX_FLAGGED:]
    told = "; ".join(shown)
    return f"{told}; and {len(rest)} more" if rest else told


def _history(graph, audits: list[DecisionAudit], scopes: list[str]) -> tuple[list[str], list[PathStep]]:
    """Each chain of replaced decisions, oldest first: "*A* (1 Jun 2026) was replaced by *B* (15 Sep 2026)",
    and the SUPERSEDES edges that tell it."""
    rows = graph.ro_query(
        f"""
        MATCH (newer:Decision)-[:SUPERSEDES]->(older:Decision)
        WHERE {SCOPED.format(var="newer")} AND {SCOPED.format(var="older")}
        RETURN older.id, newer.id
        ORDER BY coalesce(newer.decided_at, 0) DESC
        """,
        {"scopes": scopes},
    ).result_set
    known = {audit.id: audit for audit in audits}
    replaced_by: dict[str, str] = {}
    for older_id, newer_id in rows:
        if older_id in known and newer_id in known:
            replaced_by.setdefault(older_id, newer_id)
    steps = []
    for older_id, newer_id in replaced_by.items():
        known[older_id].replaced_by = known[newer_id].name
        steps.append(PathStep(known[newer_id].name, "SUPERSEDES", known[older_id].name, from_id=newer_id, to_id=older_id, from_label="Decision", to_label="Decision"))
    starts = [decision_id for decision_id in replaced_by if decision_id not in replaced_by.values()]
    chains = []
    for start in sorted(starts, key=lambda decision_id: known[decision_id].decided_at or 0):
        chain, seen = [known[start]], {start}
        while chain[-1].id in replaced_by and replaced_by[chain[-1].id] not in seen:
            chain.append(known[replaced_by[chain[-1].id]])
            seen.add(chain[-1].id)
        told = f"{_dated(chain[0])} was replaced by {_dated(chain[1])}"
        told += "".join(f", then by {_dated(audit)}" for audit in chain[2:])
        chains.append(told)
    return chains, steps


def _dated(audit: DecisionAudit) -> str:
    return f"*{audit.name}*" + (f" ({decisions.format_date(audit.decided_at)})" if audit.decided_at is not None else "")


def _audit_steps(audit: DecisionAudit) -> list[PathStep]:
    """What the audit read for one standing decision: what it is about, its sources, its people,
    what it replaced, and the uncited work on what it is about."""
    steps = [PathStep(audit.name, "ABOUT", name, from_id=audit.id, to_id=thing_id, from_label="Decision", to_label="Service") for thing_id, name in audit.about]
    steps += [PathStep(audit.name, rel, source, from_id=audit.id, to_id=source_id, from_label="Decision", to_label=label) for source_id, source, label, rel in audit.sources]
    steps += [PathStep(person, role, audit.name, from_id=person_id, to_id=audit.id, from_label="Person", to_label="Decision") for person_id, person, role in audit.people]
    for item_id, item, service_id, service, label in audit.uncited:
        rel = "ABOUT" if label == "Ticket" else "TOUCHES"
        steps.append(PathStep(item, rel, service, from_id=item_id, to_id=service_id, from_label=label, to_label="Service"))
    return steps
