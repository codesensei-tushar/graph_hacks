"""Who a person is and what they hold, every fact an edge: the person profile, for any team.

``person_profile`` resolves a person, then reads, each part scoped to the asker:

- their role and the teams they are a ``MEMBER_OF``;
- what they hold: things of any kind they ``OWNS``, ``WORKED_ON`` or ``KNOWS``, each with how
  much depends on it, counted as ``if_person_leaves`` and ``blast_radius`` count it;
- the documents they ``AUTHORED``;
- their pull requests and reviews, through the GitHub account that ``IS`` them, open first;
- the open tickets they worked on or are assigned;
- the decisions they ``PROPOSED`` or ``APPROVED``, each followed through ``SUPERSEDES`` to say
  whether it still stands;
- who they work with most: shared things, review pairs and decision pairs, one point each;
- how many things rest on them alone (``bus_factor.at_risk``, what ``if_person_leaves`` tells);
- what they said, from the memories their Slack account ``SAID``, newest first.

Someone only a connected tool knows (an unlinked Jira or HubSpot account, ``people.py``) gets
``account_profile``: what the account owns or works on, its open tickets, what rests on it
alone, and how an admin links it to a person.

It is also the fallback where ``who_knows``, ``who_owns``, ``onboarding_brief`` and
``decision_trace`` would refuse: "who is arjun" or "what does sara own" names a person, not a
service or a team, so ``profile_if_person`` answers it instead of the refusal.
"""

from dataclasses import dataclass, field

from src.db.falkordb_client import connect
from src.domain.knowledge import ask_service, bus_factor, decisions, kinds, people, resolve
from src.domain.knowledge.dto import MemoryLine, PathStep, ToolResult, day_from
from src.domain.knowledge.memories import LIVE as _LIVE
from src.domain.knowledge.resolve import SCOPED, Entity, join_names

TOOL = "person_profile"
MAX_HOLDS = 4
MAX_DOCUMENTS = 3
MAX_PULL_REQUESTS = 3
MAX_TICKETS = 3
MAX_DECISIONS = 2
MAX_COLLABORATORS = 2
MAX_MEMORIES = 2

# The person's own edge to a thing, strongest first; a thing held several ways is told once.
_HOLDS = ("OWNS", "WORKED_ON", "KNOWS")
_HOLD_VERBS = {"OWNS": "owns", "WORKED_ON": "works on", "KNOWS": "knows"}


@dataclass
class _Held:
    id: str
    name: str
    kind: str
    rel: str
    dependents: int
    team: str | None = None
    owners: list[str] = field(default_factory=list)


# The grid under the answer: one row per thing they hold, in the risk_map shape, so the page
# draws it with the same table. "alone" is if_person_leaves' own test.
GRID_COLUMNS = [
    {"key": "held", "label": "Their edge", "type": "text"},
    {"key": "dependents", "label": "Depend on it", "type": "count", "hot": "high"},
    {"key": "people", "label": "Others who know it", "type": "count", "hot": "low"},
    {"key": "alone", "label": "Rests on them alone", "type": "flag"},
]
GRID_SCORING = "their own edge (owns, works on, knows), then most dependents"
MAX_GRID_ROWS = 50


@dataclass
class _Collaborator:
    id: str
    name: str
    score: int = 0
    # What the two share, in the order told, each with the collaborator's own edge to it.
    shared: list[tuple[str, PathStep]] = field(default_factory=list)

    def share(self, what: str, step: PathStep) -> None:
        if all(existing != what for existing, _step in self.shared):
            self.shared.append((what, step))
            self.score += 1


def person_profile(question: str, scopes: list[str]) -> ToolResult:
    """Who the person the question names is, and what they hold."""
    _, graph = connect()
    resolution = people.resolve_person(question, scopes, graph)
    if not resolution.found:
        return ToolResult(tool=TOOL, answer=resolve.describe_miss(resolution, "person"), found=False, candidates=resolution.candidate_names())
    result = profile_of(graph, resolution.entity, scopes)
    result.candidates = resolution.candidate_names()
    return result


