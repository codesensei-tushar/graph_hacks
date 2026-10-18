"""One pull request, explained: what it did, who did it, what it affects, who reviewed it, what it closes.

``pr_explain`` resolves a pull request by number ("#207", "PR 207", "PR-207") or with its repo
("graph_hacks#207", "codesensei-tushar/graph_hacks#207") among the pull requests the asker can
read, exactly as ``who_should_know`` does (``coordination.find_pull_request``), and reads, every
fact a property or an edge GitHub ingest wrote:

- its title, state (open, merged or closed without merging), when it merged, and a gist of its
  description (the first prose lines, without size markers, tables or headings);
- who wrote it (``AUTHORED``), who reviewed it and how (``REVIEWED {state}``: approved, commented,
  asked for changes) and who is still asked to (``REVIEW_REQUESTED``), each named as the person
  their GitHub account ``IS`` when linked, else ``@login``;
- the services it ``TOUCHES`` (from the files it changed, matched to ``Service.paths``) and, one
  hop out, the services that ``DEPENDS_ON`` those: its blast radius;
- the tickets it ``CLOSES`` or ``REFERENCES``, and the decisions ``BASED_ON`` it.

Everything is filtered by the asker's scopes, so a private repo's pull request is refused like
one that does not exist. The lead sentence follows what was asked: "who did #216" opens with the
author, "who reviewed" with the reviews, "what does it affect" with the blast radius, "what does
it close" with the tickets. A number that is a GitHub issue, not a pull request, is answered as the
ticket it is (``ticket_status``).
"""

import re
from dataclasses import dataclass, field
from datetime import datetime

from src.db.falkordb_client import connect
from src.domain.knowledge import coordination
from src.domain.knowledge.dto import PathStep, ToolResult
from src.domain.knowledge.resolve import SCOPED, Entity, join_names

TOOL = "pr_explain"
GIST_CHARS = 280
MAX_LISTED = 6

# What a question asks about one pull request, once it names one ("what did PR 207 do").
ASKS = re.compile(
    r"\bwhat (?:did|does|do|will|would|is|was)\b|\bwhat'?s\b|\bexplain\b|\bsummar(?:y|ise|ize)\b|\btell me about\b|\bdetails?\b"
    r"|\bdescribe\b|\bstatus\b|\bstate of\b|\bmerged\b|\bwhen did\b|\bwhat happened\b"
    r"|\bwho (?:did|made|wrote|authored|opened|created|built|shipped|raised|reviewed|approved|merged)\b|\bwho(?:'s| is) the (?:author|reviewer)s?\b"
    r"|\b(?:authors?|reviewers?|reviews?)\b"
    r"|\b(?:affects?|affected|touch(?:es|ed)?|changes?|changed|impacts?|impacted|blast radius)\b"
    r"|\b(?:close[sd]?|fix(?:es)?|resolves?|tickets?|issues?)\b",
    re.IGNORECASE,
)
# A question that is only a pull request: "PR #207", "graph_hacks#207", "#207?".
_ONLY = re.compile(r"^\s*(?:(?:pr|pull request)[\s-]*#?\s*\d+|(?:[\w.-]+/)?[\w.-]*#\d+)\s*[?.!]*\s*$", re.IGNORECASE)

_WHO_WROTE = re.compile(r"\bwho (?:did|made|wrote|authored|opened|created|built|shipped|raised)\b|\bauthors?\b|\bwho(?:'s| is) the author\b", re.IGNORECASE)
_WHO_REVIEWED = re.compile(r"\bwho (?:reviewed|approved)\b|\breviewers?\b|\breviews?\b|\breviewed\b", re.IGNORECASE)
_AFFECTS = re.compile(r"\b(?:affects?|affected|touch(?:es|ed)?|impacts?|impacted|blast radius|break)\b|\bwhat (?:does|did) .+ change\b", re.IGNORECASE)
_TICKETS = re.compile(r"\b(?:close[sd]?|fix(?:es)?|resolves?|tickets?|issues?)\b", re.IGNORECASE)
# Lines of a description that are not its prose: tables, code fences, comments, checklists, footers.
_NOT_PROSE = re.compile(r"^\s*(?:\||```|<!--|[-*]\s*\[[ x]\]|🤖|co-authored-by)", re.IGNORECASE)
# "**Size: LARGE** (the connector write path ...)", "**LARGE**: auth, sessions ...", "SMALL (deploy, docs)".
_SIZE = re.compile(r"^\s*\**\s*(?:size\s*:?\s*)?\**\s*(?:small|medium|large)\b\**\s*[:.\-–—]?\s*", re.IGNORECASE)
MIN_SIZE_NOTE = 25
_HEADING = re.compile(r"^\s*#{1,6}\s+(.*)$")
_SKIPPED_SECTION = re.compile(r"\bbefore\b|\bafter\b|\bbench|\btest|\bchecklist|\bscreenshot|\bhow to (?:test|run|verify)|\bverif", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")

_REVIEW_WORDS = {"approved": "approved", "commented": "commented", "changes_requested": "asked for changes", "dismissed": "review dismissed"}


def asks_about_a_pull_request(question: str) -> bool:
    """A pull request named by number or repo, and a question about it (not who should hear of it)."""
    text = question or ""
    if coordination.pr_reference(text, bare_number=False) is None:
        return False
    return bool(_ONLY.match(text) or ASKS.search(text))


@dataclass
class Person:
    name: str
    node: tuple[str, str]  # (id, label) the path cites
    login: str


@dataclass
class PullRequest:
    id: str
    name: str
    title: str
    state: str
    merged: bool
    merged_at: str
    url: str
    summary: str
    repo: str
    authors: list[Person] = field(default_factory=list)
    reviews: list[tuple[Person, str]] = field(default_factory=list)
    requested: list[Person] = field(default_factory=list)
    touches: list[Entity] = field(default_factory=list)
    # dependent service -> the touched services it depends on
    dependents: dict[str, tuple[Entity, list[Entity]]] = field(default_factory=dict)
    closes: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)


