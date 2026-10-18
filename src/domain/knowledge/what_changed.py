"""What changed: a dated activity log over the graph, for a service, a team, or the whole company.

``what_changed`` reads four kinds of dated change, each from the edges that tie it to the
graph: decisions (``decided_at``, ``ABOUT``, who ``PROPOSED`` it, what it ``SUPERSEDES``, and
the decision that stands today through ``decisions.current_decision``), memories
(``created_at``, ``MENTIONS``, who ``SAID`` it), pull requests (GitHub's ``updated_at``,
``TOUCHES``, the ``AUTHORED`` GitHub user and the person they are) and tickets (GitHub's
``updated_at``, or ``created_at`` when Slack filed it, and ``ABOUT``).

The question picks the scope. A team means everything it ``RUNS``; a service means itself
and what it depends on, three ``DEPENDS_ON`` hops out, so "billing is failing, what changed"
finds the change one hop upstream (a depth the question names, "one hop", narrows it);
nothing named means the whole company. ``person`` names whose things to read instead
("what did I miss", where Slack fills in the asker): what they own, work on or know. Time words pick the window (``window_in``): a week by
default, widened once to a month when the week is empty, and said so. ``only`` keeps one kind
inside that window ("what did sales decide recently"). Current-state lists (the open
tickets, every decision) are ``find``'s, not this tool's.

When more than one change touched more than one thing, the answer opens as a digest grouped by thing ("Billing
Service: 2 changes; Auth Service (1 hop up): 1"), then tells the changes newest first.

Every node is read through the asker's scopes, and dates are compared in Python, since the
graph stores milliseconds for some and ISO strings for others. ``now_millis`` is the clock,
patched in tests.
"""

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.db.falkordb_client import connect
from src.domain.knowledge import algorithms, coverage, decisions, kinds, people, reading_list, resolve
from src.domain.knowledge.dto import MemoryLine, PathStep, ToolResult
from src.domain.knowledge.memories import LIVE
from src.domain.knowledge.resolve import SCOPED, Entity, join_names

TOOL = "what_changed"
ONLY = ("decisions", "tickets", "pull_requests", "memories")
DEFAULT_DAYS = 7
WIDER_DAYS = 30
MAX_DAYS = 365
# How far upstream a service's changes are looked for. A literal bound in the walk.
MAX_HOPS = 3
# Changes told in full; the rest are counted.
MAX_LISTED = 5
DAY_MS = 86_400_000

_LAST_N_DAYS = re.compile(r"\b(?:last|past) (\d{1,5}) days?\b", re.IGNORECASE)
_WINDOWS = (
    (re.compile(r"\btoday\b", re.IGNORECASE), 1),
    (re.compile(r"\byesterday\b", re.IGNORECASE), 2),
    (re.compile(r"\b(?:this|last|past) week\b", re.IGNORECASE), 7),
    (re.compile(r"\b(?:this|last|past) month\b", re.IGNORECASE), 30),
    (re.compile(r"\b(?:recently|lately)\b", re.IGNORECASE), 14),
)
_ONLY = (
    ("decisions", re.compile(r"\b(?:decid\w*|decisions?)\b", re.IGNORECASE)),
    ("tickets", re.compile(r"\b(?:tickets?|issues?)\b", re.IGNORECASE)),
    ("pull_requests", re.compile(r"\b(?:pull requests?|prs?)\b", re.IGNORECASE)),
    ("memories", re.compile(r"\b(?:memories|said|conversations?)\b", re.IGNORECASE)),
)
# How a reply counts each kind: (one, many).
_NOUNS = {
    None: ("change", "changes"),
    "decisions": ("decision", "decisions"),
    "tickets": ("ticket", "tickets"),
    "pull_requests": ("pull request", "pull requests"),
    "memories": ("thing said", "things said"),
}
_KIND_ORDER = {"decisions": 0, "pull_requests": 1, "tickets": 2, "memories": 3, "accounts": 4}


def now_millis() -> int:
    return int(time.time() * 1000)


