"""One ticket, as its tracker would show it: title, status, priority, who has it, what blocks it.

``ticket_status`` resolves one ticket by its handle ("PAY-12", "TKT-101", "app#5000") within the
asker's scopes and reads, every fact an edge or a property of the ticket:

- its title, state (open or closed), the tracker's own status and priority, and issue type;
- who it is assigned to and who opened it: a person, or a connector account named with its
  tool ("Om Sherikar (Jira)"); and who worked on it;
- what it is about (``ABOUT``);
- what blocks it and what it blocks (``BLOCKS`` points from the blocker to the blocked), each
  with its own state, so "what blocks PAY-11" is answered in the edge's true direction;
- the pull requests that reference or close it.

The lead sentence follows what was asked: "is PAY-12 fixed" opens with yes or not yet, "who is
working on PAY-11" with the people, "what blocks PAY-11" with its blockers. ``lead_line`` is the
one-sentence summary ``who_knows`` opens with when it answers about a ticket.
"""

import re
from dataclasses import dataclass, field

from src.db.falkordb_client import connect
from src.domain.knowledge import people, resolve
from src.domain.knowledge.dto import PathStep, ToolResult
from src.domain.knowledge.resolve import SCOPED, Entity, join_names

TOOL = "ticket_status"
MAX_LINKED = 5

# A ticket handle: a tracker key ("PAY-12", "TKT-101"), not a pull request ("PR-45").
HANDLE = re.compile(r"\b(?!pr-\d)[a-z][a-z0-9]{1,9}-\d+\b", re.IGNORECASE)
# What a question asks about one ticket.
_FIXED = re.compile(r"\b(?:fixed|resolved|done|closed|finished|shipped|merged|complete[d]?)\b", re.IGNORECASE)
_WHO = re.compile(r"\bwho(?:'s| is| are)? (?:working on|assigned(?: to)?|on|handling|has|owns|took|picked up)\b|\bassignee\b", re.IGNORECASE)
_BLOCKS = re.compile(r"\bblock(?:s|ed|ing|ers?)?\b|\bwaiting on\b|\bdepends on\b", re.IGNORECASE)
# The ticket as the blocker, not the blocked: "what does PAY-13 block", "is PAY-13 blocking anything",
# "what is PAY-13 blocking". "what blocks PAY-11" and "is PAY-11 blocked" ask the other way.
_BLOCKING = re.compile(r"\b(?:does|do|did|will)\s+\S+\s+block\b|\bblocking\b|\bblocks\s+(?:anything|what|any)\b", re.IGNORECASE)
# The question shapes routed here when they name a ticket handle.
ASKS = re.compile(
    r"\b(?:what|which) is\b|\bwhat'?s\b|\bstatus\b|\bstate of\b|\bpriority\b|\bprogress\b|\btell me about\b|\bdetails\b"
    r"|\bis \S+ (?:fixed|resolved|done|closed|finished|shipped|open|still open|blocked)\b|\bfixed\b|\bresolved\b"
    r"|\bwho(?:'s| is| are)? (?:working on|assigned|on|handling|has)\b|\bassignee\b|\bblock(?:s|ed|ing|ers?)?\b|\bwaiting on\b",
    re.IGNORECASE,
)


def asks_about_one_ticket(question: str) -> bool:
    """A ticket handle and a question about it: what it is, its status, who has it, what blocks it."""
    text = question or ""
    return len(set(match.lower() for match in HANDLE.findall(text))) == 1 and bool(ASKS.search(text))


@dataclass
class Ticket:
    id: str
    name: str
    title: str
    state: str
    status: str
    priority: str
    issue_type: str
    assigned: list[str] = field(default_factory=list)
    opened: list[str] = field(default_factory=list)
    worked: list[str] = field(default_factory=list)
    about: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    # Assigned, but only to someone outside the asker's scopes.
    assignee_hidden: bool = False


def ticket_status(question: str, scopes: list[str]) -> ToolResult:
    _, graph = connect()
    resolution = resolve.resolve_name(question, ("Ticket",), scopes, graph=graph)
    if not resolution.found:
        miss = ToolResult(tool=TOOL, answer=resolve.describe_miss(resolution, "ticket"), found=False, candidates=resolution.candidate_names())
        return _not_a_ticket(question, scopes, miss)
    result = ToolResult(tool=TOOL, answer="", candidates=resolution.candidate_names())
    ticket = read(graph, resolution.entity, scopes, result)
    result.answer = _answer(question, ticket)
    return result


def _not_a_ticket(question: str, scopes: list[str], miss: ToolResult) -> ToolResult:
    """What a word-number token ("q3-2026", "utf-8", "gpt-4") that names no readable ticket was really
    asking: the route the question takes without the single-ticket rule, else the refusal fallback."""
    from src.domain.knowledge import ask_service, fallback  # ask_service imports this module

    tool, args = ask_service.route(question, tickets=False)
    if tool not in ("none", TOOL):
        return ask_service._ROUTED[tool](args, scopes)
    return fallback.before_refusing(question, scopes, miss)


def read(graph, target: Entity, scopes: list[str], result: ToolResult) -> Ticket:
    """The ticket's properties and edges, each edge cited on ``result``."""
    rows = graph.ro_query(
        """
        MATCH (t:Ticket {id: $id})
        RETURN t.name, coalesce(t.title, ''), coalesce(t.state, 'open'), coalesce(t.status, ''), coalesce(t.priority, ''), coalesce(t.issue_type, '')
        """,
        {"id": target.id},
    ).result_set
    name, title, state, status, priority, issue_type = rows[0] if rows else (target.name, "", "open", "", "", "")
    ticket = Ticket(target.id, name, title, "closed" if str(state).lower() == "closed" else "open", status, priority, issue_type)

    def cite(from_id, from_name, from_label, rel, to_id, to_name, to_label):
        if len(result.path) < 40:
            result.add(PathStep(from_name, rel, to_name, from_id=from_id, to_id=to_id, from_label=from_label, to_label=to_label))

    for rel, who_id, who, who_label, source in graph.ro_query(
        f"""
        MATCH (who)-[r:ASSIGNED|OPENED|WORKED_ON]->(:Ticket {{id: $id}})
        WHERE labels(who)[0] IN ['Person', 'GitHubUser', 'ExternalUser'] AND {SCOPED.format(var="who")}
        OPTIONAL MATCH (who)-[:IS]->(person:Person)
        WHERE {SCOPED.format(var="person")}
        RETURN type(r), coalesce(person.id, who.id), coalesce(person.name, CASE WHEN labels(who)[0] = 'GitHubUser' THEN '@' + who.login ELSE who.name END),
               CASE WHEN person IS NOT NULL THEN 'Person' ELSE labels(who)[0] END, who.source
        ORDER BY type(r), coalesce(person.name, who.name)
        """,
        {"id": target.id, "scopes": scopes},
    ).result_set:
        shown = people.account_label(who, source) if who_label == people.ACCOUNT else who
        bucket = {"ASSIGNED": ticket.assigned, "OPENED": ticket.opened, "WORKED_ON": ticket.worked}[rel]
        if shown not in bucket:
            bucket.append(shown)
            cite(who_id, shown, who_label, rel, target.id, name, "Ticket")

    if not ticket.assigned:
        # Assigned to someone the asker cannot read: said so, never "no one".
        hidden = graph.ro_query("MATCH (who)-[:ASSIGNED]->(:Ticket {id: $id}) RETURN count(who)", {"id": target.id}).result_set
        ticket.assignee_hidden = bool(hidden and hidden[0][0])

    for service_id, service in graph.ro_query(
        f"MATCH (:Ticket {{id: $id}})-[:ABOUT]->(s:Service) WHERE {SCOPED.format(var='s')} RETURN s.id, s.name ORDER BY s.name",
        {"id": target.id, "scopes": scopes},
    ).result_set:
        ticket.about.append(service)
        cite(target.id, name, "Ticket", "ABOUT", service_id, service, "Service")

    # BLOCKS points from the blocker to the blocked: read each way, never the same words for both.
    for blocker, other_id, other, other_title, other_state in graph.ro_query(
        f"""
        MATCH (t:Ticket {{id: $id}})-[r:BLOCKS]-(other:Ticket)
        WHERE {SCOPED.format(var="other")}
        RETURN startNode(r) = other, other.id, other.name, coalesce(other.title, ''), coalesce(other.state, 'open')
        ORDER BY other.name
        """,
        {"id": target.id, "scopes": scopes},
    ).result_set:
        described = _linked(other, other_title, other_state)
        if blocker:
            ticket.blocked_by.append(described)
            cite(other_id, other, "Ticket", "BLOCKS", target.id, name, "Ticket")
        else:
            ticket.blocks.append(described)
            cite(target.id, name, "Ticket", "BLOCKS", other_id, other, "Ticket")

    for rel, pr_id, pr, pr_state, merged in graph.ro_query(
        f"""
        MATCH (pr:PullRequest)-[r:REFERENCES|CLOSES]->(:Ticket {{id: $id}})
        WHERE {SCOPED.format(var="pr")}
        RETURN type(r), pr.id, pr.name, coalesce(pr.state, 'open'), coalesce(pr.merged, false)
        ORDER BY pr.name
        """,
        {"id": target.id, "scopes": scopes},
    ).result_set:
        ticket.references.append(f"{pr} ({'merged' if merged else pr_state}{', closes it' if rel == 'CLOSES' else ''})")
        cite(pr_id, pr, "PullRequest", rel, target.id, name, "Ticket")
    return ticket