def pr_explain(question: str, scopes: list[str]) -> ToolResult:
    _, graph = connect()
    reference = coordination.pr_reference(question)
    if reference is None:
        return ToolResult(
            tool=TOOL,
            answer="Name the pull request by its number, as in #207, PR 207 or graph_hacks#207.",
            found=False,
        )
    named = coordination.find_pull_request(graph, question, reference, scopes)
    if not isinstance(named, tuple):
        return _not_a_pull_request(graph, question, reference, scopes, named)
    entity, _candidates = named
    result = ToolResult(tool=TOOL, answer="")
    pr = read(graph, entity, scopes, result)
    result.answer = _answer(question, pr)
    return result


def _not_a_pull_request(graph, question: str, reference: tuple[str | None, int], scopes: list[str], miss: ToolResult) -> ToolResult:
    """A number that is an issue is answered as the ticket; otherwise the miss, in this tool's words."""
    issue = coordination.find_issue(graph, reference, scopes)
    if issue is not None:
        from src.domain.knowledge import ticket_status  # ticket_status reaches ask_service, which imports this module

        return ticket_status.ticket_status(issue.name, scopes)
    if miss.candidates and "More than one pull request" in miss.answer:
        return ToolResult(tool=TOOL, answer=miss.answer, found=False, candidates=list(miss.candidates))
    repo, number = reference
    shown = f"{repo}#{number}" if repo else f"#{number}"
    answer = f"I can't find pull request {shown} in the graph you can read."
    if miss.candidates:
        answer += f" Nearest: {join_names(miss.candidates)}."
    else:
        answer += " It may be in a private repo, or older than the pull requests GitHub ingest has read."
    return ToolResult(tool=TOOL, answer=answer, found=False, candidates=list(miss.candidates))