def window_in(question: str) -> int | None:
    """Days the question looks back, or None when it says no time: today 1, a week 7, a month 30, recently 14."""
    text = question or ""
    match = _LAST_N_DAYS.search(text)
    if match:
        return max(1, min(MAX_DAYS, int(match.group(1))))
    for pattern, days in _WINDOWS:
        if pattern.search(text):
            return days
    return None


def only_in(question: str) -> str | None:
    """The one kind of change the question asks about, if it names one."""
    for kind, pattern in _ONLY:
        if pattern.search(question or ""):
            return kind
    return None


@dataclass
class Change:
    """One dated (or, in a list, undated) change, with the things it is about and the edges behind it."""

    kind: str
    id: str
    label: str
    at: int | None
    sentence: str
    # (id, name, label) of each thing it is about; the Service ids decide the scope.
    things: list[tuple[str, str, str]]
    steps: list[PathStep] = field(default_factory=list)
    memory: MemoryLine | None = None

    @property
    def service_ids(self) -> set[str]:
        return {thing_id for thing_id, _name, label in self.things if label == "Service"}


@dataclass
class Scope:
    """What the question is about: the company, one team's things, or one service and what it depends on."""

    mode: str
    entity: Entity | None = None
    walked: dict[str, reading_list.Walked] = field(default_factory=dict)
    # A person's own edge to each thing they hold, in person mode.
    held: dict[str, str] = field(default_factory=dict)

    def covers(self, change: Change) -> bool:
        return self.mode == "company" or bool(change.service_ids & set(self.walked))


def what_changed(question: str, scopes: list[str], days=None, only=None, person=None) -> ToolResult:
    """``_what_changed``, saying so when the window asked for was longer than a year."""
    result = _what_changed(question, scopes, days, only, person)
    if _window_capped(question, days):
        result.answer += " (Capped at a year.)"
    return result


def _window_capped(question: str, days) -> bool:
    asked = [days] + [match.group(1) for match in [_LAST_N_DAYS.search(question or "")] if match]
    numbers = []
    for value in asked:
        try:
            numbers.append(int(value))
        except (TypeError, ValueError):
            continue
    return any(number > MAX_DAYS for number in numbers)


def _what_changed(question: str, scopes: list[str], days=None, only=None, person=None) -> ToolResult:
    """What changed in the window, newest first, around what the question names, or around a person's own things."""
    _, graph = connect()
    text = question or ""
    only = only if only in ONLY else None
    window = (_clamp(days) if days is not None else window_in(text)) or DEFAULT_DAYS
    scope = _scope(graph, text, scopes, person)
    now = now_millis()
    # Nothing older than the widest window is ever told: synced tickets are read from there on.
    since = now - max(window, WIDER_DAYS) * DAY_MS
    changes = [change for change in _changes(graph, scopes, only, since) if scope.covers(change)]
    result = ToolResult(tool=TOOL, answer="")
    dated = sorted((change for change in changes if change.at is not None), key=lambda change: (-change.at, _KIND_ORDER[change.kind], change.label))
    recent = [change for change in dated if change.at >= now - window * DAY_MS]
    widened = not recent and window < WIDER_DAYS
    if widened:
        recent = [change for change in dated if change.at >= now - WIDER_DAYS * DAY_MS]
    one, many = _NOUNS[only]
    where = _where(scope)
    if not recent:
        place, span = where or f"around {_subject(scope)}", _days(max(window, WIDER_DAYS) if widened else window)
        result.answer = f"No {many} {place} in the last {span}." if only else f"Nothing dated changed {place} in the last {span}."
        return result
    shown, rest = recent[:MAX_LISTED], recent[MAX_LISTED:]
    _cite(result, scope, shown)
    count = f"{len(recent)} {one if len(recent) == 1 else many}{' ' + where if where else ''}"
    lead = f"Nothing dated in the last {_days(window)}. In the last {WIDER_DAYS}, {count}" if widened else f"In the last {_days(window)}, {count}"
    if scope.mode == "service":
        lead = f"{_around(scope)}: {lead[:1].lower()}{lead[1:]}"
    lines = [f"{_short_day(change.at)}: {change.sentence}." for change in shown]
    if rest:
        lines.append(f"And {len(rest)} more, back to {_short_day(rest[-1].at)}.")
    digest = _digest(scope, recent, one, many)
    parts = [f"{lead}. By thing: {digest}. Newest first: {' '.join(lines)}" if digest else f"{lead}, newest first: {' '.join(lines)}"]
    if scope.mode == "service":
        parts.append(_owners_line(graph, result, scope, recent, scopes))
    # A capped project only holds its newest items: said, so "nothing changed" is never read as everything.
    result.answer = " ".join(part for part in parts if part) + coverage.note_for(graph, scopes, result.path)
    return result


# --- scope ---------------------------------------------------------------------------------


def _scope(graph, text: str, scopes: list[str], person: str | None = None) -> Scope:
    """A team first, then a service, then the person whose things to read; anything else is the whole company."""
    if not text.strip():
        return _person_scope(graph, person, scopes)
    team = resolve.resolve_name(text, ("Team",), scopes, graph=graph)
    if team.found:
        rows = graph.ro_query(
            f"""
            MATCH (:Team {{id: $id}})-[:RUNS]->(s:Service)
            WHERE {SCOPED.format(var="s")}
            RETURN s.id, s.name, {kinds.KIND_OF.format(var="s")}
            """,
            {"id": team.entity.id, "scopes": scopes},
        ).result_set
        walked = {service_id: reading_list.Walked(service_id, name, kind, 0, (service_id,), (name,)) for service_id, name, kind in rows}
        return Scope("team", team.entity, walked)
    service = resolve.resolve_name(text, ("Service",), scopes, graph=graph)
    if service.found:
        named = algorithms.depth_in(text)
        depth = max(1, min(MAX_HOPS, named)) if named is not None else MAX_HOPS
        walked = reading_list.walk(graph, service.entity, scopes, depth=depth)
        return Scope("service", service.entity, {thing.id: thing for thing in walked})
    return _person_scope(graph, person, scopes)


def _person_scope(graph, person: str | None, scopes: list[str]) -> Scope:
    """What a person owns, works on or knows; the whole company when they hold nothing or are not in the graph."""
    # A name, or the asker by id from a Slack turn ("person:<id>"), never a namesake.
    found = people.resolve_person(person, scopes, graph) if person else None
    found = found if found is None or found.entity is None or found.entity.label == "Person" else None
    if found is None or not found.found:
        return Scope("company")
    rows = graph.ro_query(
        f"""
        MATCH (:Person {{id: $id}})-[held:OWNS|WORKED_ON|KNOWS]->(s:Service)
        WHERE {SCOPED.format(var="s")}
        RETURN s.id, s.name, {kinds.KIND_OF.format(var="s")}, collect(type(held))
        """,
        {"id": found.entity.id, "scopes": scopes},
    ).result_set
    if not rows:
        return Scope("company")
    walked = {thing_id: reading_list.Walked(thing_id, name, kind, 0, (thing_id,), (name,)) for thing_id, name, kind, _held in rows}
    # The strongest edge first, as who_knows weighs them.
    held = {thing_id: min(rels, key=("OWNS", "WORKED_ON", "KNOWS").index) for thing_id, _name, _kind, rels in rows}
    return Scope("person", found.entity, walked, held)


def _where(scope: Scope) -> str:
    if scope.mode == "company":
        return "across the company"
    if scope.mode == "team":
        return f"in {scope.entity.name}"
    if scope.mode == "person":
        return f"around what {scope.entity.name} works on"
    return ""


def _subject(scope: Scope) -> str:
    return kinds.subject(scope.entity.name, scope.entity.kind) if scope.entity else "the company"


def _around(scope: Scope) -> str:
    upstream = [thing.subject for thing in sorted(scope.walked.values(), key=lambda thing: (thing.hop, thing.name)) if thing.hop]
    if not upstream:
        return f"Around {_subject(scope)}"
    return f"Around {_subject(scope)} and what it depends on ({', '.join(upstream)})"


# --- changes -------------------------------------------------------------------------------


def _changes(graph, scopes: list[str], only: str | None, since: int | None = None) -> list[Change]:
    readers = {
        "decisions": _decisions, "memories": _memories, "pull_requests": _pull_requests,
        "tickets": lambda graph, scopes: _tickets(graph, scopes, since), "accounts": lambda graph, scopes: _accounts(graph, scopes, since),
    }
    return [change for kind, read in readers.items() if only in (None, kind) for change in read(graph, scopes)]


def _accounts(graph, scopes: list[str], since: int | None = None) -> list[Change]:
    """CRM accounts a connector synced, dated by the CRM's own last change, with their lifecycle stage
    and the stage of each deal on them (the WORKED_ON edges a deal writes). With ``since``, only the
    accounts that may have changed since then have their deals read."""
    rows = graph.ro_query(
        f"""
        MATCH (s:Service)
        WHERE s.source IS NOT NULL AND {kinds.KIND_OF.format(var="s")} = 'account' AND {SCOPED.format(var="s")}
          AND coalesce(s.source_updated_at, '') <> '' AND ($since IS NULL OR s.changed_at_ms IS NULL OR s.changed_at_ms >= $since)
        OPTIONAL MATCH (who)-[r:WORKED_ON]->(s)
        WHERE r.evidence = 'deal' AND labels(who)[0] IN ['Person', 'ExternalUser'] AND {SCOPED.format(var="who")}
        RETURN s.id, s.name, coalesce(s.status, ''), coalesce(s.state, 'open'), s.source_updated_at,
               collect(DISTINCT [who.id, who.name, labels(who)[0], who.source, r.stage])
        """,
        {"scopes": scopes, "since": since},
    ).result_set
    from src.domain.knowledge import people  # people reads the connectors' admin mappings; imported when needed

    changes = []
    for account_id, name, status, state, updated_at, deals in rows:
        at = _iso_millis(updated_at)
        if at is None:
            continue
        from src.domain.knowledge.ask_service import LIFECYCLE_LABELS  # ask_service reaches what_changed through the catalogue

        lifecycle = ", ".join(part for part in (LIFECYCLE_LABELS.get(status.lower(), status.replace("_", " ")), state) if part)
        sentence = f"the {name} account changed in the CRM" + (f" ({lifecycle})" if lifecycle else "")
        steps = []
        told = []
        for who_id, who, label, source, stage in deals:
            if who_id is None:
                continue
            shown = people.account_label(who, source) if label == people.ACCOUNT else who
            told.append(f"{stage} ({shown})" if stage else shown)
            steps.append(_step(shown, "WORKED_ON", name, who_id, account_id, label, "Service"))
        if told:
            sentence += f"; deals: {join_names(sorted(told))}"
        changes.append(Change("accounts", account_id, name, at, sentence, [(account_id, name, "Service")], steps))
    return changes


def _decisions(graph, scopes: list[str]) -> list[Change]:
    rows = graph.ro_query(
        f"""
        MATCH (d:Decision)
        WHERE {SCOPED.format(var="d")}
        OPTIONAL MATCH (d)-[:ABOUT]->(s:Service)
        WHERE {SCOPED.format(var="s")}
        WITH d, collect(DISTINCT [s.id, s.name]) AS about
        OPTIONAL MATCH (p:Person)-[:PROPOSED]->(d)
        WITH d, about, collect(DISTINCT [p.id, p.name]) AS proposers
        OPTIONAL MATCH (d)-[:SUPERSEDES]->(old:Decision)
        WHERE {SCOPED.format(var="old")}
        RETURN d.id, d.name, d.decided_at, about, proposers, collect(DISTINCT [old.id, old.name])
        """,
        {"scopes": scopes},
    ).result_set
    changes = []
    for decision_id, name, decided_at, about, proposers, older in rows:
        about = sorted((thing_id, thing) for thing_id, thing in about if thing_id)
        proposers = sorted((person_id, person) for person_id, person in proposers if person_id)
        older = [(old_id, old) for old_id, old in older if old_id]
        current = decisions.current_decision(graph, decision_id, scopes)
        replaced_by = current.name if current is not None and current.id != decision_id else None
        sentence = f"decided *{name}*" + _about([thing for _id, thing in about])
        if proposers:
            sentence += f", proposed by {join_names([person for _id, person in proposers])}"
        if older:
            sentence += f", replacing {join_names([f'*{old}*' for _id, old in older])}"
        if replaced_by:
            sentence += f"; since replaced by *{replaced_by}*"
        steps = [_step(name, "ABOUT", thing, decision_id, thing_id, "Decision", "Service") for thing_id, thing in about]
        steps += [_step(person, "PROPOSED", name, person_id, decision_id, "Person", "Decision") for person_id, person in proposers]
        steps += [_step(name, "SUPERSEDES", old, decision_id, old_id, "Decision", "Decision") for old_id, old in older]
        things = [(thing_id, thing, "Service") for thing_id, thing in about]
        at = decided_at if isinstance(decided_at, int) and decided_at > 0 else None
        changes.append(Change("decisions", decision_id, f"*{name}*", at, sentence, things, steps))
    return changes


def _memories(graph, scopes: list[str]) -> list[Change]:
    rows = graph.ro_query(
        f"""
        MATCH (m:Memory)
        WHERE m.scope IN $scopes AND {LIVE}
        OPTIONAL MATCH (m)-[:MENTIONS]->(n)
        WHERE (n:Service OR n:Ticket) AND {SCOPED.format(var="n")}
        WITH m, collect(DISTINCT [n.id, n.name, labels(n)[0]]) AS mentions
        OPTIONAL MATCH (u:SlackUser)-[:SAID]->(m)
        OPTIONAL MATCH (u)-[:IS]->(p:Person)
        RETURN m.id, m.text, m.created_at, m.ts, m.scope, m.channel_id, mentions, coalesce(u.name, p.name, u.id, 'someone')
        """,
        {"scopes": scopes},
    ).result_set
    changes = []
    for memory_id, text, created_at, ts, scope, channel_id, mentions, speaker in rows:
        at = created_at if isinstance(created_at, int) and created_at > 0 else _ts_millis(ts)
        things = sorted(((thing_id, thing, label) for thing_id, thing, label in mentions if thing_id), key=lambda thing: (thing[2] != "Service", thing[1]))
        sentence = f"{speaker} said “{text}”" + _about([thing for _id, thing, _label in things])
        line = MemoryLine(text=text or "", learned_at=_iso_day(at), author=speaker, scope=scope, channel_id=channel_id)
        changes.append(Change("memories", memory_id, f"{speaker}'s “{_clip(text)}”", at, sentence, things, memory=line))
    return changes


def _pull_requests(graph, scopes: list[str]) -> list[Change]:
    rows = graph.ro_query(
        f"""
        MATCH (pr:PullRequest)
        WHERE {SCOPED.format(var="pr")}
        OPTIONAL MATCH (pr)-[:TOUCHES]->(s:Service)
        WHERE {SCOPED.format(var="s")}
        WITH pr, collect(DISTINCT [s.id, s.name]) AS touches
        OPTIONAL MATCH (g:GitHubUser)-[:AUTHORED]->(pr)
        OPTIONAL MATCH (g)-[:IS]->(person:Person)
        RETURN pr.id, pr.name, coalesce(pr.title, ''), coalesce(pr.state, ''), coalesce(pr.merged, false), pr.updated_at, touches,
               collect(DISTINCT [coalesce(person.id, g.id), coalesce(person.name, g.login, g.name), CASE WHEN person IS NULL THEN 'GitHubUser' ELSE 'Person' END])
        """,
        {"scopes": scopes},
    ).result_set
    changes = []
    for pr_id, name, title, state, merged, updated_at, touches, authors in rows:
        touches = sorted((thing_id, thing) for thing_id, thing in touches if thing_id)
        authors = [(author_id, author, label) for author_id, author, label in authors if author_id]
        status = "merged" if merged else "closed" if state == "closed" else "open"
        sentence = f"*{name}*" + (f" “{title}”" if title else "")
        if authors:
            sentence += f" by {join_names([author for _id, author, _label in authors])}"
        sentence += f", {status}" + _about([thing for _id, thing in touches])
        steps = [_step(name, "TOUCHES", thing, pr_id, thing_id, "PullRequest", "Service") for thing_id, thing in touches]
        steps += [_step(author, "AUTHORED", name, author_id, pr_id, label, "PullRequest") for author_id, author, label in authors]
        things = [(thing_id, thing, "Service") for thing_id, thing in touches]
        changes.append(Change("pull_requests", pr_id, f"*{name}*", _iso_millis(updated_at), sentence, things, steps))
    return changes


def _tickets(graph, scopes: list[str], since: int | None = None) -> list[Change]:
    """Every readable ticket, or with ``since`` the ones that may have changed since then: a synced
    ticket by its ``changed_at_ms`` (a range-index read, not every ticket), plus every ticket without
    one (seeded, filed from Slack or GitHub: few). Each is dated below exactly as before."""
    ticket_scan = "MATCH (t:Ticket) WHERE {where} AND " + SCOPED.format(var="t")
    tail = f"""
        OPTIONAL MATCH (t)-[:ABOUT]->(s:Service)
        WHERE {SCOPED.format(var="s")}
        RETURN t.id AS id, t.name AS name, coalesce(t.title, '') AS title, coalesce(t.state, 'open') AS state,
               coalesce(t.source_updated_at, t.updated_at) AS updated, t.created_at AS created, collect(DISTINCT [s.id, s.name]) AS about
        """
    if since is None:
        query = ticket_scan.format(where="true") + tail
    else:
        query = ticket_scan.format(where="t.changed_at_ms >= $since") + tail + " UNION ALL " + ticket_scan.format(where="t.changed_at_ms IS NULL") + tail
    rows = graph.ro_query(query, {"scopes": scopes, "since": since}).result_set
    changes = []
    for ticket_id, name, title, state, updated_at, created_at, about in rows:
        about = sorted((thing_id, thing) for thing_id, thing in about if thing_id)
        at = created_at if isinstance(created_at, int) and created_at > 0 else _iso_millis(updated_at)
        titled = f"{name}" + (f" “{title}”" if title and title != name else "")
        sentence = titled + f", {'closed' if state == 'closed' else 'open'}" + _about([thing for _id, thing in about])
        steps = [_step(name, "ABOUT", thing, ticket_id, thing_id, "Ticket", "Service") for thing_id, thing in about]
        things = [(thing_id, thing, "Service") for thing_id, thing in about]
        changes.append(Change("tickets", ticket_id, name, at, sentence, things, steps))
    return changes


# --- digest --------------------------------------------------------------------------------


def _digest(scope: Scope, changes: list[Change], one: str, many: str) -> str:
    """How many changes each thing had, most first: "Billing Service: 2 changes; Auth Service (1 hop up): 1"."""
    counts: dict[str, int] = {}
    names: dict[str, str] = {}
    for change in changes:
        for thing_id, name, label in change.things:
            if label != "Service" or (scope.mode != "company" and thing_id not in scope.walked):
                continue
            counts[thing_id] = counts.get(thing_id, 0) + 1
            hop = scope.walked[thing_id].hop if thing_id in scope.walked else 0
            names[thing_id] = f"{name} ({hop} hop{'s' if hop > 1 else ''} up)" if scope.mode == "service" and hop else name
    if len(counts) < 2 or len(changes) < 2:
        return ""
    hops = {thing_id: scope.walked[thing_id].hop if thing_id in scope.walked else 0 for thing_id in counts}
    ranked = sorted(counts, key=lambda thing_id: (-counts[thing_id], hops[thing_id], names[thing_id]))
    first, *others = ranked
    head = f"{names[first]}: {counts[first]} {one if counts[first] == 1 else many}"
    return "; ".join([head, *(f"{names[thing_id]}: {counts[thing_id]}" for thing_id in others)])


