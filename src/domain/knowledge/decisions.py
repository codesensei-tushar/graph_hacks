"""Decisions that stay current.

``record_decision`` writes a Decision with the asker as proposer and links it to the
services it is about, the earlier decision it supersedes, the channel it was decided
in, and the documents, tickets or pull requests it is based on. It writes the graph,
so it runs only after the Approve click, with the approved scope and the asker,
channel and message time the approval flow adds to its arguments.

``decision_trace`` answers "why did we ..." with the current decision, the one it
replaced (and says so when the question names a stale one), both dates, and the
sources, as path steps.

Graph shape, also what ``src/seed/seed.py`` writes::

    (:Person)-[:PROPOSED|APPROVED]->(:Decision {id, name, summary, aliases, decided_at, scope})
    (:Decision)-[:ABOUT]->(:Service)
    (:Decision)-[:SUPERSEDES]->(:Decision)
    (:Decision)-[:DECIDED_IN]->(:Channel|:Document)
    (:Decision)-[:BASED_ON]->(:Document|:Ticket|:PullRequest)
    (:Decision)-[:RECORDED_FROM]->(:Memory)   the Slack message it was recorded from, quoted in the trace

``decided_at`` is milliseconds since the epoch. Graphs seeded before this module carry
``CITES`` where ``BASED_ON`` now goes; the trace reads both.
"""

import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from src.db.falkordb_client import connect
from src.domain.knowledge import catalogue, fallback, resolve
from src.domain.knowledge.dto import PathStep, ToolResult, ToolSpec
from src.domain.knowledge.resolve import Entity, join_names

_WHY = re.compile(r"\bwhy\b", re.IGNORECASE)

ORG_SHARED = "org_shared"
SCOPED = "coalesce({var}.scope, 'org_shared') IN $scopes"
SOURCE_LABELS = ("Document", "Ticket", "PullRequest")
# Words too common to tie a new decision to an old one on their own.
_STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "about", "that", "this", "move", "moving",
    "keep", "use", "using", "our", "all", "service", "services", "will", "now", "off", "onto",
}
# A question that asks for a decision outright, so a service it names can stand for that service's decision.
_ASKS_FOR_A_DECISION = re.compile(r"\b(?:decisions?|decided)\b", re.IGNORECASE)


# --- time ----------------------------------------------------------------------------------


