"""Graph tools that answer from FalkorDB and return the path they walked.

Every call takes the caller's readable scopes. Nodes with no ``scope`` property are
seeded company knowledge and count as ``org_shared``. Tickets, chats, and decisions
written from Slack carry the scope of the room they came from, and are invisible
outside it.

Names are resolved against the graph first (``resolve.py``). A question about
something the graph does not have gets ``found=False`` and the nearest names, never
a guess. Reads go through ``ro_query``; only ``ingest_ticket`` writes.

``ask`` is the keyword router: it picks a tool from the question's words when no
model is configured. The agent picks tools with a model instead and calls the same
functions through the catalogue.
"""

import re

from src.db.falkordb_client import connect
from src.domain.knowledge import resolve
from src.domain.knowledge.dto import PathStep, TicketCreated, ToolResult
from src.domain.knowledge.resolve import ORG_SHARED, SCOPED, Entity, Resolution

ANSWER_LABELS = ("Service", "Document", "Ticket")

_OWNS = re.compile(r"\b(?:owns?|owner|owned|responsible)\b", re.IGNORECASE)
_WIDER_THAN_OWNER = re.compile(r"\b(?:else|also|worked|works|knows?|reviewed|touched)\b", re.IGNORECASE)


def route(question: str) -> tuple[str, dict]:
    """Pick a tool and its arguments from the question's words. The fallback when no model is configured."""
    text = question.strip()
    if not text:
        return "none", {}
    lowered = text.lower()
    if any(word in lowered for word in ("why", "decision", "decide", "decided")):
        return "decision_trace", {"topic": text}
    if any(word in lowered for word in ("break", "breaks", "goes down", "outage", "depend", "blast")):
        return "blast_radius", {"name": text}
    if _OWNS.search(lowered) and not _WIDER_THAN_OWNER.search(lowered):
        return "who_owns", {"name": text}
    return "who_knows", {"name": text}


def ask(question: str, scopes: list[str]) -> ToolResult:
    """Route by keywords and run the tool. The whole no-model path in one call."""
    tool, args = route(question)
    if tool == "none":
        return ToolResult(tool="none", answer="Ask a question about the company graph.")
    return _ROUTED[tool](args, scopes)


def ticket_target(title: str, about: str, graph=None) -> Entity | None:
    """The service a ticket is about: a strict match first, then a best guess. None when nothing matches."""
    if graph is None:
        _, graph = connect()
    text = about or title
    target = resolve.resolve_name(text, ("Service",), [ORG_SHARED], graph=graph).entity
    # Classifying a ticket is a best guess, unlike answering a question; the approval card shows it first.
    return target or resolve.best_match(text, ("Service",), [ORG_SHARED], graph=graph)


def ingest_ticket(title: str, about: str, scope: str) -> TicketCreated:
    _, graph = connect()
    target = ticket_target(title, about, graph=graph)
    ticket_id = _next_ticket_id(graph)
    graph.query(
        "CREATE (ticket:Ticket {id: $id, name: $id, title: $title, summary: $title, scope: $scope})",
        {"id": ticket_id, "title": title, "scope": scope},
    )
    if target is None:
        return TicketCreated(id=ticket_id, about_name=None)
    graph.query(
        "MATCH (ticket:Ticket {id: $id}), (service:Service {id: $service}) MERGE (ticket)-[:ABOUT]->(service)",
        {"id": ticket_id, "service": target.id},
    )
    step = PathStep(ticket_id, "ABOUT", target.name, from_id=ticket_id, to_id=target.id, from_label="Ticket", to_label="Service")
    return TicketCreated(id=ticket_id, about_name=target.name, path=[step])


