"""People insights: who leads on a measure across the company, and who to ask about a thing now.

``people_ranking`` counts one measure per person, each one cited query, every node scoped:

- holds: things of any kind they ``OWNS``, ``WORKED_ON`` or ``KNOWS``;
- owns: things they ``OWNS``;
- reviews: pull requests their GitHub account ``REVIEWED``;
- authored: pull requests their GitHub account ``AUTHORED``, and documents they ``AUTHORED``;
- decisions: decisions they ``PROPOSED`` or ``APPROVED``;
- load: what is on their plate now: things they own (accounts aside), open tickets they worked on
  or are assigned, and open pull requests they wrote or were asked to review;
- bridges: other teams whose things they hold, beside the team they are a ``MEMBER_OF``.

A team in the question narrows the field to its members. The reply names the leader with the
edges behind the count and the runners-up with theirs, and the grid has every person's count on
every measure, for a page to draw as a leaderboard.

``go_to`` answers "who is the go-to for postgres right now" and "who last worked on billing":
``who_knows``' own ranking of the thing (``ask_service.knowers`` and ``rank_people``, so the order
agrees), each person with their last dated touch on it: a pull request they wrote or reviewed, a
memory they said that mentions it, a decision about it they proposed or approved, a ticket about
it they worked on. It leads with the most recent dated touch, whoever made it, and says when
that was over ``STALE_DAYS`` ago.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.db.falkordb_client import connect
from src.domain.knowledge import ask_service, decisions, fallback, kinds, resolve
from src.domain.knowledge.dto import MemoryLine, PathStep, ToolResult, day_from
from src.domain.knowledge.memories import _LIVE
from src.domain.knowledge.resolve import SCOPED, Entity, join_names

RANK_TOOL = "people_ranking"
GO_TO_TOOL = "go_to"
MEASURES = ("holds", "owns", "reviews", "authored", "decisions", "load", "bridges")
DEFAULT_LIMIT = 3
MAX_LIMIT = 10
STALE_DAYS = 90
MAX_GO_TO = 3
_DAY_MS = 24 * 60 * 60 * 1000
_CLIP = 48

# The headline for each measure, and the empty answer when nothing counts yet.
_HEADLINES = {
    "holds": "Holds the most",
    "owns": "Owns the most",
    "reviews": "Reviewed the most pull requests",
    "authored": "Wrote the most",
    "decisions": "On the most decisions",
    "load": "Carries the most right now",
    "bridges": "Bridges the most teams",
}
_EMPTY = {
    "holds": "No one is linked to anything in the graph you can read yet.",
    "owns": "No one owns anything in the graph you can read yet.",
    "reviews": "No pull request reviews are in the graph you can read yet.",
    "authored": "No pull requests or documents with an author are in the graph you can read yet.",
    "decisions": "No one has proposed or approved a decision in the graph you can read yet.",
    "load": "No one owns anything or has open work in the graph you can read yet.",
    "bridges": "No one holds anything another team runs in the graph you can read yet.",
}
_COLUMN_LABELS = {
    "holds": "Holds",
    "owns": "Owns",
    "reviews": "Reviews",
    "authored": "Wrote",
    "decisions": "Decisions",
    "load": "Load now",
    "bridges": "Other teams",
}
_VERBS = {"OWNS": "owns", "WORKED_ON": "works on", "KNOWS": "knows", "REVIEWED": "reviewed", "AUTHORED": "wrote",
          "PROPOSED": "proposed", "APPROVED": "approved", "ASSIGNED": "assigned", "REVIEW_REQUESTED": "asked to review"}
_ORDER = ("OWNS", "WORKED_ON", "KNOWS", "AUTHORED", "REVIEWED", "REVIEW_REQUESTED", "PROPOSED", "APPROVED", "ASSIGNED")

# Which measure a ranking question asks for, from its verb. The first that matches wins.
_MEASURE_WORDS = (
    (re.compile(r"\breview(?:ed|s|ers?)?\b", re.IGNORECASE), "reviews"),
    (re.compile(r"\bbridges?\b", re.IGNORECASE), "bridges"),
    (re.compile(r"\b(?:proposed|approved|decisions?)\b", re.IGNORECASE), "decisions"),
    (re.compile(r"\b(?:wrote|authored|authors?|contributors?|prolific)\b", re.IGNORECASE), "authored"),
    (re.compile(r"\b(?:busiest|loaded|overloaded|stretched|most active|open|tickets|plate|work)\b", re.IGNORECASE), "load"),
    (re.compile(r"\bowns?\b|\bowners?\b", re.IGNORECASE), "owns"),
)

# Words that ask for a ranking without naming a team: what is left names the team, if anything.
_RANK_WORDS = frozenset(
    {
        "most", "reviewed", "reviews", "review", "reviewer", "reviewers", "owns", "own", "owner", "owners", "worked", "works",
        "wrote", "authored", "author", "authors", "proposed", "approved", "knows", "know", "holds", "hold", "busiest",
        "loaded", "overloaded", "stretched", "active", "prolific", "people", "person", "engineers", "engineer",
        "teammates", "teammate", "top", "contributors", "contributor", "bridges", "bridge", "teams", "team", "code",
        "things", "services", "service", "pull", "requests", "request", "prs", "decisions", "decision", "has", "have",
        "the", "who", "whos", "s", "is", "in", "on", "our", "company", "here", "right", "now", "carries", "open", "tickets",
        "ticket", "work", "plate", "their", "his", "her",
    }
)


def measure_in(question: str) -> str:
    """The measure a ranking question asks for; holds when no verb picks another."""
    for pattern, measure in _MEASURE_WORDS:
        if pattern.search(question or ""):
            return measure
    return "holds"


def now_millis() -> int:
    """The clock ``go_to`` judges staleness by; tests patch it."""
    return decisions.now_millis()


# --- people ranking ------------------------------------------------------------------------


@dataclass
class _Tally:
    """One person's count on one measure, with the edges behind it."""

    id: str
    name: str
    items: dict[str, tuple[str, str]] = field(default_factory=dict)  # item id -> (name, strongest rel)
    steps: list[PathStep] = field(default_factory=list)
    # For bridges: the other team for each item.
    teams: dict[str, str] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(set(self.teams.values())) if self.teams else len(self.items)


def people_ranking(measure: str | None, scopes: list[str], team: str | None = None, limit=None) -> ToolResult:
    """The people who lead on one measure, the leader's count told from its edges."""
    measure = measure if measure in MEASURES else "holds"
    try:
        limit = max(1, min(MAX_LIMIT, int(limit))) if limit is not None else DEFAULT_LIMIT
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    _, graph = connect()
    members, team_entity = _members(graph, team, scopes)
    tallies = {name: _tally(graph, name, scopes) for name in MEASURES}
    people = _people(graph, scopes, members)
    result = ToolResult(tool=RANK_TOOL, answer="")
    ranked = sorted((tally for tally in tallies[measure].values() if tally.id in people and tally.count), key=lambda tally: (-tally.count, tally.name))
    result.grid = _rank_grid(measure, people, tallies)
    within = f" on {team_entity.name}" if team_entity else ""
    if not ranked:
        result.answer = _EMPTY[measure] if not team_entity else _EMPTY[measure].replace(" in the graph", f" on {team_entity.name} in the graph")
        return result
    leader = ranked[0]
    # Everyone tied for the lead is named, even past the limit, so a tie is never cut short.
    ties = [tally for tally in ranked if tally.count == leader.count]
    shown = ranked[: max(limit, len(ties))]
    rest = [tally for tally in shown if tally.count != leader.count]
    headline = f"{_HEADLINES[measure]}{within}"
    details = _details(measure, leader)
    if len(ties) > 1:
        answer = f"{headline}: {join_names([tally.name for tally in ties])} tie at {leader.count}; {leader.name} ({details})"
    elif measure == "bridges":
        answer = f"{headline}: {leader.name} ({details})"
    else:
        answer = f"{headline}: {leader.name} ({leader.count}: {details})"
    if rest:
        answer += f", then {join_names([f'{tally.name} ({tally.count})' for tally in rest])}"
    result.answer = answer + "."
    result.extend(leader.steps)
    for tally in shown[1:]:
        if tally.steps:
            result.add(tally.steps[0])
    return result