def to_millis(value) -> int | None:
    """Epoch milliseconds from an int, float, or string in seconds or milliseconds."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    # Anything below this is seconds (that many milliseconds would still be in 1973).
    return int(number * 1000) if number < 100_000_000_000 else int(number)


def now_millis() -> int:
    return int(time.time() * 1000)


def format_date(millis: int | None) -> str:
    if millis is None:
        return "an unknown date"
    moment = datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
    return f"{moment.day} {moment:%b %Y}"


# --- trace ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionRow:
    id: str
    name: str
    summary: str
    decided_at: int | None

    @property
    def entity(self) -> Entity:
        return Entity(self.id, self.name, "Decision")


def _decision(graph, decision_id: str) -> DecisionRow | None:
    rows = graph.ro_query(
        "MATCH (d:Decision {id: $id}) RETURN d.id, d.name, coalesce(d.summary, ''), d.decided_at", {"id": decision_id}
    ).result_set
    if not rows:
        return None
    return DecisionRow(rows[0][0], rows[0][1], rows[0][2], rows[0][3])


def _newer_than(graph, decision_id: str, scopes: list[str]) -> DecisionRow | None:
    """The visible decision that supersedes this one, newest first when several do."""
    rows = graph.ro_query(
        f"""
        MATCH (newer:Decision)-[:SUPERSEDES]->(:Decision {{id: $id}})
        WHERE {SCOPED.format(var="newer")}
        RETURN newer.id, newer.name, coalesce(newer.summary, ''), newer.decided_at
        ORDER BY coalesce(newer.decided_at, 0) DESC
        LIMIT 1
        """,
        {"id": decision_id, "scopes": scopes},
    ).result_set
    return DecisionRow(*rows[0]) if rows else None


def _older_than(graph, decision_id: str, scopes: list[str]) -> DecisionRow | None:
    rows = graph.ro_query(
        f"""
        MATCH (:Decision {{id: $id}})-[:SUPERSEDES]->(older:Decision)
        WHERE {SCOPED.format(var="older")}
        RETURN older.id, older.name, coalesce(older.summary, ''), older.decided_at
        ORDER BY coalesce(older.decided_at, 0) DESC
        LIMIT 1
        """,
        {"id": decision_id, "scopes": scopes},
    ).result_set
    return DecisionRow(*rows[0]) if rows else None


def current_decision(graph, decision_id: str, scopes: list[str]) -> DecisionRow | None:
    """Follow SUPERSEDES forward to the decision that stands today."""
    current = _decision(graph, decision_id)
    seen = {decision_id}
    while current is not None:
        newer = _newer_than(graph, current.id, scopes)
        if newer is None or newer.id in seen:
            return current
        seen.add(newer.id)
        current = newer
    return None


def _describe(graph, decision: DecisionRow, scopes: list[str], result: ToolResult, *, sources_too: bool = True) -> list[str]:
    """Add one decision's edges to ``result`` and return the sentences that describe it."""
    people = graph.ro_query(
        """
        MATCH (person:Person)-[rel:PROPOSED|APPROVED]->(decision:Decision {id: $id})
        RETURN person.id, person.name, type(rel)
        ORDER BY type(rel) DESC, person.name
        """,
        {"id": decision.id},
    ).result_set
    about = graph.ro_query(
        f"MATCH (:Decision {{id: $id}})-[:ABOUT]->(service:Service) WHERE {SCOPED.format(var='service')} RETURN service.id, service.name ORDER BY service.name",
        {"id": decision.id, "scopes": scopes},
    ).result_set
    sources = graph.ro_query(
        f"""
        MATCH (:Decision {{id: $id}})-[rel:BASED_ON|CITES]->(source)
        WHERE {SCOPED.format(var="source")}
        RETURN source.id, source.name, labels(source)[0], type(rel)
        ORDER BY source.name
        """,
        {"id": decision.id, "scopes": scopes},
    ).result_set
    where = graph.ro_query(
        """
        MATCH (:Decision {id: $id})-[:DECIDED_IN]->(place)
        RETURN place.id, coalesce(place.name, place.id), labels(place)[0]
        """,
        {"id": decision.id},
    ).result_set
    came_from = _recorded_from(graph, decision.id)

    for person_id, person, rel in people:
        result.add(PathStep(person, rel, decision.name, from_id=person_id, to_id=decision.id, from_label="Person", to_label="Decision"))
    for service_id, service in about:
        result.add(PathStep(decision.name, "ABOUT", service, from_id=decision.id, to_id=service_id, from_label="Decision", to_label="Service"))
    if sources_too:
        for source_id, source, label, rel in sources:
            result.add(PathStep(decision.name, rel, source, from_id=decision.id, to_id=source_id, from_label="Decision", to_label=label))
    for place_id, place, label in where:
        shown = f"#{place}" if label == "Channel" and not place.startswith("#") else place
        result.add(PathStep(decision.name, "DECIDED_IN", shown, from_id=decision.id, to_id=place_id, from_label="Decision", to_label=label))
    if came_from is not None:
        result.add(PathStep(decision.name, "RECORDED_FROM", f"“{_clip(came_from.text)}”", from_id=decision.id, to_id=came_from.memory_id, from_label="Decision", to_label="Memory"))

    sentences = []
    proposed = [person for _id, person, rel in people if rel == "PROPOSED"]
    approved = [person for _id, person, rel in people if rel == "APPROVED"]
    if came_from is not None:
        # The reason, quoted from the message it was recorded from: where, when, who, and what was said.
        who = came_from.author or ", ".join(proposed) or "someone"
        sentences.append(f"Decided in #{came_from.channel} on {format_date(decision.decided_at)} by {who}: “{came_from.text}”.")
        if proposed and came_from.author and came_from.author not in proposed:
            sentences.append(f"Proposed by {', '.join(proposed)}.")
    else:
        if decision.decided_at is not None:
            sentences.append(f"Decided {format_date(decision.decided_at)}.")
        if proposed:
            sentences.append(f"Proposed by {', '.join(proposed)}.")
    if approved:
        sentences.append(f"Approved by {', '.join(approved)}.")
    if sources_too and sources:
        sentences.append(f"Based on: {', '.join(source_name for _id, source_name, _label, _rel in sources)}.")
    if came_from is None:
        for _id, place, label in where:
            sentences.append(f"Decided in {'#' + place if label == 'Channel' else place}.")
    return sentences


