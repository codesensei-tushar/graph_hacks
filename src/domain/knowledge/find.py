"""Lists by filter: open tickets, a team's services, the standing decisions about a service.

``find(label, filters)`` lists the nodes of one label that pass every filter given. The
label comes from an enum, and each label takes a fixed set of filters. A filter that names
a thing (a team, a service, a person) is resolved to a node id before anything is listed,
and refused with the nearest names when nothing matches; a filter that picks a state or a
kind is checked against its enum. No filter value ever reaches a query as text: each filter
is one hand-written query below that takes the resolved id as ``$value``, and the list is
the intersection of what each filter returns, so no WHERE clause is assembled at all.

Every node listed, and every node a filter walks through, passes the scope clause. The
reply cites the edges that satisfy each filter ("Arjun Mehta -[OWNS]-> Billing Service"),
or, when no filter walks an edge, the edge that places each one (a ticket's service, a
person's team), so the page can draw the list. Up to ``MAX_ROWS`` are named, decisions
newest first and everything else by name.
"""

import re
from dataclasses import dataclass

from src.db.falkordb_client import connect
from src.domain.knowledge import kinds, people, resolve
from src.domain.knowledge.dto import PathStep, ToolResult
from src.domain.knowledge.resolve import SCOPED, Entity, join_names

TOOL = "find"
LABELS = ("Service", "Ticket", "Decision", "PullRequest", "Document", "Person", "Team")
TICKET_STATES = ("open", "closed")
PULL_REQUEST_STATES = ("open", "closed", "merged")
MAX_ROWS = 25
PATH_CAP = 50


@dataclass(frozen=True)
class Filter:
    name: str
    # The label a value resolves to, or None for an enum or a yes/no.
    target: str | None = None
    # Returns (x.id, [[from_id, from_name, from_label, rel, to_id, to_name, to_label], ...]) for each x that passes.
    query: str | None = None
    # How the reply qualifies the list; {value} is the thing named.
    words: str = ""


def _q(text: str) -> str:
    return text.replace("{scoped_x}", SCOPED.format(var="x")).replace("{scoped_s}", SCOPED.format(var="s"))


_EDGE_X_S = "[x.id, x.name, labels(x)[0], type(r), s.id, s.name, 'Service']"
_EDGE_T_S = "[t.id, t.name, 'Team', 'RUNS', s.id, s.name, 'Service']"
_EDGE_P_X = "[p.id, p.name, 'Person', type(r), x.id, x.name, labels(x)[0]]"
# A person named through their GitHub account: $accounts holds the person's id and every account linked to them.
_EDGE_WHO_X = "[$value, $value_name, $value_label, type(r), x.id, x.name, labels(x)[0]]"