def _linked(name: str, title: str, state: str) -> str:
    closed = str(state).lower() == "closed"
    return f"{name} (“{title}”, {'closed' if closed else 'open'})" if title and title != name else f"{name} ({'closed' if closed else 'open'})"


def lead_line(ticket: Ticket) -> str:
    """"PAY-12 is “Billing exports time out”: open (In Progress), priority High, assigned to Om Sherikar (Jira)."."""
    titled = f" is “{ticket.title}”" if ticket.title and ticket.title != ticket.name else ""
    state = ticket.state + (f" ({ticket.status})" if ticket.status and ticket.status.lower() != ticket.state else "")
    details = [state]
    if ticket.priority:
        details.append(f"priority {ticket.priority}")
    if ticket.assigned:
        details.append(f"assigned to {join_names(ticket.assigned)}")
    else:
        details.append("the assignee isn't visible to you" if ticket.assignee_hidden else "assigned to no one")
    return f"{ticket.name}{titled}: {', '.join(details)}." if titled else f"{ticket.name} is {', '.join(details)}."


def _answer(question: str, ticket: Ticket) -> str:
    parts = []
    if _FIXED.search(question) and not _BLOCKS.search(question):
        done = ticket.state == "closed"
        status = f" ({ticket.status})" if ticket.status else ""
        parts.append(f"Yes: {ticket.name} is closed{status}." if done else f"Not yet: {ticket.name} is still open{status}.")
    elif _BLOCKING.search(question):
        # The ticket as the blocker: "what does PAY-13 block", "is PAY-13 blocking anything".
        if ticket.blocks:
            parts.append(f"{ticket.name} blocks {_some(ticket.blocks)}.")
        else:
            parts.append(f"{ticket.name} blocks nothing you can read.")
    elif _BLOCKS.search(question):
        if ticket.blocked_by:
            parts.append(f"{ticket.name} is blocked by {_some(ticket.blocked_by)}.")
        else:
            parts.append(f"Nothing you can read blocks {ticket.name}.")
    elif _WHO.search(question):
        if ticket.assigned or ticket.worked:
            people_on = ticket.assigned + [who for who in ticket.worked if who not in ticket.assigned]
            parts.append(f"{join_names(people_on)} {'is' if len(people_on) == 1 else 'are'} on {ticket.name}.")
        else:
            parts.append(f"No one is assigned {ticket.name}.")
    parts.append(lead_line(ticket))
    if ticket.issue_type:
        parts.append(f"Type: {ticket.issue_type}.")
    if ticket.opened:
        parts.append(f"Opened by {join_names(ticket.opened)}.")
    worked = [who for who in ticket.worked if who not in ticket.assigned]
    if worked:
        parts.append(f"Worked on by {join_names(worked)}.")
    if ticket.about:
        parts.append(f"About {join_names(ticket.about)}.")
    if ticket.blocked_by and not _BLOCKS.search(question):
        parts.append(f"Blocked by {_some(ticket.blocked_by)}.")
    if ticket.blocks and not _BLOCKING.search(question):
        parts.append(f"It blocks {_some(ticket.blocks)}.")
    if ticket.blocked_by and _BLOCKING.search(question):
        parts.append(f"Blocked by {_some(ticket.blocked_by)}.")
    if ticket.references:
        parts.append(f"Pull requests: {_some(ticket.references)}.")
    return " ".join(parts)


def _some(names: list[str]) -> str:
    shown = names[:MAX_LINKED]
    if len(names) > MAX_LINKED:
        shown = [*shown, f"{len(names) - MAX_LINKED} more"]
    return join_names(shown)