@dataclass(frozen=True)
class SourceMessage:
    """The Slack message a decision was recorded from, as kept by the memory layer."""

    memory_id: str
    text: str
    channel: str
    ts: str | None
    author: str | None


def _clip(text: str, size: int = 140) -> str:
    text = " ".join(text.split())
    return text if len(text) <= size else text[: size - 1].rstrip() + "…"


def _recorded_from(graph, decision_id: str) -> SourceMessage | None:
    rows = graph.ro_query(
        """
        MATCH (:Decision {id: $id})-[:RECORDED_FROM]->(m:Memory)
        OPTIONAL MATCH (u:SlackUser)-[:SAID]->(m)
        OPTIONAL MATCH (u)-[:IS]->(p:Person)
        OPTIONAL MATCH (c:Channel {id: m.channel_id})
        RETURN m.id, coalesce(m.text, m.content, ''), coalesce(c.name, m.channel_id, ''), m.ts, coalesce(p.name, u.name, u.id)
        LIMIT 1
        """,
        {"id": decision_id},
    ).result_set
    if not rows:
        return None
    memory_id, text, channel, ts, author = rows[0]
    return SourceMessage(memory_id, text, channel, ts, author)


def _slack_ts(seconds: float) -> str:
    return f"{seconds:.6f}"


# A decision recorded a little after the message that announced it still comes from that message.
SOURCE_WINDOW_SECONDS = 15 * 60


def source_message(graph, channel_id: str | None, slack_user_id: str | None, decided_at: int, source_ts: str | None = None) -> SourceMessage | None:
    """The kept message this decision came from: the one at ``source_ts`` when the caller
    names it, else the asker's latest message in the channel within the window before the
    decision. Seeded decisions have none."""
    if not channel_id:
        return None
    if source_ts:
        rows = graph.ro_query(
            "MATCH (m:Memory {channel_id: $channel, ts: $ts}) RETURN m.id, coalesce(m.text, m.content, ''), m.ts LIMIT 1",
            {"channel": channel_id, "ts": str(source_ts)},
        ).result_set
    elif slack_user_id:
        # Compare Slack ts as strings ("1792229885.974270", ten-digit seconds): FalkorDB's toFloat
        # is single precision and rounds a timestamp this size to the nearest 128 seconds.
        rows = graph.ro_query(
            """
            MATCH (:SlackUser {id: $user})-[:SAID]->(m:Memory {channel_id: $channel})
            WHERE m.ts IS NOT NULL AND m.ts <= $upper AND m.ts >= $lower
            RETURN m.id, coalesce(m.text, m.content, ''), m.ts
            ORDER BY m.ts DESC
            LIMIT 1
            """,
            {"user": slack_user_id, "channel": channel_id, "upper": _slack_ts(decided_at / 1000 + 1), "lower": _slack_ts(decided_at / 1000 - SOURCE_WINDOW_SECONDS)},
        ).result_set
    else:
        return None
    if not rows:
        return None
    memory_id, text, ts = rows[0]
    return SourceMessage(memory_id, text, channel_id, ts, None)


def _has_summary(decision: DecisionRow) -> bool:
    return bool(decision.summary.strip()) and decision.summary.strip() != decision.name.strip()


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else text + "."