# Each label's filters, each with its one query. ``x`` is what is listed.
FILTERS: dict[str, dict[str, Filter]] = {
    "Service": {
        "kind": Filter("kind"),
        "team": Filter("team", "Team", _q("MATCH (t:Team {id: $value})-[r:RUNS]->(x:Service) WHERE {scoped_x} RETURN x.id, [[t.id, t.name, 'Team', type(r), x.id, x.name, 'Service']]"), "run by the {value} team"),
        "owner": Filter("owner", "Person", _q(f"MATCH (a)-[r:OWNS]->(x:Service) WHERE a.id IN $accounts AND {{scoped_x}} RETURN x.id, [{_EDGE_WHO_X}]"), "owned by {value}"),
    },
    "Ticket": {
        "state": Filter("state"),
        "about": Filter("about", "Service", _q(f"MATCH (x:Ticket)-[r:ABOUT]->(s:Service {{id: $value}}) WHERE {{scoped_x}} RETURN x.id, [{_EDGE_X_S}]"), "about {value}"),
        "team": Filter(
            "team", "Team",
            _q(f"MATCH (x:Ticket)-[r:ABOUT]->(s:Service)<-[:RUNS]-(t:Team {{id: $value}}) WHERE {{scoped_x}} AND {{scoped_s}} RETURN x.id, [{_EDGE_X_S}, {_EDGE_T_S}]"),
            "about what the {value} team runs",
        ),
        "person": Filter("person", "Person", _q(f"MATCH (a)-[r:WORKED_ON|ASSIGNED]->(x:Ticket) WHERE a.id IN $accounts AND {{scoped_x}} RETURN x.id, [{_EDGE_WHO_X}]"), "worked on by or assigned to {value}"),
        "blocked": Filter("blocked"),
    },
    "Decision": {
        "about": Filter("about", "Service", _q(f"MATCH (x:Decision)-[r:ABOUT]->(s:Service {{id: $value}}) WHERE {{scoped_x}} RETURN x.id, [{_EDGE_X_S}]"), "about {value}"),
        "team": Filter(
            "team", "Team",
            _q(f"MATCH (x:Decision)-[r:ABOUT]->(s:Service)<-[:RUNS]-(t:Team {{id: $value}}) WHERE {{scoped_x}} AND {{scoped_s}} RETURN x.id, [{_EDGE_X_S}, {_EDGE_T_S}]"),
            "about what the {value} team runs",
        ),
        "standing": Filter("standing"),
        "proposer": Filter("proposer", "Person", _q(f"MATCH (p:Person {{id: $value}})-[r:PROPOSED]->(x:Decision) WHERE {{scoped_x}} RETURN x.id, [{_EDGE_P_X}]"), "proposed by {value}"),
        "approver": Filter("approver", "Person", _q(f"MATCH (p:Person {{id: $value}})-[r:APPROVED]->(x:Decision) WHERE {{scoped_x}} RETURN x.id, [{_EDGE_P_X}]"), "approved by {value}"),
    },
    "PullRequest": {
        "state": Filter("state"),
        "touches": Filter("touches", "Service", _q(f"MATCH (x:PullRequest)-[r:TOUCHES]->(s:Service {{id: $value}}) WHERE {{scoped_x}} RETURN x.id, [{_EDGE_X_S}]"), "touching {value}"),
        "author": Filter("author", "Person", _q(f"MATCH (a:GitHubUser)-[r:AUTHORED]->(x:PullRequest) WHERE a.id IN $accounts AND {{scoped_x}} RETURN x.id, [{_EDGE_WHO_X}]"), "by {value}"),
        "reviewer": Filter("reviewer", "Person", _q(f"MATCH (a:GitHubUser)-[r:REVIEWED]->(x:PullRequest) WHERE a.id IN $accounts AND {{scoped_x}} RETURN x.id, [{_EDGE_WHO_X}]"), "reviewed by {value}"),
    },
    "Document": {
        "describes": Filter("describes", "Service", _q(f"MATCH (x:Document)-[r:DESCRIBES]->(s:Service {{id: $value}}) WHERE {{scoped_x}} RETURN x.id, [{_EDGE_X_S}]"), "describing {value}"),
        "author": Filter("author", "Person", _q(f"MATCH (p:Person {{id: $value}})-[r:AUTHORED]->(x:Document) WHERE {{scoped_x}} RETURN x.id, [{_EDGE_P_X}]"), "written by {value}"),
        "team": Filter(
            "team", "Team",
            _q(f"MATCH (x:Document)-[r:DESCRIBES]->(s:Service)<-[:RUNS]-(t:Team {{id: $value}}) WHERE {{scoped_x}} AND {{scoped_s}} RETURN x.id, [{_EDGE_X_S}, {_EDGE_T_S}]"),
            "describing what the {value} team runs",
        ),
    },
    "Person": {
        "team": Filter("team", "Team", _q("MATCH (x:Person)-[r:MEMBER_OF]->(t:Team {id: $value}) WHERE {scoped_x} RETURN x.id, [[x.id, x.name, 'Person', type(r), t.id, t.name, 'Team']]"), "on the {value} team"),
        "holds": Filter(
            "holds", "Service",
            _q(f"MATCH (x:Person)-[r:OWNS|WORKED_ON|KNOWS]->(s:Service {{id: $value}}) WHERE {{scoped_x}} RETURN x.id, [{_EDGE_X_S}]"),
            "who own, worked on or know {value}",
        ),
    },
    "Team": {},
}
FILTER_NAMES = tuple(dict.fromkeys(name for filters in FILTERS.values() for name in filters))