def who_owns(question: str, scopes: list[str]) -> ToolResult:
    _, graph = connect()
    resolution = resolve.resolve_name(question, ANSWER_LABELS, scopes, graph=graph)
    if not resolution.found:
        return _miss("who_owns", resolution)
    target = resolution.entity

    result = ToolResult(tool="who_owns", answer="", candidates=resolution.candidate_names())
    service = target
    if target.label != "Service":
        rows = graph.ro_query(
            """
            MATCH (node {id: $id})-[rel:ABOUT|DESCRIBES]->(service:Service)
            RETURN service.id, service.name, type(rel)
            LIMIT 1
            """,
            {"id": target.id},
        ).result_set
        if rows:
            service = Entity(rows[0][0], rows[0][1], "Service")
            result.add(_step(target, rows[0][2], service))

    owners = graph.ro_query(
        """
        MATCH (person:Person)-[:OWNS]->(service:Service {id: $id})
        RETURN person.id, person.name
        ORDER BY person.name
        """,
        {"id": service.id},
    ).result_set
    teams = graph.ro_query(
        """
        MATCH (team:Team)-[:RUNS]->(service:Service {id: $id})
        RETURN team.id, team.name
        ORDER BY team.name
        """,
        {"id": service.id},
    ).result_set
    for person_id, person_name in owners:
        result.add(_step(Entity(person_id, person_name, "Person"), "OWNS", service))
    for team_id, team_name in teams:
        result.add(_step(Entity(team_id, team_name, "Team"), "RUNS", service))

    parts = []
    if service != target:
        parts.append(f"{target.name} is about {service.name}.")
    if owners:
        parts.append(f"{service.name} is owned by {', '.join(name for _, name in owners)}.")
    elif teams:
        parts.append(f"{service.name} has no named owner.")
    else:
        parts.append(f"{service.name} is in the graph, and no owner or team is recorded for it.")
    if teams:
        parts.append(f"Run by the {', '.join(name for _, name in teams)} team.")
    result.answer = " ".join(parts)
    return result


def who_knows(question: str, scopes: list[str]) -> ToolResult:
    _, graph = connect()
    resolution = resolve.resolve_name(question, ANSWER_LABELS, scopes, graph=graph)
    if not resolution.found:
        return _miss("who_knows", resolution)
    target = resolution.entity

    result = ToolResult(tool="who_knows", answer="", candidates=resolution.candidate_names())
    rows = graph.ro_query(
        """
        MATCH (person:Person)-[rel:OWNS|WORKED_ON|AUTHORED|KNOWS]->(node {id: $id})
        RETURN person.id, person.name, type(rel), node.name
        ORDER BY person.name, type(rel)
        """,
        {"id": target.id},
    ).result_set
    for person_id, person, rel, node in rows:
        result.add(PathStep(person, rel, node, from_id=person_id, to_id=target.id, from_label="Person", to_label=target.label))
    # GitHub people on an issue-backed ticket: who opened it and who is assigned.
    github_rows = graph.ro_query(
        """
        MATCH (gh:GitHubUser)-[rel:OPENED|ASSIGNED]->(node {id: $id})
        OPTIONAL MATCH (gh)-[:IS]->(person:Person)
        RETURN coalesce(person.id, gh.id), coalesce(person.name, '@' + gh.login),
               CASE WHEN person IS NULL THEN 'GitHubUser' ELSE 'Person' END, type(rel), node.name
        ORDER BY type(rel)
        """,
        {"id": target.id},
    ).result_set
    for who_id, who, who_label, rel, node in github_rows:
        result.add(PathStep(who, rel, node, from_id=who_id, to_id=target.id, from_label=who_label, to_label=target.label))

    if target.label != "Service":
        links = graph.ro_query(
            """
            MATCH (node {id: $id})-[rel:ABOUT|DESCRIBES]->(service:Service)
            RETURN node.name, type(rel), service.id, service.name
            """,
            {"id": target.id},
        ).result_set
        for source, rel, service_id, service in links:
            result.add(PathStep(source, rel, service, from_id=target.id, to_id=service_id, from_label=target.label, to_label="Service"))
        via = graph.ro_query(
            """
            MATCH (person:Person)-[rel:OWNS|WORKED_ON|KNOWS]->(service:Service)<-[:DESCRIBES|ABOUT]-(node {id: $id})
            RETURN person.id, person.name, type(rel), service.id, service.name
            ORDER BY person.name
            """,
            {"id": target.id},
        ).result_set
        for person_id, person, rel, service_id, service in via:
            result.add(PathStep(person, rel, service, from_id=person_id, to_id=service_id, from_label="Person", to_label="Service"))
    else:
        tickets = graph.ro_query(
            f"""
            MATCH (ticket:Ticket)-[:ABOUT]->(service:Service {{id: $id}})
            WHERE {SCOPED.format(var="ticket")}
            OPTIONAL MATCH (person:Person)-[:WORKED_ON]->(ticket)
            RETURN ticket.id, ticket.name, service.name, person.id, person.name
            ORDER BY ticket.name
            """,
            {"id": target.id, "scopes": scopes},
        ).result_set
        for ticket_id, ticket, service, person_id, person in tickets:
            result.add(PathStep(ticket, "ABOUT", service, from_id=ticket_id, to_id=target.id, from_label="Ticket", to_label="Service"))
            if person:
                result.add(PathStep(person, "WORKED_ON", ticket, from_id=person_id, to_id=ticket_id, from_label="Person", to_label="Ticket"))
        # Pull requests that changed this service's code, and who wrote or reviewed them.
        pull_requests = graph.ro_query(
            f"""
            MATCH (gh:GitHubUser)-[rel:AUTHORED|REVIEWED]->(pr:PullRequest)-[:TOUCHES]->(service:Service {{id: $id}})
            WHERE {SCOPED.format(var="pr")}
            OPTIONAL MATCH (gh)-[:IS]->(person:Person)
            RETURN coalesce(person.id, gh.id), coalesce(person.name, '@' + gh.login),
                   CASE WHEN person IS NULL THEN 'GitHubUser' ELSE 'Person' END, type(rel), pr.id, pr.name, service.name
            ORDER BY pr.name
            """,
            {"id": target.id, "scopes": scopes},
        ).result_set
        for who_id, who, who_label, rel, pr_id, pull_request, service in pull_requests:
            result.add(PathStep(pull_request, "TOUCHES", service, from_id=pr_id, to_id=target.id, from_label="PullRequest", to_label="Service"))
            result.add(PathStep(who, rel, pull_request, from_id=who_id, to_id=pr_id, from_label=who_label, to_label="PullRequest"))

    if not result.path:
        result.answer = f"{target.name} is in the graph, and no person is linked to it yet."
        return result

    path = result.path
    owners = [step.from_name for step in path if step.rel == "OWNS"]
    workers = [step.from_name for step in path if step.rel in {"WORKED_ON", "AUTHORED", "ASSIGNED", "OPENED"} and step.from_name not in owners]
    reviewers = [step.from_name for step in path if step.rel == "REVIEWED" and step.from_name not in owners + workers]
    knowers = [step.from_name for step in path if step.rel == "KNOWS"]
    open_tickets = [step.from_name for step in path if step.rel == "ABOUT" and step.to_name == target.name]
    parts = []
    if owners:
        parts.append(f"{target.name} is owned by {', '.join(_unique(owners))}.")
    else:
        parts.append(f"{target.name} is in the graph.")
    if workers:
        parts.append(f"Also worked on by {', '.join(_unique(workers))}.")
    if reviewers:
        parts.append(f"Reviewed by {', '.join(_unique(reviewers))}.")
    if knowers:
        parts.append(f"Known by {', '.join(_unique(knowers))}.")
    if open_tickets:
        parts.append(f"Tickets: {', '.join(_unique(open_tickets))}.")
    result.answer = " ".join(parts)
    return result