def _headline(decision: DecisionRow) -> str:
    """``Name. Summary.`` or just ``Name.`` when the summary is missing or repeats the name."""
    return f"{decision.name}. {_sentence(decision.summary)}" if _has_summary(decision) else f"{decision.name}."


def _named_decision(graph, question: str, scopes: list[str]) -> resolve.Resolution:
    """The decision a question names. One that asks for "the decision" about a service and
    names no decision gets that service's standing decision, when it has exactly one.

    Only a question that says "decision" falls back: "why is billing slow" is not asking for
    the billing retries decision, and is refused as before.
    """
    resolution = resolve.resolve_name(question, ("Decision",), scopes, graph=graph)
    if resolution.found or not _ASKS_FOR_A_DECISION.search(question):
        return resolution
    service = resolve.resolve_name(question, ("Service",), scopes, graph=graph)
    if not service.found:
        return resolution
    rows = graph.ro_query(
        f"""
        MATCH (d:Decision)-[:ABOUT]->(:Service {{id: $id}})
        WHERE {SCOPED.format(var="d")}
        RETURN d.id
        """,
        {"id": service.entity.id, "scopes": scopes},
    ).result_set
    standing = {current.id: current for (decision_id,) in rows if (current := current_decision(graph, decision_id, scopes))}
    if len(standing) == 1:
        (only,) = standing.values()
        return resolve.Resolution(query=question, entity=Entity(only.id, only.name, "Decision"))
    return resolution


def decision_trace(question: str, scopes: list[str]) -> ToolResult:
    _, graph = connect()
    resolution = _named_decision(graph, question, scopes)
    if not resolution.found:
        miss = ToolResult(
            tool="decision_trace",
            answer=resolve.describe_miss(resolution, "decision"),
            found=False,
            candidates=resolution.candidate_names(),
        )
        # "what has maya decided" can still be answered; "why did we pick kafka" asks for a reason the graph lacks.
        return miss if _WHY.search(question) else fallback.before_refusing(question, scopes, miss)
    named = resolution.entity
    current = current_decision(graph, named.id, scopes) or _decision(graph, named.id)
    result = ToolResult(tool="decision_trace", answer="", candidates=resolution.candidate_names())

    parts = []
    if current.id != named.id:
        stale = _decision(graph, named.id)
        when = f" (decided {format_date(stale.decided_at)})" if stale and stale.decided_at is not None else ""
        parts.append(f"*{named.name}*{when} is no longer current.")
    parts.append(_headline(current))
    parts.extend(_describe(graph, current, scopes, result))

    older = _older_than(graph, current.id, scopes)
    if older is not None:
        result.add(PathStep(current.name, "SUPERSEDES", older.name, from_id=current.id, to_id=older.id, from_label="Decision", to_label="Decision"))
        replaced = f"This replaced *{older.name}* (decided {format_date(older.decided_at)})"
        parts.append(f"{replaced}: {_sentence(older.summary)}" if _has_summary(older) else f"{replaced}.")
        parts.extend(_describe(graph, older, scopes, result, sources_too=False))

    chats = graph.ro_query(
        f"""
        MATCH (chat:Chat)-[:ABOUT]->(decision:Decision {{id: $id}})
        WHERE {SCOPED.format(var="chat")}
        OPTIONAL MATCH (author:Person)-[:AUTHORED]->(chat)
        RETURN author.id, author.name, chat.id, chat.name
        """,
        {"id": current.id, "scopes": scopes},
    ).result_set
    for author_id, author, chat_id, chat in chats:
        if author:
            result.add(PathStep(author, "AUTHORED", chat, from_id=author_id, to_id=chat_id, from_label="Person", to_label="Chat"))
        result.add(PathStep(chat, "ABOUT", current.name, from_id=chat_id, to_id=current.id, from_label="Chat", to_label="Decision"))

    result.answer = " ".join(parts)
    return result