# The edge that places each listed node, cited when no filter walks one.
CONTEXT = {
    "Service": _q("MATCH (t:Team)-[r:RUNS]->(x:Service) WHERE x.id IN $ids RETURN x.id, [[t.id, t.name, 'Team', type(r), x.id, x.name, 'Service']]"),
    "Ticket": _q(f"MATCH (x:Ticket)-[r:ABOUT]->(s:Service) WHERE x.id IN $ids AND {{scoped_s}} RETURN x.id, [{_EDGE_X_S}]"),
    "Decision": _q(f"MATCH (x:Decision)-[r:ABOUT]->(s:Service) WHERE x.id IN $ids AND {{scoped_s}} RETURN x.id, [{_EDGE_X_S}]"),
    "PullRequest": _q(f"MATCH (x:PullRequest)-[r:TOUCHES]->(s:Service) WHERE x.id IN $ids AND {{scoped_s}} RETURN x.id, [{_EDGE_X_S}]"),
    "Document": _q(f"MATCH (x:Document)-[r:DESCRIBES]->(s:Service) WHERE x.id IN $ids AND {{scoped_s}} RETURN x.id, [{_EDGE_X_S}]"),
    "Person": _q("MATCH (x:Person)-[r:MEMBER_OF]->(t:Team) WHERE x.id IN $ids RETURN x.id, [[x.id, x.name, 'Person', type(r), t.id, t.name, 'Team']]"),
    "Team": _q("MATCH (x:Team)-[r:RUNS]->(s:Service) WHERE x.id IN $ids AND {scoped_s} RETURN x.id, [[x.id, x.name, 'Team', type(r), s.id, s.name, 'Service']]"),
}
_ROWS = _q(
    """
    MATCH (x)
    WHERE labels(x)[0] = $label AND x.name IS NOT NULL AND {scoped_x}
    RETURN x.id, x.name, coalesce(x.title, ''), x.kind, x.state, x.merged, x.decided_at, coalesce(x.role, '')
    """
)
# The open tickets that block each of $ids, as cited edges.
_BLOCKERS = _q(
    """
    MATCH (b:Ticket)-[r:BLOCKS]->(x:Ticket)
    WHERE x.id IN $ids AND coalesce(b.scope, 'org_shared') IN $scopes AND coalesce(b.state, 'open') <> 'closed'
    RETURN x.id, collect([b.id, b.name, 'Ticket', type(r), x.id, x.name, 'Ticket'])
    """
)
_ACCOUNTS_OF ="MATCH (a)-[:IS]->(p:Person {id: $id}) RETURN a.id"

_NOUNS = {
    "Service": ("service", "services"), "Ticket": ("ticket", "tickets"), "Decision": ("decision", "decisions"),
    "PullRequest": ("pull request", "pull requests"), "Document": ("document", "documents"),
    "Person": ("person", "people"), "Team": ("team", "teams"),
}
_KIND_PLURALS = {"process": "processes", "policy": "policies"}
_TRUE = frozenset({"true", "yes", "1", "standing", "current"})
_FALSE = frozenset({"false", "no", "0", "replaced", "superseded"})


@dataclass(frozen=True)
class Row:
    id: str
    name: str
    title: str
    kind: str | None
    state: str
    decided_at: int | None
    role: str


def find(label, filters: dict, scopes: list[str]) -> ToolResult:
    """The nodes of ``label`` that pass every filter in ``filters``, cited by the edges that make them pass."""
    filters = {key: value for key, value in (filters or {}).items() if value is not None and value != ""}
    chosen = _label(label)
    if chosen is None:
        # A tool named outright gets the question in its first argument: read the list it asks for from it.
        parsed = parse(str(label or "")) if label else None
        if not parsed:
            return ToolResult(tool=TOOL, answer=f"find lists one of {join_names(list(LABELS))}; “{label}” is not one of them.", found=False)
        chosen = parsed.pop("label")
        filters = {**parsed, **filters}
    allowed = FILTERS[chosen]
    unknown = [key for key in filters if key not in allowed]
    if unknown:
        plural = _NOUNS[chosen][1].capitalize()
        takes = f"can be filtered by {join_names(list(allowed))}" if allowed else "take no filters"
        return ToolResult(tool=TOOL, answer=f"{plural} {takes}; not by {join_names(unknown)}.", found=False)

    enums: dict[str, object] = {}
    for key, value in filters.items():
        if allowed[key].target is None:
            checked = _enum(chosen, key, value)
            if isinstance(checked, ToolResult):
                return checked
            enums[key] = checked

    _, graph = connect()
    named: dict[str, Entity] = {}
    candidates: list[str] = []
    for key, value in filters.items():
        spec = allowed[key]
        if spec.target is None:
            continue
        if spec.target == "Person":
            # A person, or someone a connected tool knows (Jira, HubSpot), within the asker's scopes.
            resolution = people.resolve_person(str(value), scopes, graph)
        else:
            resolution = resolve.resolve_name(str(value), (spec.target,), scopes, graph=graph)
        if not resolution.found:
            what = {"Service": "service", "Team": "team", "Person": "person"}[spec.target]
            return ToolResult(tool=TOOL, answer=resolve.describe_miss(resolution, what), found=False, candidates=resolution.candidate_names())
        named[key] = resolution.entity
        candidates.extend(name for name in resolution.candidate_names() if name not in candidates)

    rows = {row[0]: Row(row[0], row[1], row[2], row[3], _state(row[4], row[5]), row[6], row[7]) for row in graph.ro_query(_ROWS, {"label": chosen, "scopes": scopes}).result_set}
    edges: dict[str, list[list]] = {}
    for key, entity in named.items():
        passing: dict[str, list[list]] = {}
        params = {
            "value": entity.id, "value_name": entity.name, "value_label": entity.label, "scopes": scopes,
            "accounts": _accounts(graph, entity) if entity.label in ("Person", people.ACCOUNT) else [],
        }
        for node_id, cited in graph.ro_query(allowed[key].query, params).result_set:
            passing.setdefault(node_id, []).extend(cited)
        rows = {node_id: row for node_id, row in rows.items() if node_id in passing}
        for node_id, cited in passing.items():
            edges.setdefault(node_id, []).extend(cited)
    rows = _apply_enums(graph, chosen, rows, enums, scopes)
    if "blocked" in enums:
        # Blocked: an open ticket the asker can read BLOCKS it. Each blocker's edge is cited.
        blockers = {node_id: cited for node_id, cited in graph.ro_query(_BLOCKERS, {"ids": list(rows), "scopes": scopes}).result_set}
        rows = {node_id: row for node_id, row in rows.items() if (node_id in blockers) == enums["blocked"]}
        for node_id, cited in blockers.items():
            if node_id in rows:
                edges.setdefault(node_id, []).extend(cited)

    ordered = sorted(rows.values(), key=lambda row: (-(row.decided_at or 0), row.name.lower()) if chosen == "Decision" else (row.name.lower(),))
    listed = ordered[:MAX_ROWS]
    result = ToolResult(tool=TOOL, answer="", candidates=candidates)
    if not named and listed:
        for node_id, cited in graph.ro_query(CONTEXT[chosen], {"ids": [row.id for row in listed], "scopes": scopes}).result_set:
            edges.setdefault(node_id, []).extend(cited)
    for row in listed:
        for from_id, from_name, from_label, rel, to_id, to_name, to_label in edges.get(row.id, []):
            if len(result.path) < PATH_CAP:
                result.add(PathStep(from_name, rel, to_name, from_id=from_id, to_id=to_id, from_label=from_label, to_label=to_label))

    replaced = _replaced(graph, [row.id for row in listed], scopes) if chosen == "Decision" else set()
    qualified = ", ".join(allowed[key].words.format(value=_named(entity)) for key, entity in named.items())
    described = " ".join(part for part in (_noun(chosen, enums, len(ordered)), qualified) if part)
    hints = "".join(f" {people.unlinked_hint(entity)}" for entity in named.values() if people.is_account(entity))
    if not ordered:
        result.answer = f"No {described}.{hints}"
        return result
    # "3 accounts: Acme and Initech": an account is a company, and "Acme account" reads as something else.
    items = [row.name if enums.get("kind") == "account" else _item(chosen, row, row.id in replaced) for row in listed]
    if len(ordered) > len(listed):
        items.append(f"{len(ordered) - len(listed)} more")
    result.answer = f"{len(ordered)} {described}: {join_names(items)}.{hints}"
    return result


# --- arguments -----------------------------------------------------------------------------


def _label(value) -> str | None:
    text = str(value or "").strip().replace(" ", "").lower()
    return next((label for label in LABELS if label.lower() == text or _NOUNS[label][1].replace(" ", "") == text), None)


def _enum(label: str, key: str, value):
    if key == "kind":
        kind = str(value).strip().lower()
        if kind not in kinds.SERVICE_KINDS:
            return ToolResult(tool=TOOL, answer=f"“{value}” is not a kind. A service's kind is one of {join_names(list(kinds.SERVICE_KINDS))}.", found=False)
        return kind
    if key == "state":
        states = PULL_REQUEST_STATES if label == "PullRequest" else TICKET_STATES
        state = str(value).strip().lower()
        if state not in states:
            noun = _NOUNS[label][0]
            return ToolResult(tool=TOOL, answer=f"“{value}” is not a state. A {noun} is {join_names(list(states)).replace(' and ', ' or ')}.", found=False)
        return state
    # standing or blocked: a yes or no, from the model as a boolean or as text.
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE or text in _FALSE:
        return text in _TRUE
    if key == "blocked":
        return ToolResult(tool=TOOL, answer=f"blocked is true (tickets an open ticket blocks) or false (the rest); “{value}” is neither.", found=False)
    return ToolResult(tool=TOOL, answer=f"standing is true (the decisions that stand today) or false (the ones replaced); “{value}” is neither.", found=False)


def _state(state, merged) -> str:
    if merged:
        return "merged"
    text = str(state or "").strip().lower()
    return "closed" if text == "closed" else "open"


def _accounts(graph, person: Entity) -> list[str]:
    return [person.id, *(row[0] for row in graph.ro_query(_ACCOUNTS_OF, {"id": person.id}).result_set)]


def _apply_enums(graph, label: str, rows: dict[str, Row], enums: dict, scopes: list[str]) -> dict[str, Row]:
    if "kind" in enums:
        rows = {node_id: row for node_id, row in rows.items() if kinds.kind_or_default(row.kind) == enums["kind"]}
    if "state" in enums:
        rows = {node_id: row for node_id, row in rows.items() if row.state == enums["state"]}
    if "standing" in enums:
        replaced = _replaced(graph, list(rows), scopes)
        rows = {node_id: row for node_id, row in rows.items() if (node_id not in replaced) == enums["standing"]}
    return rows


def _replaced(graph, decision_ids: list[str], scopes: list[str]) -> set[str]:
    from src.domain.knowledge import explore

    return explore._replaced(graph, decision_ids, scopes)


# --- wording -------------------------------------------------------------------------------


def _noun(label: str, enums: dict, count: int) -> str:
    singular, plural = _NOUNS[label]
    if "kind" in enums:
        singular = enums["kind"]
        plural = _KIND_PLURALS.get(singular, singular + "s")
    word = singular if count == 1 else plural
    adjectives = []
    if "state" in enums:
        adjectives.append(enums["state"])
    if "standing" in enums:
        adjectives.append("standing" if enums["standing"] else "replaced")
    if "blocked" in enums:
        adjectives.append("blocked" if enums["blocked"] else "unblocked")
    return " ".join([*adjectives, word])


def _named(entity: Entity) -> str:
    if entity.label == "Service":
        return kinds.subject(entity.name, entity.kind)
    return entity.name


def _item(label: str, row: Row, replaced: bool) -> str:
    if label == "Service":
        return kinds.listed(row.name, row.kind)
    if label in ("Ticket", "PullRequest"):
        return f"{row.name} ({row.title})" if row.title and row.title != row.name else row.name
    if label == "Decision":
        from src.domain.knowledge import decisions

        when = decisions.format_date(decisions.to_millis(row.decided_at)) if row.decided_at else ""
        notes = ", ".join(note for note in (when, "replaced" if replaced else "") if note)
        return f"*{row.name}*" + (f" ({notes})" if notes else "")
    if label == "Person":
        return f"{row.name} ({row.role})" if row.role else row.name
    return row.name