def _members(graph, team: str | None, scopes: list[str]) -> tuple[set[str] | None, Entity | None]:
    """The ids of the named team's members, or None (everyone) when the question names no team."""
    words = set(resolve.core(team or "").split()) - _RANK_WORDS
    if not words:
        return None, None
    resolution = resolve.resolve_name(team, ("Team",), scopes, graph=graph)
    if not resolution.found:
        return None, None
    rows = graph.ro_query("MATCH (p:Person)-[:MEMBER_OF]->(:Team {id: $id}) RETURN p.id", {"id": resolution.entity.id}).result_set
    return {row[0] for row in rows}, resolution.entity


def _people(graph, scopes: list[str], members: set[str] | None) -> dict[str, tuple[str, str | None]]:
    """Every person in the field: id -> (name, first team)."""
    rows = graph.ro_query(
        f"""
        MATCH (p:Person)
        WHERE {SCOPED.format(var="p")}
        OPTIONAL MATCH (p)-[:MEMBER_OF]->(t:Team)
        RETURN p.id, p.name, min(t.name)
        """,
        {"scopes": scopes},
    ).result_set
    return {person_id: (name, team) for person_id, name, team in rows if members is None or person_id in members}


# Each measure is one query returning (person id, person, item id, item, item label, rel, step from
# name, step from id, step from label, extra team id, extra team): one countable item and the edge
# that counts it. "extra" is the other team, for bridges.
_MEASURE_QUERIES = {
    "holds": f"""
        MATCH (p:Person)-[r:OWNS|WORKED_ON|KNOWS]->(s:Service)
        WHERE {SCOPED.format(var="s")}
        RETURN p.id AS pid, p.name AS pname, s.id AS iid, s.name AS iname, 'Service' AS ilabel, type(r) AS rel, null AS tid, null AS tname
    """,
    "owns": f"""
        MATCH (p:Person)-[r:OWNS]->(s:Service)
        WHERE {SCOPED.format(var="s")}
        RETURN p.id AS pid, p.name AS pname, s.id AS iid, s.name AS iname, 'Service' AS ilabel, type(r) AS rel, null AS tid, null AS tname
    """,
    "reviews": f"""
        MATCH (p:Person)<-[:IS]-(:GitHubUser)-[r:REVIEWED]->(pr:PullRequest)
        WHERE {SCOPED.format(var="pr")}
        RETURN p.id AS pid, p.name AS pname, pr.id AS iid, pr.name AS iname, 'PullRequest' AS ilabel, type(r) AS rel, null AS tid, null AS tname
    """,
    "authored": f"""
        MATCH (p:Person)<-[:IS]-(:GitHubUser)-[r:AUTHORED]->(pr:PullRequest)
        WHERE {SCOPED.format(var="pr")}
        RETURN p.id AS pid, p.name AS pname, pr.id AS iid, pr.name AS iname, 'PullRequest' AS ilabel, type(r) AS rel, null AS tid, null AS tname
        UNION
        MATCH (p:Person)-[r:AUTHORED]->(doc:Document)
        WHERE {SCOPED.format(var="doc")}
        RETURN p.id AS pid, p.name AS pname, doc.id AS iid, doc.name AS iname, 'Document' AS ilabel, type(r) AS rel, null AS tid, null AS tname
    """,
    "decisions": f"""
        MATCH (p:Person)-[r:PROPOSED|APPROVED]->(d:Decision)
        WHERE {SCOPED.format(var="d")}
        RETURN p.id AS pid, p.name AS pname, d.id AS iid, d.name AS iname, 'Decision' AS ilabel, type(r) AS rel, null AS tid, null AS tname
    """,
    "load": f"""
        MATCH (p:Person)-[r:OWNS]->(s:Service)
        WHERE {SCOPED.format(var="s")} AND NOT {kinds.KIND_OF.format(var="s")} IN $excluded
        RETURN p.id AS pid, p.name AS pname, s.id AS iid, s.name AS iname, 'Service' AS ilabel, type(r) AS rel, null AS tid, null AS tname
        UNION
        MATCH (p:Person)-[r:WORKED_ON]->(t:Ticket)
        WHERE {SCOPED.format(var="t")} AND coalesce(t.state, 'open') <> 'closed'
        RETURN p.id AS pid, p.name AS pname, t.id AS iid, t.name AS iname, 'Ticket' AS ilabel, type(r) AS rel, null AS tid, null AS tname
        UNION
        MATCH (p:Person)<-[:IS]-(:GitHubUser)-[r:ASSIGNED]->(t:Ticket)
        WHERE {SCOPED.format(var="t")} AND coalesce(t.state, 'open') <> 'closed'
        RETURN p.id AS pid, p.name AS pname, t.id AS iid, t.name AS iname, 'Ticket' AS ilabel, type(r) AS rel, null AS tid, null AS tname
        UNION
        MATCH (p:Person)<-[:IS]-(:GitHubUser)-[r:AUTHORED|REVIEW_REQUESTED]->(pr:PullRequest)
        WHERE {SCOPED.format(var="pr")} AND coalesce(pr.state, '') = 'open' AND NOT coalesce(pr.merged, false)
        RETURN p.id AS pid, p.name AS pname, pr.id AS iid, pr.name AS iname, 'PullRequest' AS ilabel, type(r) AS rel, null AS tid, null AS tname
    """,
    "bridges": f"""
        MATCH (p:Person)-[r:OWNS|WORKED_ON|KNOWS]->(s:Service)<-[:RUNS]-(t:Team)
        WHERE {SCOPED.format(var="s")} AND {SCOPED.format(var="t")} AND NOT (p)-[:MEMBER_OF]->(t)
        RETURN p.id AS pid, p.name AS pname, s.id AS iid, s.name AS iname, 'Service' AS ilabel, type(r) AS rel, t.id AS tid, t.name AS tname
    """,
}


def _tally(graph, measure: str, scopes: list[str]) -> dict[str, _Tally]:
    rows = graph.ro_query(_MEASURE_QUERIES[measure], {"scopes": scopes, "excluded": sorted(kinds.NOT_DEPENDABLE)}).result_set
    rows.sort(key=lambda row: (_ORDER.index(row[5]) if row[5] in _ORDER else len(_ORDER), str(row[3])))
    tallies: dict[str, _Tally] = {}
    for person_id, person, item_id, item, label, rel, team_id, team in rows:
        tally = tallies.setdefault(person_id, _Tally(person_id, person))
        if item_id not in tally.items:
            tally.items[item_id] = (item, rel)
            tally.steps.append(PathStep(person, rel, item, from_id=person_id, to_id=item_id, from_label="Person", to_label=label))
            if team_id is not None:
                tally.steps.append(PathStep(team, "RUNS", item, from_id=team_id, to_id=item_id, from_label="Team", to_label=label))
        if team_id is not None:
            tally.teams.setdefault(item_id, team)
    return tallies


def _details(measure: str, tally: _Tally) -> str:
    """What the leader's count is made of: "owns Postgres and Graph Store, knows Billing Service and Auth Service"."""
    if measure == "bridges":
        by_team: dict[str, list[str]] = {}
        for item_id, team in tally.teams.items():
            by_team.setdefault(team, []).append(tally.items[item_id][0])
        teams = len(by_team)
        told = join_names([f"{team} through {join_names(things)}" for team, things in sorted(by_team.items())])
        return f"{teams} other team{'s' if teams != 1 else ''}: {told}"
    groups: dict[str, list[str]] = {}
    for item, rel in tally.items.values():
        shown = f"*{item}*" if measure == "decisions" else (f"the {item}" if rel == "AUTHORED" and not re.search(r"#\d+$", item) else item)
        groups.setdefault(_VERBS.get(rel, rel.lower()), []).append(shown)
    return ", ".join(f"{verb} {join_names(items)}" for verb, items in groups.items())


def _rank_grid(measure: str, people: dict[str, tuple[str, str | None]], tallies: dict[str, dict[str, _Tally]]) -> dict:
    rows = []
    for person_id, (name, team) in people.items():
        row = {"id": person_id, "name": name, "team": team}
        for key in MEASURES:
            tally = tallies[key].get(person_id)
            row[key] = tally.count if tally else 0
        rows.append(row)
    rows.sort(key=lambda row: (-row[measure], row["name"]))
    columns = [{"key": key, "label": _COLUMN_LABELS[key], "type": "count", **({"hot": "high"} if key == measure else {})} for key in MEASURES]
    return {"kind": RANK_TOOL, "measure": measure, "columns": columns, "rows": rows}


# --- go-to ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Touch:
    """One dated thing a person did on the thing asked about."""

    person_id: str
    person: str
    at: int
    what: str
    steps: tuple[PathStep, ...]
    memory: MemoryLine | None = None


_GO_TO_FIRST = re.compile(r"\bgo[- ]to\b|\bright now\b|\bthese days\b|\bcurrently\b|\blately\b", re.IGNORECASE)
# "go-to" is no part of the thing named: "the go-to for the espresso machine" names the espresso machine.
_GO_TO_WORD = re.compile(r"\bgo[- ]to\b", re.IGNORECASE)


def go_to(name: str, scopes: list[str], question: str | None = None) -> ToolResult:
    """Who to ask about a thing now: who_knows' ranking, each person with their last dated touch on it."""
    question = question or name or ""
    _, graph = connect()
    resolution = resolve.resolve_name(_GO_TO_WORD.sub(" ", name or ""), ask_service.ANSWER_LABELS, scopes, graph=graph)
    if not resolution.found:
        miss = ToolResult(tool=GO_TO_TOOL, answer=resolve.describe_miss(resolution), found=False, candidates=resolution.candidate_names())
        return fallback.before_refusing(question, scopes, miss)
    target = resolution.entity
    known = ask_service.knowers(graph, target, scopes)
    ranked = ask_service.rank_people(known.path, target)
    touches = _touches(graph, target, scopes)
    last: dict[str, Touch] = {}
    for touch in touches:
        if touch.person not in last or touch.at > last[touch.person].at:
            last[touch.person] = touch

    result = ToolResult(tool=GO_TO_TOOL, answer="", candidates=resolution.candidate_names())
    named = kinds.subject(target.name, target.kind) if target.label == "Service" else target.name
    cutoff = now_millis() - STALE_DAYS * _DAY_MS
    latest = max((touch.at for touch in touches), default=None)
    latest_sentence = _latest_sentence(touches, latest, cutoff) if latest is not None else "No one has a dated touch on it in the graph yet."

    people = []
    for person, _score, reasons in ranked[:MAX_GO_TO]:
        touch = last.get(person)
        when = "no dated activity on it" if touch is None else f"last {decisions.format_date(touch.at)}" + (", stale" if touch.at < cutoff else "")
        people.append(f"{person} ({reasons}; {when})")
        result.extend(ask_service.edges_of(known.path, [person]))
    if len(ranked) > MAX_GO_TO:
        people.append(f"{len(ranked) - MAX_GO_TO} more")
    go_to_sentence = f"Go-to for {named}: {join_names(people)}." if people else f"No one is linked to {named} yet."

    if _GO_TO_FIRST.search(question):
        tail = latest_sentence.replace("Last touched ", "Most recent dated touch: ", 1) if latest is not None else latest_sentence
        parts = [go_to_sentence, tail]
    else:
        parts = [latest_sentence, go_to_sentence]
    result.answer = " ".join(parts)
    for touch in sorted(touches, key=lambda touch: -touch.at)[:6]:
        result.extend(touch.steps)
    result.memories = [touch.memory for touch in sorted(touches, key=lambda touch: -touch.at) if touch.memory][:2]
    return result


def _latest_sentence(touches: list[Touch], latest: int, cutoff: int) -> str:
    """"Last touched 20 Sep 2026 by Arjun Mehta (proposed *X*, said “…”) and Tomas Reyes (approved it)."."""
    day = decisions.format_date(latest)
    on_day = [touch for touch in touches if decisions.format_date(touch.at) == day]
    by_person: dict[str, list[str]] = {}
    seen_whats: set[str] = set()
    for touch in sorted(on_day, key=lambda touch: (touch.person, -touch.at)):
        what = touch.what
        # A decision already named on this day reads "it" the second time.
        decision = re.match(r"(proposed|approved) (\*.+\*)$", what)
        if decision and decision.group(2) in seen_whats:
            what = f"{decision.group(1)} it"
        elif decision:
            seen_whats.add(decision.group(2))
        by_person.setdefault(touch.person, []).append(what)
    stale = f", over {STALE_DAYS} days ago," if latest < cutoff else ""
    told = join_names([f"{person} ({', '.join(whats)})" for person, whats in by_person.items()])
    return f"Last touched {day}{stale} by {told}."


def _touches(graph, target: Entity, scopes: list[str]) -> list[Touch]:
    """Every dated thing anyone did on the target, through their own edges."""
    params = {"id": target.id, "scopes": scopes}
    label = target.label
    anchor = resolve.label_of(target)  # each read seeks the target by its label's id index, then walks out
    touches: list[Touch] = []
    for person_id, person, rel, pr_id, pr, updated in graph.ro_query(
        f"""
        MATCH (x:{anchor} {{id: $id}})
        WITH x
        MATCH (p:Person)<-[:IS]-(:GitHubUser)-[r:AUTHORED|REVIEWED]->(pr:PullRequest)-[:TOUCHES]->(x)
        WHERE {SCOPED.format(var="pr")}
        RETURN p.id, p.name, type(r), pr.id, pr.name, coalesce(pr.updated_at, '')
        """,
        params,
    ).result_set:
        at = _millis(updated)
        if at is not None:
            steps = (
                PathStep(person, rel, pr, from_id=person_id, to_id=pr_id, from_label="Person", to_label="PullRequest"),
                PathStep(pr, "TOUCHES", target.name, from_id=pr_id, to_id=target.id, from_label="PullRequest", to_label=label),
            )
            touches.append(Touch(person_id, person, at, f"{_VERBS[rel]} {pr}", steps))
    for person_id, person, memory_id, text, scope, channel_id, created, ts in graph.ro_query(
        f"""
        MATCH (x:{anchor} {{id: $id}})
        WITH x
        MATCH (p:Person)<-[:IS]-(:SlackUser)-[:SAID]->(m:Memory)-[:MENTIONS]->(x)
        WHERE m.scope IN $scopes AND {_LIVE}
        RETURN p.id, p.name, m.id, m.text, m.scope, m.channel_id, m.created_at, m.ts
        """,
        params,
    ).result_set:
        at = _millis(created) or _millis(ts)
        if at is not None:
            name = text if len(text or "") <= _CLIP else (text or "")[: _CLIP - 1].rstrip() + "…"
            # The Slack account that said it is collapsed into its person, as knowers collapses GitHub accounts.
            steps = (
                PathStep(person, "SAID", name, from_id=person_id, to_id=memory_id, from_label="Person", to_label="Memory"),
                PathStep(name, "MENTIONS", target.name, from_id=memory_id, to_id=target.id, from_label="Memory", to_label=label),
            )
            memory = MemoryLine(text=text or "", learned_at=day_from(created, ts), author=person, scope=scope, channel_id=channel_id)
            touches.append(Touch(person_id, person, at, f"said “{name}”", steps, memory))
    for person_id, person, rel, decision_id, decision, decided in graph.ro_query(
        f"""
        MATCH (x:{anchor} {{id: $id}})
        WITH x
        MATCH (p:Person)-[r:PROPOSED|APPROVED]->(d:Decision)-[:ABOUT]->(x)
        WHERE {SCOPED.format(var="d")}
        RETURN p.id, p.name, type(r), d.id, d.name, d.decided_at
        """,
        params,
    ).result_set:
        at = decisions.to_millis(decided)
        if at is not None:
            steps = (
                PathStep(person, rel, decision, from_id=person_id, to_id=decision_id, from_label="Person", to_label="Decision"),
                PathStep(decision, "ABOUT", target.name, from_id=decision_id, to_id=target.id, from_label="Decision", to_label=label),
            )
            touches.append(Touch(person_id, person, at, f"{_VERBS[rel]} *{decision}*", steps))
    for person_id, person, ticket_id, ticket, updated in graph.ro_query(
        f"""
        MATCH (x:{anchor} {{id: $id}})
        WITH x
        MATCH (p:Person)-[:WORKED_ON]->(t:Ticket)-[:ABOUT]->(x)
        WHERE {SCOPED.format(var="t")}
        RETURN p.id, p.name, t.id, t.name, coalesce(t.updated_at, t.created_at, '')
        """,
        params,
    ).result_set:
        at = _millis(updated)
        if at is not None:
            steps = (
                PathStep(person, "WORKED_ON", ticket, from_id=person_id, to_id=ticket_id, from_label="Person", to_label="Ticket"),
                PathStep(ticket, "ABOUT", target.name, from_id=ticket_id, to_id=target.id, from_label="Ticket", to_label=label),
            )
            touches.append(Touch(person_id, person, at, f"worked on {ticket}", steps))
    return touches


def _millis(value) -> int | None:
    """Epoch milliseconds from a number or an ISO timestamp ("2026-09-20T10:00:00Z"); None when undated."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return decisions.to_millis(value)
    text = str(value).strip()
    try:
        return decisions.to_millis(float(text))
    except ValueError:
        pass
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)