def decision_trace(question: str, scopes: list[str]) -> ToolResult:
    _, graph = connect()
    resolution = resolve.resolve_name(question, ("Decision",), scopes, graph=graph)
    if not resolution.found:
        return _miss("decision_trace", resolution, kind="decision")
    target = resolution.entity

    people = graph.ro_query(
        """
        MATCH (person:Person)-[rel:PROPOSED|APPROVED]->(decision:Decision {id: $id})
        RETURN person.id, person.name, type(rel), decision.name
        ORDER BY type(rel) DESC
        """,
        {"id": target.id},
    ).result_set
    cites = graph.ro_query(
        f"""
        MATCH (decision:Decision {{id: $id}})-[rel:CITES|ABOUT]->(source)
        WHERE {SCOPED.format(var="source")}
        RETURN decision.name, type(rel), source.id, source.name, labels(source)[0]
        """,
        {"id": target.id, "scopes": scopes},
    ).result_set
    chats = graph.ro_query(
        f"""
        MATCH (chat:Chat)-[:ABOUT]->(decision:Decision {{id: $id}})
        WHERE {SCOPED.format(var="chat")}
        OPTIONAL MATCH (author:Person)-[:AUTHORED]->(chat)
        RETURN author.id, author.name, chat.id, chat.name
        """,
        {"id": target.id, "scopes": scopes},
    ).result_set

    summary_rows = graph.ro_query("MATCH (d:Decision {id: $id}) RETURN d.summary", {"id": target.id}).result_set
    summary = summary_rows[0][0] if summary_rows else ""
    proposed = [row[1] for row in people if row[2] == "PROPOSED"]
    approved = [row[1] for row in people if row[2] == "APPROVED"]
    sources = [row[3] for row in cites if row[1] == "CITES"]
    answer = f"{target.name}. {summary}"
    if proposed:
        answer += f" Proposed by {', '.join(proposed)}."
    if approved:
        answer += f" Approved by {', '.join(approved)}."
    if sources:
        answer += f" Sources: {', '.join(sources)}."

    result = ToolResult(tool="decision_trace", answer=answer, candidates=resolution.candidate_names())
    for person_id, person, rel, decision in people:
        result.add(PathStep(person, rel, decision, from_id=person_id, to_id=target.id, from_label="Person", to_label="Decision"))
    for decision, rel, source_id, source, label in cites:
        result.add(PathStep(decision, rel, source, from_id=target.id, to_id=source_id, from_label="Decision", to_label=label))
    for author_id, author, chat_id, chat in chats:
        if author:
            result.add(PathStep(author, "AUTHORED", chat, from_id=author_id, to_id=chat_id, from_label="Person", to_label="Chat"))
        result.add(PathStep(chat, "ABOUT", target.name, from_id=chat_id, to_id=target.id, from_label="Chat", to_label="Decision"))
    return result