# --- citing --------------------------------------------------------------------------------


def _cite(result: ToolResult, scope: Scope, changes: list[Change]) -> None:
    """Each change's own edges, then how it reaches the scope: the team's RUNS, or the route upstream."""
    for change in changes:
        result.extend(change.steps)
        if change.memory is not None:
            result.memories.append(change.memory)
        for thing_id in sorted(change.service_ids & set(scope.walked)):
            thing = scope.walked[thing_id]
            if scope.mode == "team":
                result.add(_step(scope.entity.name, "RUNS", thing.name, scope.entity.id, thing.id, "Team", "Service"))
            elif scope.mode == "person":
                result.add(_step(scope.entity.name, scope.held[thing_id], thing.name, scope.entity.id, thing.id, "Person", "Service"))
            else:
                result.extend(
                    _step(thing.names[index], "DEPENDS_ON", thing.names[index + 1], thing.ids[index], thing.ids[index + 1], "Service", "Service")
                    for index in range(len(thing.ids) - 1)
                )


def _owners_line(graph, result: ToolResult, scope: Scope, changes: list[Change], scopes: list[str]) -> str:
    """Who to ask about each walked thing that changed, nearest first."""
    changed = [scope.walked[thing_id] for thing_id in {thing_id for change in changes for thing_id in change.service_ids} & set(scope.walked)]
    changed.sort(key=lambda thing: (thing.hop, thing.name))
    if not changed:
        return ""
    rows = graph.ro_query(
        f"MATCH (p:Person)-[:OWNS]->(s:Service) WHERE s.id IN $ids AND {SCOPED.format(var='p')} RETURN s.id, p.id, p.name ORDER BY p.name",
        {"ids": [thing.id for thing in changed], "scopes": scopes},
    ).result_set
    owners: dict[str, list[tuple[str, str]]] = {}
    for thing_id, person_id, person in rows:
        owners.setdefault(thing_id, []).append((person_id, person))
    # Each owner once, nearest thing first, with everything of theirs that changed.
    things_of: dict[str, list[str]] = {}
    for thing in changed:
        for person_id, person in owners.get(thing.id, []):
            things_of.setdefault(person, []).append(thing.name)
            result.add(_step(person, "OWNS", thing.name, person_id, thing.id, "Person", "Service"))
    named = [f"{person} ({join_names(names)})" for person, names in things_of.items()]
    return f"Owners to ask: {', '.join(named)}." if named else ""


# --- small helpers -------------------------------------------------------------------------


def _step(source: str, rel: str, target: str, source_id: str, target_id: str, source_label: str, target_label: str) -> PathStep:
    return PathStep(source, rel, target, from_id=source_id, to_id=target_id, from_label=source_label, to_label=target_label)


def _about(names: list[str]) -> str:
    return f" ({', '.join(names)})" if names else ""


def _clamp(days) -> int:
    try:
        return max(1, min(MAX_DAYS, int(days)))
    except (TypeError, ValueError):
        return DEFAULT_DAYS


def _days(days: int) -> str:
    return "day" if days == 1 else f"{days} days"


def _iso_millis(value) -> int | None:
    """GitHub's ISO time ("2026-10-17T09:30:00Z") as milliseconds; None when empty or unreadable."""
    if not value or not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def _ts_millis(ts) -> int | None:
    try:
        millis = int(float(ts) * 1000)
    except (TypeError, ValueError):
        return None
    return millis if millis > 0 else None


def _short_day(millis: int) -> str:
    moment = datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
    return f"{moment.day} {moment:%b}"


def _iso_day(millis: int | None) -> str:
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).strftime("%Y-%m-%d") if millis else ""


def _clip(text: str, size: int = 40) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= size else text[: size - 1] + "…"