# --- the keyword path ----------------------------------------------------------------------

# The list a question asks for, from its noun. A kind word lists services of that kind.
_LIST_NOUNS = (
    (re.compile(r"\bdecisions?\b", re.IGNORECASE), "Decision", None),
    (re.compile(r"\b(?:tickets?|issues?)\b", re.IGNORECASE), "Ticket", None),
    (re.compile(r"\b(?:pull requests?|prs?)\b", re.IGNORECASE), "PullRequest", None),
    (re.compile(r"\b(?:documents?|docs?|runbooks?)\b", re.IGNORECASE), "Document", None),
    (re.compile(r"\bpeople\b", re.IGNORECASE), "Person", None),
    (re.compile(r"\bteams\b", re.IGNORECASE), "Team", None),
    (re.compile(r"\baccounts\b", re.IGNORECASE), "Service", "account"),
    (re.compile(r"\bservices\b", re.IGNORECASE), "Service", None),
    (re.compile(r"\bsystems\b", re.IGNORECASE), "Service", "system"),
    (re.compile(r"\bprocesses\b", re.IGNORECASE), "Service", "process"),
    (re.compile(r"\bplaybooks\b", re.IGNORECASE), "Service", "playbook"),
)
_STATE = re.compile(r"\b(open|closed|merged)\b", re.IGNORECASE)
_STANDING = re.compile(r"\b(?:standing|current)\b", re.IGNORECASE)
_BLOCKED = re.compile(r"\bblocked\b", re.IGNORECASE)
_ABOUT = re.compile(r"\b(?:about|for|on|touching|describing)\s+(.+?)\s*\??\s*$", re.IGNORECASE)
_IN_TEAM = re.compile(r"\bin\s+(?:the\s+)?(.+?)(?:\s+team)?\s*\??\s*$", re.IGNORECASE)
# Whose list it is: "tickets assigned to Om Sherikar", "accounts owned by Yash", "which accounts does Yash own".
_BY_PERSON = re.compile(r"\b(?:assigned to|worked on by|owned by|opened by|held by)\s+(.+?)\s*\??\s*$", re.IGNORECASE)
_DOES_PERSON = re.compile(r"\b(?:does|do)\s+(.+?)\s+(?:own|have|hold|handle|work on)\b", re.IGNORECASE)
# The filter a person fills, per label.
_PERSON_FILTER = {"Ticket": "person", "Service": "owner", "Document": "author", "PullRequest": "author"}
# The filter "about X" fills, per label. Services and teams have none.
_ABOUT_FILTER = {"Ticket": "about", "Decision": "about", "PullRequest": "touches", "Document": "describes", "Person": "holds"}


def parse(question: str) -> dict:
    """The label and filters a list question asks for ("which tickets are open for billing"), or {}."""
    text = (question or "").strip()
    hits = [(match.start(), label, kind) for pattern, label, kind in _LIST_NOUNS if (match := pattern.search(text))]
    if not hits:
        return {}
    _start, label, kind = min(hits)
    args: dict = {"label": label}
    allowed = FILTERS[label]
    if kind:
        args["kind"] = kind
    if (state := _STATE.search(text)) and "state" in allowed:
        value = state.group(1).lower()
        if value in (PULL_REQUEST_STATES if label == "PullRequest" else TICKET_STATES):
            args["state"] = value
    if "standing" in allowed and _STANDING.search(text):
        args["standing"] = True
    if "blocked" in allowed and _BLOCKED.search(text):
        args["blocked"] = True
    who = _BY_PERSON.search(text) or _DOES_PERSON.search(text)
    person_filter = _PERSON_FILTER.get(label)
    if who and re.search(r"\bteam$", who.group(1), re.IGNORECASE) and "team" in allowed:
        # "which services does the payments team own": the team, not a person.
        args["team"] = who.group(1)
    elif who and person_filter in allowed:
        args[person_filter] = who.group(1)
    elif (about := _ABOUT.search(text)) and label in _ABOUT_FILTER:
        args[_ABOUT_FILTER[label]] = about.group(1)
    elif (team := _IN_TEAM.search(text)) and "team" in allowed:
        args["team"] = team.group(1)
    return args