def blast_radius(question: str, scopes: list[str]) -> ToolResult:
    _, graph = connect()
    resolution = resolve.resolve_name(question, ("Service",), scopes, graph=graph)
    if not resolution.found:
        return _miss("blast_radius", resolution, kind="service")
    target = resolution.entity

    edges = graph.ro_query(
        """
        MATCH (child:Service)-[:DEPENDS_ON]->(parent:Service)
        RETURN child.id, child.name, parent.id, parent.name
        """
    ).result_set
    dependents: dict[str, list[tuple[str, str, str]]] = {}
    for child_id, child_name, parent_id, parent_name in edges:
        dependents.setdefault(parent_id, []).append((child_id, child_name, parent_name))

    result = ToolResult(tool="blast_radius", answer="", candidates=resolution.candidate_names())
    seen = {target.id}
    queue = [target.id]
    while queue:
        current = queue.pop(0)
        for child_id, child_name, parent_name in dependents.get(current, []):
            result.add(PathStep(child_name, "DEPENDS_ON", parent_name, from_id=child_id, to_id=current, from_label="Service", to_label="Service"))
            if child_id not in seen:
                seen.add(child_id)
                queue.append(child_id)

    if not result.path:
        result.answer = f"Nothing in the graph depends on {target.name}."
        return result
    affected = _unique(step.from_name for step in result.path)
    result.answer = f"If {target.name} goes down, these stop with it: {', '.join(affected)}."
    return result


def match_service(text: str) -> dict | None:
    """The org-wide service a piece of ingested text is most likely about, or None. A best guess, for classification."""
    entity = resolve.best_match(text, ("Service",), [ORG_SHARED])
    return {"id": entity.id, "name": entity.name, "label": entity.label} if entity else None


_ROUTED = {
    "decision_trace": lambda args, scopes: decision_trace(args["topic"], scopes),
    "blast_radius": lambda args, scopes: blast_radius(args["name"], scopes),
    "who_owns": lambda args, scopes: who_owns(args["name"], scopes),
    "who_knows": lambda args, scopes: who_knows(args["name"], scopes),
}


def _miss(tool: str, resolution: Resolution, kind: str = "service, document, or ticket") -> ToolResult:
    return ToolResult(tool=tool, answer=resolve.describe_miss(resolution, kind), found=False, candidates=resolution.candidate_names())


def _step(source: Entity, rel: str, target: Entity) -> PathStep:
    return PathStep(source.name, rel, target.name, from_id=source.id, to_id=target.id, from_label=source.label, to_label=target.label)


def _next_ticket_id(graph) -> str:
    rows = graph.ro_query("MATCH (ticket:Ticket) RETURN ticket.id").result_set
    numbers = []
    for (ticket_id,) in rows:
        match = re.search(r"(\d+)$", str(ticket_id))
        if match:
            numbers.append(int(match.group(1)))
    return f"TKT-{max(numbers, default=100) + 1}"


def _unique(items) -> list[str]:
    seen = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen
