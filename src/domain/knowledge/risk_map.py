"""Where risk concentrates across the company: the risk map, for any team.

``risk_map`` takes every owned thing (a Service node of any kind) the asker can see and
checks it for three gaps:

- one person: at most one person knows it. "Knows" is ``who_knows`` itself
  (``ask_service.knowers`` and ``rank_people``), so the people listed are its "Ask first" list;
- no document: no document describes it. Accounts are not documented, so for them it is None;
- owner on another team: it has owners and a running team, and no owner is on that team.

Each thing is scored ``(1 + dependents) x gaps``, where dependents follow ``DEPENDS_ON`` the
way ``if_person_leaves`` and ``blast_radius`` count them, so a gap under something much of
the company leans on comes first. PageRank, the number ``load_bearing`` ranks by, is shown
beside each row but does not change the order. The reply tells the first three in full and
names the rest; the grid carries every row for a page to draw as a table.
"""

from dataclasses import dataclass, field

from src.db.falkordb_client import connect
from src.domain.knowledge import algorithms, ask_service, bus_factor, kinds
from src.domain.knowledge.dto import PathStep, ToolResult
from src.domain.knowledge.resolve import SCOPED, Entity, join_names

TOOL = "risk_map"
# Rows told in full; the rest are named in one closing sentence.
MAX_LISTED = 3
MAX_ROWS = 50

COLUMNS = [
    {"key": "dependents", "label": "Depend on it", "type": "count", "hot": "high"},
    {"key": "pagerank", "label": "PageRank", "type": "score"},
    {"key": "people", "label": "People who know it", "type": "count", "hot": "low"},
    {"key": "one_person", "label": "One person", "type": "flag"},
    {"key": "no_document", "label": "No document", "type": "flag"},
    {"key": "owner_elsewhere", "label": "Owner on another team", "type": "flag"},
]
SCORING = "(1 + dependents) x flags raised; ties by dependents, then name"


@dataclass(frozen=True)
class Owner:
    id: str
    name: str
    # (team id, team name) for each team the owner is a member of.
    teams: tuple[tuple[str, str], ...] = ()


@dataclass
class RiskRow:
    """One owned thing on the map: what depends on it, who knows it, and which gaps it has."""

    id: str
    name: str
    kind: str
    team_id: str | None
    team: str | None
    owners: list[Owner]
    dependents: int
    pagerank: float | None
    people: list[str]
    flags: dict[str, bool | None] = field(default_factory=dict)

    @property
    def score(self) -> int:
        return (1 + self.dependents) * sum(1 for flag in self.flags.values() if flag)

    @property
    def owner_teams(self) -> list[str]:
        return sorted({name for owner in self.owners for _id, name in owner.teams})

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "team": self.team,
            "owners": [owner.name for owner in self.owners],
            "owner_teams": self.owner_teams,
            "dependents": self.dependents,
            "pagerank": self.pagerank,
            "people": list(self.people),
            "flags": dict(self.flags),
            "score": self.score,
        }


def risk_map(scopes: list[str]) -> ToolResult:
    """Every visible owned thing with its gaps, the riskiest first."""
    _, graph = connect()
    ranked = rows(graph, scopes)
    result = ToolResult(tool=TOOL, answer="")
    if not ranked:
        result.answer = "The graph has no owned things yet, so there is nothing to rank."
        return result
    result.grid = grid(ranked)

    flagged = [row for row in ranked if row.score]
    if not flagged:
        each = "the one thing in the graph" if len(ranked) == 1 else f"each of the {len(ranked)} things"
        result.answer = (
            f"No gaps: {each} has more than one person who knows it, "
            "a document where one applies, and an owner on the team that runs it."
        )
        return result
    listed, rest = flagged[:MAX_LISTED], flagged[MAX_LISTED:]
    parts = [f"Risk sits with {len(flagged)} of {len(ranked)} things, ranked by how much depends on each and how many gaps it has."]
    for row in listed:
        parts.append(_sentence(row))
        result.extend(_steps(graph, row, scopes))
    for row in rest:
        result.extend(_owns(row))
    if rest:
        parts.append(f"And {len(rest)} more: {join_names([kinds.listed(row.name, row.kind) for row in rest])}.")
    result.answer = " ".join(parts)
    return result


def rows(graph, scopes: list[str], ids: list[str] | None = None) -> list[RiskRow]:
    """Every visible owned thing as a row, the riskiest first; only the things in ``ids`` when given.

    A team page passes the ids of what the team runs, so its grid and the tool's agree row for row.
    """
    only = "AND s.id IN $ids" if ids is not None else ""
    things = graph.ro_query(
        f"""
        MATCH (s:Service)
        WHERE {SCOPED.format(var="s")} {only}
        OPTIONAL MATCH (team:Team)-[:RUNS]->(s)
        WITH s, team ORDER BY team.name
        WITH s, collect(DISTINCT [team.id, team.name])[0] AS team
        OPTIONAL MATCH (owner:Person)-[:OWNS]->(s)
        OPTIONAL MATCH (owner)-[:MEMBER_OF]->(home:Team)
        WITH s, team, owner, collect(DISTINCT [home.id, home.name]) AS homes
        WITH s, team, collect(CASE WHEN owner IS NULL THEN NULL ELSE [owner.id, owner.name, homes] END) AS owners
        OPTIONAL MATCH (doc:Document)-[:DESCRIBES]->(s)
        WHERE {SCOPED.format(var="doc")}
        RETURN s.id, s.name, {kinds.KIND_OF.format(var="s")}, team, owners, count(DISTINCT doc)
        ORDER BY s.name
        """,
        {"scopes": scopes, "ids": list(ids or [])},
    ).result_set
    if not things:
        return []
    pagerank = algorithms.pagerank_by_id(graph)
    reach = _reach_all(graph, scopes, [thing[0] for thing in things])
    known = ask_service.knowers_many(graph, [Entity(thing[0], thing[1], "Service", kinds.kind_or_default(thing[2])) for thing in things], scopes)
    ranked = [_row(thing, pagerank, reach, known[thing[0]].result.path) for thing in things]
    ranked.sort(key=lambda row: (-row.score, -row.dependents, row.name))
    return ranked


def grid(ranked: list[RiskRow]) -> dict:
    """The rows as the grid the page draws: the tool's answer and a team page use the same one."""
    return {"kind": TOOL, "columns": COLUMNS, "rows": [row.to_dict() for row in ranked[:MAX_ROWS]], "scoring": SCORING}


def _reach_all(graph, scopes: list[str], ids: list[str]) -> dict[str, int]:
    """``bus_factor._dependents`` for every thing in ``ids`` in one query: how many visible things
    depend on each, directly or through others, over routes the asker can read."""
    rows = graph.ro_query(
        f"""
        MATCH path = (dependent:Service)-[:DEPENDS_ON*1..{bus_factor.MAX_DEPTH}]->(target:Service)
        WHERE target.id IN $ids AND all(n IN nodes(path) WHERE {SCOPED.format(var="n")})
          AND NOT {kinds.KIND_OF.format(var="dependent")} IN $excluded
        RETURN target.id, count(DISTINCT dependent)
        """,
        {"ids": ids, "scopes": scopes, "excluded": sorted(kinds.NOT_DEPENDABLE)},
    ).result_set
    return dict(rows)


def _row(thing: list, pagerank: dict[str, float], reach: dict[str, int], path: list[PathStep]) -> RiskRow:
    service_id, name, kind, team, owner_rows, documents = thing
    kind = kinds.kind_or_default(kind)
    team_id, team_name = team if team and team[0] is not None else (None, None)
    owners = sorted(
        (Owner(owner_id, owner_name, tuple((home_id, home) for home_id, home in homes if home_id is not None)) for owner_id, owner_name, homes in owner_rows if owner_id),
        key=lambda owner: owner.name,
    )
    entity = Entity(service_id, name, "Service", kind)
    people = [person for person, _score, _reasons in ask_service.rank_people(path, entity)]
    account = kind in kinds.NOT_DEPENDABLE
    flags = {
        "one_person": len(people) <= 1,
        "no_document": None if account else documents == 0,
        "owner_elsewhere": bool(owners) and team_id is not None and not any(home_id == team_id for owner in owners for home_id, _home in owner.teams),
    }
    return RiskRow(
        id=service_id,
        name=name,
        kind=kind,
        team_id=team_id,
        team=team_name,
        owners=owners,
        dependents=reach.get(service_id, 0),
        # Accounts never appear in load_bearing, so they get no PageRank either.
        pagerank=None if account or service_id not in pagerank else round(pagerank[service_id], 4),
        people=people,
        flags=flags,
    )


def _sentence(row: RiskRow) -> str:
    named = kinds.capitalized(kinds.subject(row.name, row.kind))
    if row.pagerank is not None:
        named += f" (PageRank {row.pagerank:.2f})"
    clauses = []
    if row.dependents:
        clauses.append(f"{row.dependents} thing{'s' if row.dependents != 1 else ''} depend{'s' if row.dependents == 1 else ''} on it")
    if row.flags["one_person"]:
        clauses.append(f"only {row.people[0]} knows it" if row.people else "no one is linked to it")
    if row.flags["no_document"]:
        clauses.append("no document describes it")
    if row.flags["owner_elsewhere"]:
        names = [owner.name for owner in row.owners]
        whose = f"its owner {names[0]} is" if len(names) == 1 else f"its owners {join_names(names)} are"
        on = f"on {join_names(row.owner_teams)}" if row.owner_teams else "on no team"
        clauses.append(f"{whose} {on} while {row.team} runs it")
    return f"{named}: {_series(clauses)}."


def _series(clauses: list[str]) -> str:
    """A, B, and C: one row's clauses, with a serial comma once there are three."""
    if len(clauses) <= 2:
        return " and ".join(clauses)
    return ", ".join(clauses[:-1]) + ", and " + clauses[-1]


def _owns(row: RiskRow) -> list[PathStep]:
    return [PathStep(owner.name, "OWNS", row.name, from_id=owner.id, to_id=row.id, from_label="Person", to_label="Service") for owner in row.owners]


def _steps(graph, row: RiskRow, scopes: list[str]) -> list[PathStep]:
    """The owner's OWNS edge, the team's RUNS edge, what depends on it directly, and, when the
    owner is on another team, the owner's MEMBER_OF edges."""
    steps = _owns(row)
    if row.team_id is not None:
        steps.append(PathStep(row.team, "RUNS", row.name, from_id=row.team_id, to_id=row.id, from_label="Team", to_label="Service"))
    direct = graph.ro_query(
        f"""
        MATCH (dependent:Service)-[:DEPENDS_ON]->(:Service {{id: $id}})
        WHERE {SCOPED.format(var="dependent")}
        RETURN dependent.id, dependent.name
        ORDER BY dependent.name
        """,
        {"id": row.id, "scopes": scopes},
    ).result_set
    for dependent_id, dependent in direct:
        steps.append(PathStep(dependent, "DEPENDS_ON", row.name, from_id=dependent_id, to_id=row.id, from_label="Service", to_label="Service"))
    if row.flags["owner_elsewhere"]:
        for owner in row.owners:
            for team_id, team in owner.teams:
                steps.append(PathStep(owner.name, "MEMBER_OF", team, from_id=owner.id, to_id=team_id, from_label="Person", to_label="Team"))
    return steps