def read(graph, target: Entity, scopes: list[str], result: ToolResult) -> PullRequest:
    """The pull request's properties and edges, each edge cited on ``result``."""
    params = {"id": target.id, "scopes": list(scopes)}
    rows = graph.ro_query(
        f"""
        MATCH (pr:PullRequest {{id: $id}})
        OPTIONAL MATCH (pr)-[:IN_REPO]->(r:Repo)
        WHERE {SCOPED.format(var="r")}
        RETURN pr.name, coalesce(pr.title, ''), coalesce(pr.state, ''), coalesce(pr.merged, false),
               coalesce(pr.merged_at, ''), coalesce(pr.url, ''), coalesce(pr.summary, ''), coalesce(r.full_name, '')
        """,
        params,
    ).result_set
    name, title, state, merged, merged_at, url, summary, repo = rows[0] if rows else (target.name, "", "", False, "", "", "", "")
    pr = PullRequest(target.id, name, title, str(state).lower(), bool(merged), str(merged_at or ""), url, summary, repo)

    def cite(from_name, rel, to_name, from_node, to_node):
        result.add(PathStep(from_name, rel, to_name, from_id=from_node[0], to_id=to_node[0], from_label=from_node[1], to_label=to_node[1]))

    here = (pr.id, "PullRequest")
    for rel, review_state, github_id, login, person_id, person in graph.ro_query(
        f"""
        MATCH (pr:PullRequest {{id: $id}})
        WITH pr
        MATCH (gh:GitHubUser)-[r:AUTHORED|REVIEWED|REVIEW_REQUESTED]->(pr)
        WHERE {SCOPED.format(var="gh")}
        OPTIONAL MATCH (gh)-[:IS]->(p:Person)
        WHERE {SCOPED.format(var="p")}
        RETURN type(r), coalesce(r.state, ''), gh.id, gh.login, p.id, p.name
        ORDER BY type(r), coalesce(p.name, gh.login)
        """,
        params,
    ).result_set:
        who = Person(person or f"@{login}", (person_id, "Person") if person_id else (github_id, "GitHubUser"), login)
        if rel == "AUTHORED":
            pr.authors.append(who)
        elif rel == "REVIEWED":
            pr.reviews.append((who, str(review_state).lower()))
        else:
            pr.requested.append(who)
        cite(who.name, rel, pr.name, who.node, here)

    for service_id, service, kind in graph.ro_query(
        f"""
        MATCH (pr:PullRequest {{id: $id}})
        WITH pr
        MATCH (pr)-[:TOUCHES]->(s:Service)
        WHERE {SCOPED.format(var="s")}
        RETURN s.id, s.name, coalesce(s.kind, 'service')
        ORDER BY s.name
        """,
        params,
    ).result_set:
        pr.touches.append(Entity(service_id, service, "Service", kind))
        cite(pr.name, "TOUCHES", service, here, (service_id, "Service"))

    touched = {entity.id: entity for entity in pr.touches}
    if touched:
        for dependent_id, dependent, kind, on_id in graph.ro_query(
            f"""
            UNWIND $touched AS touched_id
            MATCH (s:Service {{id: touched_id}})
            WITH s
            MATCH (d:Service)-[:DEPENDS_ON]->(s)
            WHERE {SCOPED.format(var="d")} AND NOT d.id IN $touched
            RETURN d.id, d.name, coalesce(d.kind, 'service'), s.id
            ORDER BY d.name, s.id
            """,
            {**params, "touched": list(touched)},
        ).result_set:
            entry = pr.dependents.setdefault(dependent_id, (Entity(dependent_id, dependent, "Service", kind), []))
            entry[1].append(touched[on_id])
            cite(dependent, "DEPENDS_ON", touched[on_id].name, (dependent_id, "Service"), (on_id, "Service"))

    for rel, ticket_id, ticket, ticket_title, ticket_state in graph.ro_query(
        f"""
        MATCH (pr:PullRequest {{id: $id}})
        WITH pr
        MATCH (pr)-[r:CLOSES|REFERENCES]->(t:Ticket)
        WHERE {SCOPED.format(var="t")}
        RETURN type(r), t.id, t.name, coalesce(t.title, ''), coalesce(t.state, 'open')
        ORDER BY t.name
        """,
        params,
    ).result_set:
        shown = _ticket(ticket, ticket_title, ticket_state)
        (pr.closes if rel == "CLOSES" else pr.references).append(shown)
        cite(pr.name, rel, ticket, here, (ticket_id, "Ticket"))

    for decision_id, decision in graph.ro_query(
        f"""
        MATCH (pr:PullRequest {{id: $id}})
        WITH pr
        MATCH (d:Decision)-[:BASED_ON|CITES]->(pr)
        WHERE {SCOPED.format(var="d")}
        RETURN DISTINCT d.id, d.name
        ORDER BY d.name
        """,
        params,
    ).result_set:
        pr.decisions.append(decision)
        cite(decision, "BASED_ON", pr.name, (decision_id, "Decision"), here)
    return pr


def _ticket(name: str, title: str, state: str) -> str:
    closed = str(state).lower() == "closed"
    titled = f" “{title}”" if title and title != name else ""
    return f"{name}{titled} ({'closed' if closed else 'open'})"


def when(stamp: str) -> str:
    """"2026-10-18T17:15:37Z" -> "18 Oct 2026, 17:15 UTC"; anything else as it came."""
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return stamp
    return f"{moment.day} {moment.strftime('%b %Y, %H:%M')} UTC"


def _prose(line: str) -> str:
    """One description line as plain prose: no size marker, bold, or bullet; empty when nothing is left."""
    sized = _SIZE.match(line)
    if sized:
        line = line[sized.end() :].strip()
        bare = line.strip("()., ")
        if len(bare) < MIN_SIZE_NOTE:
            return ""
        line = bare[:1].upper() + bare[1:] + "."
    line = re.sub(r"\*\*|__", "", line).strip()
    return re.sub(r"^[-*]\s+", "", line)