def decision_pressure(question: str, scopes: list[str]) -> ToolResult:
    """Is the standing decision still the last word on its service?

    Tickets ``ABOUT`` that service and pull requests that ``TOUCHES`` it, which this
    decision does not ``BASED_ON`` or ``CITES``, are the pressure. Ordinary "why did we"
    questions stay on ``decision_trace``; this only runs when the question asks whether
    the decision still holds.
    """
    _, graph = connect()
    resolution = _named_decision(graph, question, scopes)
    if not resolution.found:
        return ToolResult(
            tool="decision_pressure",
            answer=resolve.describe_miss(resolution, "decision"),
            found=False,
            candidates=resolution.candidate_names(),
        )
    named = resolution.entity
    current = current_decision(graph, named.id, scopes) or _decision(graph, named.id)
    result = ToolResult(tool="decision_pressure", answer="", candidates=resolution.candidate_names())
    parts = []
    if current.id != named.id:
        parts.append(f"*{named.name}* is no longer current.")
    about = graph.ro_query(
        f"MATCH (:Decision {{id: $id}})-[:ABOUT]->(service:Service) WHERE {SCOPED.format(var='service')} RETURN service.id, service.name ORDER BY service.name",
        {"id": current.id, "scopes": scopes},
    ).result_set
    for service_id, service in about:
        result.add(PathStep(current.name, "ABOUT", service, from_id=current.id, to_id=service_id, from_label="Decision", to_label="Service"))
    cited = graph.ro_query(
        f"""
        MATCH (:Decision {{id: $id}})-[rel:BASED_ON|CITES]->(source)
        WHERE {SCOPED.format(var="source")}
        RETURN source.id, source.name, labels(source)[0], type(rel)
        ORDER BY source.name
        """,
        {"id": current.id, "scopes": scopes},
    ).result_set
    for source_id, source, label, rel in cited:
        result.add(PathStep(current.name, rel, source, from_id=current.id, to_id=source_id, from_label="Decision", to_label=label))

    service_ids = [service_id for service_id, _name in about]
    service_names = join_names([name for _id, name in about])
    parts.append(f"*{current.name}* is the standing decision{f' about {service_names}' if service_names else ''}.")
    if cited:
        parts.append(f"It cites {join_names([source for _id, source, _label, _rel in cited])}.")
    if not about:
        parts.append("It is not about a service, so there is no ticket or pull request to check it against.")
        result.answer = " ".join(parts)
        return result

    tickets, pull_requests, total = uncited_work(graph, current.id, service_ids, scopes)
    # The few most on topic, then a count: a synced tracker can hold hundreds about one service.
    tickets, pull_requests = tickets[:PRESSURE_SHOWN], pull_requests[: max(0, PRESSURE_SHOWN - len(tickets[:PRESSURE_SHOWN]))]
    for ticket_id, ticket, service_id, service in tickets:
        result.add(PathStep(ticket, "ABOUT", service, from_id=ticket_id, to_id=service_id, from_label="Ticket", to_label="Service"))
    for pr_id, pr, service_id, service in pull_requests:
        result.add(PathStep(pr, "TOUCHES", service, from_id=pr_id, to_id=service_id, from_label="PullRequest", to_label="Service"))
    uncited = [f"{name} (about {service})" for _id, name, _sid, service in tickets + pull_requests]
    if total > len(uncited):
        uncited.append(f"{total - len(uncited)} more")
    if uncited:
        parts.append(f"It does not cite {join_names(uncited)}.")
    else:
        parts.append(f"Nothing else about {service_names} has landed that it does not cite.")
    result.answer = " ".join(parts)
    return result


# Uncited tickets, and uncited pull requests, named per decision before "and N more".
MAX_UNCITED = 8
# Uncited work decision_pressure names before "and N more", most on topic first.
PRESSURE_SHOWN = 3


def uncited_work(graph, decision_id: str, service_ids: list[str], scopes: list[str]) -> tuple[list, list, int]:
    """Tickets ``ABOUT`` and pull requests that ``TOUCHES`` these services which the decision does
    not ``BASED_ON`` or ``CITES``: rows of (id, name, service id, service name), at most
    ``MAX_UNCITED`` of each, and how many there are in all. Open and recently updated tickets come
    first (a synced tracker can hold hundreds about one service), then pull requests by name. What
    ``decision_pressure`` tells for one decision and ``decision_health`` checks for every one."""
    decision = _decision(graph, decision_id)
    decided_at = to_millis(decision.decided_at) if decision else None
    words = _tokens(f"{decision.name} {decision.summary}") if decision else set()
    tickets = graph.ro_query(
        f"""
        MATCH (ticket:Ticket)-[:ABOUT]->(service:Service)
        WHERE service.id IN $services AND {SCOPED.format(var="ticket")}
          AND NOT (:Decision {{id: $id}})-[:BASED_ON|CITES]->(ticket)
        RETURN ticket.id, ticket.name, service.id, service.name, coalesce(ticket.title, '') + ' ' + coalesce(ticket.summary, ''),
               [ticket.created_at, ticket.source_updated_at, ticket.updated_at]
        ORDER BY CASE WHEN coalesce(ticket.state, 'open') = 'closed' THEN 1 ELSE 0 END,
                 coalesce(ticket.source_updated_at, ticket.updated_at, '') DESC, ticket.name
        """,
        {"services": service_ids, "scopes": scopes, "id": decision_id},
    ).result_set
    pull_requests = graph.ro_query(
        f"""
        MATCH (pr:PullRequest)-[:TOUCHES]->(service:Service)
        WHERE service.id IN $services AND {SCOPED.format(var="pr")}
          AND NOT (:Decision {{id: $id}})-[:BASED_ON|CITES]->(pr)
        RETURN pr.id, pr.name, service.id, service.name, coalesce(pr.title, '') + ' ' + coalesce(pr.summary, ''),
               [pr.created_at, pr.merged_at, pr.updated_at]
        ORDER BY pr.name
        """,
        {"services": service_ids, "scopes": scopes, "id": decision_id},
    ).result_set
    tickets = _pressing(tickets, decided_at, words)
    pull_requests = _pressing(pull_requests, decided_at, words)
    return tickets[:MAX_UNCITED], pull_requests[:MAX_UNCITED], len(tickets) + len(pull_requests)


def _pressing(rows: list, decided_at: int | None, words: set[str]) -> list[tuple]:
    """The rows that can press on a decision, most on topic first: only work that landed or changed
    after it was decided (work with no date at all is kept), ranked by the words it shares with the
    decision's title and summary, then in the order given. Rows come back as (id, name, service id, service)."""
    kept = []
    for index, (node_id, name, service_id, service, text, stamps) in enumerate(rows):
        times = [when for when in (_when(stamp) for stamp in stamps or []) if when is not None]
        if decided_at is not None and times and max(times) <= decided_at:
            continue
        overlap = len(words & _tokens(f"{name} {text}"))
        kept.append((-overlap, index, (node_id, name, service_id, service)))
    return [row for _overlap, _index, row in sorted(kept)]


def _when(stamp) -> int | None:
    """Epoch milliseconds from a stored time: epoch seconds or milliseconds, or ISO 8601 text (Jira, GitHub)."""
    if stamp is None or stamp == "":
        return None
    millis = to_millis(stamp)
    if millis is not None:
        return millis
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return int(when.timestamp() * 1000)


# --- record --------------------------------------------------------------------------------


def _tokens(text: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(token) > 2 and token not in _STOPWORDS}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48]


def services_named(graph, text: str, scopes: list[str]) -> list[Entity]:
    return resolve.resolve_all(text, ("Service",), scopes, graph=graph)


@dataclass(frozen=True)
class Supersession:
    """What a new decision replaces. ``miss`` is set when a decision was named but not found."""

    decision: DecisionRow | None = None
    miss: resolve.Resolution | None = None


def superseded_decision(graph, title: str, supersedes: str | None, about: list[Entity], scopes: list[str], decided_at: int) -> Supersession:
    """What this decision replaces: the one named, or else the standing decision about the
    same service that talks about the same thing. A named decision that is not in the graph
    is a miss, never a guess."""
    if supersedes:
        resolution = resolve.resolve_name(supersedes, ("Decision",), scopes, graph=graph)
        if not resolution.found:
            return Supersession(miss=resolution)
        found = resolution.entity
        return Supersession(decision=current_decision(graph, found.id, scopes) or _decision(graph, found.id))
    if not about:
        return Supersession()
    rows = graph.ro_query(
        f"""
        MATCH (d:Decision)-[:ABOUT]->(s:Service)
        WHERE s.id IN $services AND {SCOPED.format(var="d")}
          AND NOT (:Decision)-[:SUPERSEDES]->(d)
          AND coalesce(d.decided_at, 0) < $before
        RETURN DISTINCT d.id, d.name, coalesce(d.summary, ''), d.decided_at, coalesce(d.aliases, '')
        ORDER BY coalesce(d.decided_at, 0) DESC
        """,
        {"services": [service.id for service in about], "scopes": scopes, "before": decided_at},
    ).result_set
    wanted = _tokens(title)
    for decision_id, name, summary, when, aliases in rows:
        if wanted & _tokens(f"{name} {aliases}"):
            return Supersession(decision=DecisionRow(decision_id, name, summary, when))
    return Supersession()


def _person_for_slack_user(graph, slack_user_id: str | None) -> Entity | None:
    if not slack_user_id:
        return None
    rows = graph.ro_query(
        "MATCH (:SlackUser {id: $user})-[:IS]->(p:Person) RETURN p.id, p.name LIMIT 1", {"user": slack_user_id}
    ).result_set
    return Entity(rows[0][0], rows[0][1], "Person") if rows else None


def _channel_id(args: dict) -> str | None:
    if args.get("channel_id"):
        return str(args["channel_id"])
    scope = args.get("scope") or ""
    return scope.removeprefix("slack_channel_") if scope.startswith("slack_channel_") else None


def _sources(graph, named: list[str], scopes: list[str]) -> list[Entity]:
    found: list[Entity] = []
    for text in named or []:
        for entity in resolve.resolve_all(str(text), SOURCE_LABELS, scopes, graph=graph):
            if entity not in found:
                found.append(entity)
    return found


def _title(args: dict) -> str:
    title = " ".join(str(args.get("title") or "").split())
    return title[:1].upper() + title[1:]


def _replacing(found: Supersession) -> str:
    if found.miss is not None:
        nearest = ", ".join(found.miss.candidate_names())
        return f"replacing *{found.miss.query}*, which is not in the graph" + (f" (nearest: {nearest})" if nearest else "")
    if found.decision is None:
        return "replacing nothing"
    return f"replacing *{found.decision.name}* (decided {format_date(found.decision.decided_at)})"


def preview_record_decision(args: dict, scopes: list[str]) -> str:
    _, graph = connect()
    title = _title(args)
    about = services_named(graph, args.get("about") or title, scopes)
    found = superseded_decision(graph, title, args.get("supersedes"), about, scopes, to_millis(args.get("decided_at")) or now_millis())
    text = f"Record the decision *{title}*"
    text += f" about *{', '.join(s.name for s in about)}*" if about else ", not linked to any service (nothing in the graph matches it)"
    return f"{text}, {_replacing(found)}"


def record_decision(args: dict, scopes: list[str]) -> ToolResult:
    title = _title(args)
    if not title:
        return ToolResult(tool="record_decision", answer="A decision needs a title.", found=False)
    _, graph = connect()
    scope = args.get("scope") or ORG_SHARED
    decided_at = to_millis(args.get("decided_at")) or now_millis()
    about = services_named(graph, args.get("about") or title, scopes)
    found = superseded_decision(graph, title, args.get("supersedes"), about, scopes, decided_at)
    if found.miss is not None:
        return ToolResult(
            tool="record_decision",
            answer=f"Not recorded: {resolve.describe_miss(found.miss, 'decision')}",
            found=False,
            candidates=found.miss.candidate_names(),
        )
    older = found.decision
    sources = _sources(graph, args.get("sources") or [], scopes)
    proposer = _person_for_slack_user(graph, args.get("asker_slack_user_id"))
    channel_id = _channel_id(args)
    # The time alone could collide for two decisions in one millisecond; the suffix cannot.
    decision_id = f"decision-{_slug(title)}-{decided_at}-{uuid.uuid4().hex[:6]}"
    aliases = ", ".join(dict.fromkeys(str(value) for value in (args.get("about"), args.get("supersedes")) if value))

    graph.query(
        """
        MERGE (d:Decision {id: $id})
        SET d.name = $name, d.summary = $summary, d.aliases = $aliases, d.decided_at = $decided_at, d.scope = $scope
        """,
        {"id": decision_id, "name": title, "summary": (args.get("summary") or "").strip(), "aliases": aliases, "decided_at": decided_at, "scope": scope},
    )
    if proposer is not None:
        graph.query("MATCH (p:Person {id: $p}), (d:Decision {id: $d}) MERGE (p)-[:PROPOSED]->(d)", {"p": proposer.id, "d": decision_id})
    for service in about:
        graph.query("MATCH (d:Decision {id: $d}), (s:Service {id: $s}) MERGE (d)-[:ABOUT]->(s)", {"d": decision_id, "s": service.id})
    if older is not None:
        graph.query("MATCH (d:Decision {id: $d}), (o:Decision {id: $o}) MERGE (d)-[:SUPERSEDES]->(o)", {"d": decision_id, "o": older.id})
    if channel_id:
        graph.query(
            "MERGE (c:Channel {id: $c}) WITH c MATCH (d:Decision {id: $d}) MERGE (d)-[:DECIDED_IN]->(c)",
            {"c": channel_id, "d": decision_id},
        )
    for source in sources:
        graph.query("MATCH (d:Decision {id: $d}), (s {id: $s}) MERGE (d)-[:BASED_ON]->(s)", {"d": decision_id, "s": source.id})
    came_from = source_message(graph, channel_id, args.get("asker_slack_user_id"), decided_at, args.get("source_ts"))
    if came_from is not None:
        graph.query("MATCH (d:Decision {id: $d}), (m:Memory {id: $m}) MERGE (d)-[:RECORDED_FROM]->(m)", {"d": decision_id, "m": came_from.memory_id})

    recorded = _decision(graph, decision_id)
    result = ToolResult(tool="record_decision", answer="")
    answer = f"Recorded *{title}*"
    if about:
        answer += f" about {', '.join(service.name for service in about)}"
    answer += f", {_replacing(found)}."
    result.answer = " ".join([answer, *_describe(graph, recorded, scopes, result)])
    if older is not None:
        result.add(PathStep(title, "SUPERSEDES", older.name, from_id=decision_id, to_id=older.id, from_label="Decision", to_label="Decision"))
    if proposer is None and args.get("asker_slack_user_id"):
        result.answer += f" Proposed by <@{args['asker_slack_user_id']}>, who is not linked to a person in the graph yet."
    return result


catalogue.register(
    ToolSpec(
        name="record_decision",
        description=(
            "Record a decision the team just made: what was decided, what it is about, what it replaces, "
            "and what it is based on. Writes the graph, so it needs approval."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "The decision in one line, copied from the message."},
                "summary": {"type": "string", "description": "Why, in a sentence, if the message says."},
                "about": {"type": "string", "description": "The service or topic it concerns, copied from the message."},
                "supersedes": {"type": "string", "description": "The earlier decision or topic it replaces, if named."},
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Documents, tickets, or pull requests it is based on, as named in the message.",
                },
            },
            "required": ["title"],
            "additionalProperties": False,
        },
        run=record_decision,
        needs_approval=True,
        preview=preview_record_decision,
    )
)