def profile_if_person(question: str, scopes: list[str], miss: ToolResult) -> ToolResult:
    """The profile when the question names a person, else ``miss`` unchanged, candidates included.

    For the tools that resolve services, documents, tickets, teams or decisions: where they
    would refuse "who is arjun", the person is what was asked about."""
    profile = profile_or_none(question, scopes)
    return miss if profile is None else profile


def profile_or_none(question: str, scopes: list[str]) -> ToolResult | None:
    """The profile of the person the question names, or None when it names no one: the
    first step of ``fallback.before_refusing``."""
    _, graph = connect()
    resolution = people.resolve_person(question, scopes, graph)
    if not resolution.found:
        return None
    return profile_of(graph, resolution.entity, scopes)


def profile_of(graph, person: Entity, scopes: list[str]) -> ToolResult:
    """The profile of a person already resolved, or of an unlinked connector account."""
    if people.is_account(person):
        return account_profile(graph, person, scopes)
    result = ToolResult(tool=TOOL, answer="")
    params = {"id": person.id, "scopes": scopes}
    parts = [_lead(graph, person, params, result)]

    held = _held(graph, person, params, result)
    sections = [
        _holds_sentence(held),
        _documents(graph, person, params, result),
        _pull_requests(graph, person, params, result),
        _tickets(graph, person, params, result),
    ]
    rows = _decision_rows(graph, params)
    sections.append(_decisions_sentence(graph, person, rows, scopes, result))
    sections.append(_collaborators(graph, person, params, held, rows, result))
    told = [section for section in sections if section]
    parts.extend(told)

    first = person.name.split()[0]
    at_risk = bus_factor.at_risk(graph, person, scopes)
    if held:
        # A thing rests on them alone exactly when if_person_leaves lists it.
        alone_ids = {item.steps[0].to_id for item in at_risk if item.order == 0}
        result.grid = _grid(graph, person, held, alone_ids, scopes)
    if told:
        alone = len(at_risk)
        if alone:
            things = "1 thing rests" if alone == 1 else f"{alone} things rest"
            parts.append(f"{things} on {first} alone; ask “what if {first} leaves”.")
        else:
            parts.append(f"Nothing rests on {first} alone.")
    else:
        parts.append("Nothing else is linked to them yet.")

    result.memories = _said(graph, person, params)
    result.answer = " ".join(parts)
    return result


# --- an account a connected tool knows -------------------------------------------------------

MAX_ACCOUNT_THINGS = 6
MAX_ACCOUNT_CITES = 25


def account_profile(graph, account: Entity, scopes: list[str]) -> ToolResult:
    """What an unlinked Jira or HubSpot account holds: what it owns or works on, its open tickets,
    what rests on it alone, and how an admin links it to a person. Every fact an edge."""
    result = ToolResult(tool=TOOL, answer="")
    params = {"id": account.id, "scopes": scopes}
    rows = graph.ro_query(
        f"""
        MATCH (:ExternalUser {{id: $id}})-[rel:OWNS|WORKED_ON]->(s:Service)
        WHERE {SCOPED.format(var="s")}
        RETURN s.id, s.name, {kinds.KIND_OF.format(var="s")}, collect(DISTINCT type(rel))
        ORDER BY s.name
        """,
        params,
    ).result_set
    held: list[_Held] = []
    reach: dict[str, int] = {}
    for service_id, name, kind, rels in rows:
        rel = "OWNS" if "OWNS" in rels else "WORKED_ON"
        held.append(_Held(service_id, name, kinds.kind_or_default(kind), rel, bus_factor._dependents(graph, service_id, params, reach)))
    parts = [f"{account.name} is an account in {account.kind or 'a connected tool'}."]
    cited = 0
    for verb, rel in (("Owns", "OWNS"), ("Works on", "WORKED_ON")):
        things = [thing for thing in held if thing.rel == rel]
        if not things:
            continue
        for thing in things:
            if cited < MAX_ACCOUNT_CITES:
                result.add(PathStep(account.name, rel, thing.name, from_id=account.id, to_id=thing.id, from_label=account.label, to_label="Service"))
                cited += 1
        named = [kinds.listed(thing.name, thing.kind) if len({t.kind for t in things}) > 1 else thing.name for thing in things[:MAX_ACCOUNT_THINGS]]
        if len(things) > MAX_ACCOUNT_THINGS:
            named.append(f"{len(things) - MAX_ACCOUNT_THINGS} more")
        noun = _things_noun(things)
        parts.append(f"{verb} {len(things)} {noun}: {join_names(named)}.")
    tickets = graph.ro_query(
        f"""
        MATCH (:ExternalUser {{id: $id}})-[rel:ASSIGNED|OPENED|WORKED_ON]->(t:Ticket)
        WHERE {SCOPED.format(var="t")} AND coalesce(t.state, 'open') <> 'closed'
        RETURN t.id, t.name, coalesce(t.title, t.name), collect(DISTINCT type(rel))
        ORDER BY t.name
        """,
        params,
    ).result_set
    for verb, rel in (("Assigned", "ASSIGNED"), ("Opened", "OPENED")):
        mine = [row for row in tickets if rel in row[3] and (rel == "ASSIGNED" or "ASSIGNED" not in row[3])]
        if not mine:
            continue
        named = []
        for ticket_id, name, title, _rels in mine[:MAX_TICKETS]:
            result.add(PathStep(account.name, rel, name, from_id=account.id, to_id=ticket_id, from_label=account.label, to_label="Ticket"))
            named.append(f"{name} (“{title}”)" if title and title != name else name)
        if len(mine) > MAX_TICKETS:
            named.append(f"{len(mine) - MAX_TICKETS} more")
        parts.append(f"{verb} {len(mine)} open ticket{'s' if len(mine) != 1 else ''}: {join_names(named)}.")
    if held or tickets:
        at_risk = bus_factor.at_risk(graph, account, scopes)
        if held:
            alone_ids = {item.steps[0].to_id for item in at_risk if item.order == 0}
            result.grid = _grid(graph, account, held, alone_ids, scopes)
        alone = len(at_risk)
        parts.append(f"{'1 thing rests' if alone == 1 else f'{alone} things rest'} on this account alone." if alone else "Nothing rests on this account alone.")
    else:
        parts.append("Nothing else is linked to it yet.")
    parts.append(people.unlinked_hint(account))
    result.answer = " ".join(parts)
    return result


def _things_noun(things: list[_Held]) -> str:
    """"accounts" when every thing is an account, "things" when the kinds differ."""
    found = {thing.kind for thing in things}
    if len(found) != 1:
        return "thing" if len(things) == 1 else "things"
    kind = found.pop()
    if len(things) == 1:
        return kind
    return {"process": "processes", "policy": "policies"}.get(kind, kind + "s")


# --- who they are --------------------------------------------------------------------------


def _lead(graph, person: Entity, params: dict, result: ToolResult) -> str:
    """"Arjun Mehta is the Billing engineer on Payments.", with the MEMBER_OF edges."""
    rows = graph.ro_query(
        f"""
        MATCH (p:Person {{id: $id}})
        OPTIONAL MATCH (p)-[:MEMBER_OF]->(team:Team)
        WHERE {SCOPED.format(var="team")}
        RETURN p.role, team.id, team.name
        ORDER BY team.name
        """,
        params,
    ).result_set
    role = (rows[0][0] or "").strip() if rows else ""
    teams = []
    for _role, team_id, team in rows:
        if team_id is not None:
            teams.append(team)
            result.add(PathStep(person.name, "MEMBER_OF", team, from_id=person.id, to_id=team_id, from_label="Person", to_label="Team"))
    lead = f"{person.name} is the {role}" if role else f"{person.name} is"
    if teams:
        return f"{lead} on {join_names(teams)}."
    return f"{lead}." if role else f"{person.name} is in the graph."


# --- what they hold ------------------------------------------------------------------------


def _held(graph, person: Entity, params: dict, result: ToolResult) -> list[_Held]:
    rows = graph.ro_query(
        f"""
        MATCH (:Person {{id: $id}})-[rel:OWNS|WORKED_ON|KNOWS]->(s:Service)
        WHERE {SCOPED.format(var="s")}
        OPTIONAL MATCH (team:Team)-[:RUNS]->(s)
        OPTIONAL MATCH (owner:Person)-[:OWNS]->(s)
        RETURN s.id, s.name, {kinds.KIND_OF.format(var="s")}, collect(DISTINCT type(rel)), collect(DISTINCT team.name), collect(DISTINCT owner.name)
        """,
        params,
    ).result_set
    reach: dict[str, int] = {}
    held = [
        _Held(
            service_id, name, kinds.kind_or_default(kind), min(rels, key=_HOLDS.index), bus_factor._dependents(graph, service_id, params, reach),
            team=min(teams) if teams else None, owners=sorted(owners),
        )
        for service_id, name, kind, rels, teams, owners in rows
    ]
    held.sort(key=lambda thing: (_HOLDS.index(thing.rel), -thing.dependents, thing.name))
    for thing in held[:MAX_HOLDS]:
        result.add(PathStep(person.name, thing.rel, thing.name, from_id=person.id, to_id=thing.id, from_label="Person", to_label="Service"))
    return held


def _grid(graph, person: Entity, held: list[_Held], alone_ids: set[str], scopes: list[str]) -> dict:
    """Every thing they hold, with who else knows it (who_knows' own ranking) and whether it rests on them alone."""
    rows = []
    for thing in held[:MAX_GRID_ROWS]:
        entity = Entity(thing.id, thing.name, "Service", thing.kind)
        known = ask_service.knowers(graph, entity, scopes)
        # Told apart by id: an unlinked account with their name is someone else.
        others = [name for who, name, _score, _reasons in ask_service.rank_people_keyed(known.path, entity) if who != (person.label, person.id)]
        alone = thing.id in alone_ids
        rows.append(
            {
                "id": thing.id,
                "name": thing.name,
                "kind": thing.kind,
                "team": thing.team,
                "owners": list(thing.owners),
                "owner_teams": [],
                "held": _HOLD_VERBS[thing.rel],
                "dependents": thing.dependents,
                "people": others,
                "flags": {"alone": alone},
                "score": (1 + thing.dependents) if alone else 0,
            }
        )
    return {"kind": TOOL, "title": f"What {person.name} holds", "columns": GRID_COLUMNS, "rows": rows, "scoring": GRID_SCORING}


def _holds_sentence(held: list[_Held]) -> str:
    """"Owns Billing Service (2 things depend on it) and knows Auth Service (5 depend on it)."."""
    if not held:
        return ""
    groups: list[tuple[str, list[str]]] = []
    first_count = True
    for thing in held[:MAX_HOLDS]:
        named = kinds.listed(thing.name, thing.kind)
        if thing.dependents:
            count = thing.dependents
            if first_count:
                named += f" ({count} thing{'s' if count != 1 else ''} depend{'s' if count == 1 else ''} on it)"
                first_count = False
            else:
                named += f" ({count} depend{'s' if count == 1 else ''} on it)"
        verb = _HOLD_VERBS[thing.rel]
        if groups and groups[-1][0] == verb:
            groups[-1][1].append(named)
        else:
            groups.append((verb, [named]))
    clauses = [f"{verb} {join_names(items)}" for verb, items in groups]
    if len(held) > MAX_HOLDS:
        clauses.append(f"{len(held) - MAX_HOLDS} more")
    # "Owns A and B, and knows C": a comma before the last clause once a clause has its own "and".
    serial = len(clauses) > 1 and any(len(items) > 1 for _verb, items in groups)
    sentence = ", ".join(clauses[:-1]) + ", and " + clauses[-1] if serial else _series(clauses)
    return kinds.capitalized(sentence) + "."


def _documents(graph, person: Entity, params: dict, result: ToolResult) -> str:
    rows = graph.ro_query(
        f"""
        MATCH (:Person {{id: $id}})-[:AUTHORED]->(doc:Document)
        WHERE {SCOPED.format(var="doc")}
        RETURN doc.id, doc.name
        ORDER BY doc.name
        """,
        params,
    ).result_set
    if not rows:
        return ""
    for doc_id, doc in rows[:MAX_DOCUMENTS]:
        result.add(PathStep(person.name, "AUTHORED", doc, from_id=person.id, to_id=doc_id, from_label="Person", to_label="Document"))
    named = [f"the {doc}" for _id, doc in rows[:MAX_DOCUMENTS]]
    if len(rows) > MAX_DOCUMENTS:
        named.append(f"{len(rows) - MAX_DOCUMENTS} more")
    return f"Wrote {join_names(named)}."


def _pull_requests(graph, person: Entity, params: dict, result: ToolResult) -> str:
    """Their pull requests and reviews through their GitHub account, open first."""
    rows = graph.ro_query(
        f"""
        MATCH (:Person {{id: $id}})<-[:IS]-(:GitHubUser)-[rel:AUTHORED|REVIEWED]->(pr:PullRequest)
        WHERE {SCOPED.format(var="pr")}
        OPTIONAL MATCH (pr)-[:TOUCHES]->(s:Service)
        WHERE {SCOPED.format(var="s")}
        RETURN pr.id, pr.name, coalesce(pr.state, ''), coalesce(pr.merged, false), type(rel), collect(DISTINCT [s.id, s.name]),
               coalesce(pr.updated_at, '')
        """,
        params,
    ).result_set
    if not rows:
        return ""
    is_open = {pr_id: state == "open" and not merged for pr_id, _name, state, merged, *_rest in rows}
    # Open first, then authored before reviewed, then the most recently updated, then by name.
    rows.sort(key=lambda row: row[1])
    rows.sort(key=lambda row: row[6], reverse=True)
    rows.sort(key=lambda row: (not is_open[row[0]], row[4] != "AUTHORED"))
    groups: dict[str, list[str]] = {}
    for pr_id, name, _state, _merged, rel, touched, _updated in rows[:MAX_PULL_REQUESTS]:
        result.add(PathStep(person.name, rel, name, from_id=person.id, to_id=pr_id, from_label="Person", to_label="PullRequest"))
        for service_id, service in touched:
            if service_id is not None:
                result.add(PathStep(name, "TOUCHES", service, from_id=pr_id, to_id=service_id, from_label="PullRequest", to_label="Service"))
        groups.setdefault(rel, []).append(name + (" (open)" if is_open[pr_id] else ""))
    clauses = [f"{'authored' if rel == 'AUTHORED' else 'reviewed'} {join_names(names)}" for rel, names in groups.items()]
    if len(rows) > MAX_PULL_REQUESTS:
        clauses.append(f"{len(rows) - MAX_PULL_REQUESTS} more")
    return f"Pull requests: {'; '.join(clauses)}."


def _tickets(graph, person: Entity, params: dict, result: ToolResult) -> str:
    """Open tickets they worked on, or are assigned through a linked account (GitHub, Jira)."""
    rows = graph.ro_query(
        f"""
        MATCH (:Person {{id: $id}})-[:WORKED_ON]->(ticket:Ticket)
        WHERE {SCOPED.format(var="ticket")} AND coalesce(ticket.state, 'open') <> 'closed'
        RETURN ticket.id AS id, ticket.name AS name, coalesce(ticket.title, ticket.name) AS title, 'WORKED_ON' AS rel
        UNION
        MATCH (:Person {{id: $id}})<-[:IS]-(account)-[:ASSIGNED]->(ticket:Ticket)
        WHERE labels(account)[0] IN $accounts AND {SCOPED.format(var="account")}
          AND {SCOPED.format(var="ticket")} AND coalesce(ticket.state, 'open') <> 'closed'
        RETURN ticket.id AS id, ticket.name AS name, coalesce(ticket.title, ticket.name) AS title, 'ASSIGNED' AS rel
        """,
        {**params, "accounts": list(ask_service.ACCOUNT_LABELS)},
    ).result_set
    tickets: dict[str, tuple[str, str, str]] = {}
    for ticket_id, name, title, rel in sorted(rows, key=lambda row: (row[1], row[3] != "WORKED_ON")):
        tickets.setdefault(ticket_id, (name, title, rel))
    if not tickets:
        return ""
    named = []
    for ticket_id, (name, title, rel) in list(tickets.items())[:MAX_TICKETS]:
        result.add(PathStep(person.name, rel, name, from_id=person.id, to_id=ticket_id, from_label="Person", to_label="Ticket"))
        named.append(f"{name} (“{title}”)" if title and title != name else name)
    if len(tickets) > MAX_TICKETS:
        named.append(f"{len(tickets) - MAX_TICKETS} more")
    return f"Open tickets: {join_names(named)}."


# --- decisions -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _DecisionRow:
    id: str
    name: str
    decided_at: int | None
    aliases: str
    roles: tuple[str, ...]
    # (id, name, role) for everyone else on it.
    others: tuple[tuple[str, str, str], ...]

    @property
    def short(self) -> str:
        """How a list of shared work names it: "the billing retries decision"."""
        alias = next((alias.strip() for alias in self.aliases.split(",") if alias.strip()), "")
        return f"the {alias} decision" if alias else f"*{self.name}*"


def _decision_rows(graph, params: dict) -> list[_DecisionRow]:
    rows = graph.ro_query(
        f"""
        MATCH (:Person {{id: $id}})-[mine:PROPOSED|APPROVED]->(d:Decision)
        WHERE {SCOPED.format(var="d")}
        WITH d, collect(DISTINCT type(mine)) AS roles
        OPTIONAL MATCH (other:Person)-[theirs:PROPOSED|APPROVED]->(d)
        WHERE other.id <> $id
        RETURN d.id, d.name, d.decided_at, coalesce(d.aliases, ''), roles,
               collect(DISTINCT CASE WHEN other IS NULL THEN NULL ELSE [other.id, other.name, type(theirs)] END)
        ORDER BY coalesce(d.decided_at, 0) DESC, d.name
        """,
        params,
    ).result_set
    return [
        _DecisionRow(decision_id, name, decided_at, aliases, tuple(sorted(roles, reverse=True)), tuple(tuple(other) for other in others if other))
        for decision_id, name, decided_at, aliases, roles, others in rows
    ]


def _decisions_sentence(graph, person: Entity, rows: list[_DecisionRow], scopes: list[str], result: ToolResult) -> str:
    """"Proposed *X* (20 Sep 2026, still standing, approved by Tomas Reyes).", newest first."""
    if not rows:
        return ""
    phrases = []
    for row in rows[:MAX_DECISIONS]:
        for role in row.roles:
            result.add(PathStep(person.name, role, row.name, from_id=person.id, to_id=row.id, from_label="Person", to_label="Decision"))
        verb = " and ".join(role.lower() for role in row.roles)
        details = [decisions.format_date(row.decided_at)] if row.decided_at is not None else []
        current = decisions.current_decision(graph, row.id, scopes)
        if current is None or current.id == row.id:
            details.append("still standing")
        else:
            details.append(f"replaced by *{current.name}*")
        for opposite, word in (("APPROVED", "approved"), ("PROPOSED", "proposed")):
            if opposite in row.roles:
                continue
            cosigners = [(other_id, other) for other_id, other, role in row.others if role == opposite]
            if cosigners:
                details.append(f"{word} by {join_names([other for _id, other in cosigners])}")
                for other_id, other in cosigners:
                    result.add(PathStep(other, opposite, row.name, from_id=other_id, to_id=row.id, from_label="Person", to_label="Decision"))
        phrases.append(f"{verb} *{row.name}* ({', '.join(details)})")
    sentence = "; ".join(phrases)
    if len(rows) > MAX_DECISIONS:
        sentence += f"; and {len(rows) - MAX_DECISIONS} more"
    return kinds.capitalized(sentence) + "."


# --- who they work with ----------------------------------------------------------------------


def _collaborators(graph, person: Entity, params: dict, held: list[_Held], rows: list[_DecisionRow], result: ToolResult) -> str:
    """The two people they share the most with: things, review pairs, decision pairs."""
    collaborators: dict[str, _Collaborator] = {}

    def of(other_id: str, other: str) -> _Collaborator:
        return collaborators.setdefault(other_id, _Collaborator(other_id, other))

    order = {thing.id: index for index, thing in enumerate(held)}
    names = {thing.id: kinds.listed(thing.name, thing.kind) for thing in held}
    shared = graph.ro_query(
        f"""
        MATCH (:Person {{id: $id}})-[:OWNS|WORKED_ON|KNOWS]->(s:Service)<-[theirs:OWNS|WORKED_ON|KNOWS]-(other:Person)
        WHERE {SCOPED.format(var="s")} AND other.id <> $id
        RETURN other.id, other.name, s.id, s.name, collect(DISTINCT type(theirs))
        """,
        params,
    ).result_set
    for other_id, other, service_id, service, rels in sorted(shared, key=lambda row: (order.get(row[2], len(order)), row[1])):
        step = PathStep(other, min(rels, key=_HOLDS.index), service, from_id=other_id, to_id=service_id, from_label="Person", to_label="Service")
        of(other_id, other).share(names.get(service_id, service), step)

    reviews = graph.ro_query(
        f"""
        MATCH (:Person {{id: $id}})<-[:IS]-(:GitHubUser)-[mine:AUTHORED|REVIEWED]->(pr:PullRequest)<-[theirs:AUTHORED|REVIEWED]-(:GitHubUser)-[:IS]->(other:Person)
        WHERE {SCOPED.format(var="pr")} AND other.id <> $id AND type(mine) <> type(theirs)
        RETURN DISTINCT other.id, other.name, pr.id, pr.name, type(theirs)
        ORDER BY pr.name
        """,
        params,
    ).result_set
    for other_id, other, pr_id, pr, rel in reviews:
        of(other_id, other).share(pr, PathStep(other, rel, pr, from_id=other_id, to_id=pr_id, from_label="Person", to_label="PullRequest"))

    for row in rows:
        for other_id, other, role in row.others:
            if role not in row.roles:
                of(other_id, other).share(row.short, PathStep(other, role, row.name, from_id=other_id, to_id=row.id, from_label="Person", to_label="Decision"))

    top = sorted(collaborators.values(), key=lambda collaborator: (-collaborator.score, collaborator.name))[:MAX_COLLABORATORS]
    if not top:
        return ""
    named = []
    for collaborator in top:
        result.add(collaborator.shared[0][1])
        named.append(f"{collaborator.name} ({', '.join(what for what, _step in collaborator.shared)})")
    return f"Works most with {join_names(named)}."


# --- what they said --------------------------------------------------------------------------


def _said(graph, person: Entity, params: dict) -> list[MemoryLine]:
    rows = graph.ro_query(
        f"""
        MATCH (:Person {{id: $id}})<-[:IS]-(:SlackUser)-[:SAID]->(m:Memory)
        WHERE m.scope IN $scopes AND {_LIVE}
        RETURN m.text, m.scope, m.channel_id, m.created_at, m.ts
        ORDER BY coalesce(m.created_at, 0) DESC
        LIMIT $limit
        """,
        {**params, "limit": MAX_MEMORIES},
    ).result_set
    return [
        MemoryLine(text=text or "", learned_at=day_from(created_at, ts), author=person.name, scope=scope, channel_id=channel_id)
        for text, scope, channel_id, created_at, ts in rows
    ]


def _series(clauses: list[str]) -> str:
    """A and B, or A, B, and C once there are three."""
    if len(clauses) <= 2:
        return " and ".join(clauses)
    return ", ".join(clauses[:-1]) + ", and " + clauses[-1]