def gist(body: str, size: int = GIST_CHARS) -> str:
    """The description's first prose, trimmed to about ``size`` characters at a sentence end.

    Size markers ("**Size: LARGE**") are dropped but what follows them is kept; headings, tables,
    code, checklists and footers are skipped, and so are whole sections of measurements or test
    notes ("## Before / after", "## Tests")."""
    kept: list[str] = []
    length = 0
    skipping = False
    for line in (body or "").splitlines():
        heading = _HEADING.match(line)
        if heading:
            skipping = bool(_SKIPPED_SECTION.search(heading.group(1)))
            continue
        if skipping or not line.strip() or _NOT_PROSE.match(line):
            continue
        line = _prose(line)
        if not line:
            continue
        kept.append(line)
        length += len(line) + 1
        if length >= size:
            break
    text = " ".join(" ".join(kept).split())
    if len(text) <= size:
        return text
    cut = text[: size + 1]
    ends = [match.end() for match in _SENTENCE_END.finditer(cut)]
    if ends and ends[-1] >= size // 2:
        return cut[: ends[-1]]
    return cut[: cut.rfind(" ")].rstrip(",;:") + "…" if " " in cut else cut[:size] + "…"


def _state(pr: PullRequest) -> str:
    if pr.merged:
        return f"merged {when(pr.merged_at)}" if pr.merged_at else "merged"
    return "closed without merging" if pr.state == "closed" else "open"


def _people(people: list[Person]) -> str:
    return join_names([f"{who.name} (@{who.login})" if not who.name.startswith("@") else who.name for who in people])


def _reviews(pr: PullRequest) -> str:
    if not pr.reviews:
        waiting = f" Review requested from {_people(pr.requested)}." if pr.requested else ""
        return f"No one has reviewed {pr.name} on GitHub.{waiting}"
    shown = join_names([f"{who.name} ({_REVIEW_WORDS.get(state, state or 'reviewed')})" for who, state in pr.reviews])
    waiting = f" Still requested: {_people(pr.requested)}." if pr.requested else ""
    return f"Reviewed by {shown}.{waiting}"


def _affects(pr: PullRequest) -> str:
    if not pr.touches:
        return f"{pr.name} touches no service the graph maps (its files match no Service.paths)."
    parts = [f"{pr.name} touches {_some([entity.name for entity in pr.touches])}."]
    if pr.dependents:
        hop = []
        for entity, on in sorted(pr.dependents.values(), key=lambda item: item[0].name):
            hop.append(f"{entity.name} (on {join_names([service.name for service in on])})")
        parts.append(f"One hop out, {_some(hop)} depend{'s' if len(hop) == 1 else ''} on {'it' if len(pr.touches) == 1 else 'them'}.")
    else:
        parts.append("Nothing you can read depends on " + ("it." if len(pr.touches) == 1 else "them."))
    return " ".join(parts)


def _tickets(pr: PullRequest) -> str:
    if not pr.closes and not pr.references:
        return f"{pr.name} closes and references no ticket."
    parts = []
    if pr.closes:
        parts.append(f"Closes {_some(pr.closes)}.")
    if pr.references:
        parts.append(f"References {_some(pr.references)}.")
    return " ".join(parts)


def _some(names: list[str]) -> str:
    shown = names[:MAX_LISTED]
    if len(names) > MAX_LISTED:
        shown = [*shown, f"{len(names) - MAX_LISTED} more"]
    return join_names(shown)


def _answer(question: str, pr: PullRequest) -> str:
    titled = f" “{pr.title}”" if pr.title else ""
    head = f"{pr.name}{titled} is {_state(pr)}."
    wrote = f"Written by {_people(pr.authors)}." if pr.authors else "Its author isn't visible to you."
    lead: list[str] = []
    rest: list[str] = []
    if _WHO_WROTE.search(question) and not _WHO_REVIEWED.search(question):
        lead.append(f"{pr.name} was written by {_people(pr.authors)}." if pr.authors else f"The author of {pr.name} isn't visible to you.")
        lead.append(head)
        wrote = ""
    elif _WHO_REVIEWED.search(question):
        lead.append(_reviews(pr))
        lead.append(head)
    elif _TICKETS.search(question) and not _AFFECTS.search(question):
        lead.append(_tickets(pr))
        lead.append(head)
    elif _AFFECTS.search(question):
        lead.append(_affects(pr))
        lead.append(head)
    else:
        lead.append(head)
    if wrote:
        rest.append(wrote)
    if not _WHO_REVIEWED.search(question):
        rest.append(_reviews(pr))
    described = gist(pr.summary)
    if described:
        rest.append(f"What it does: {described}")
    if not (_AFFECTS.search(question) and lead[0] == _affects(pr)):
        rest.append(_affects(pr))
    if pr.closes or pr.references:
        if lead[0] != _tickets(pr):
            rest.append(_tickets(pr))
    elif lead[0] != _tickets(pr):
        rest.append("No ticket linked.")
    if pr.decisions:
        rest.append(f"Decisions based on it: {_some(pr.decisions)}.")
    return " ".join(part for part in lead + rest if part)


__all__ = ["TOOL", "ASKS", "asks_about_a_pull_request", "pr_explain", "read", "gist", "when"]
