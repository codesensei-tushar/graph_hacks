"""Graph tools that answer from FalkorDB and return the path they walked.

Every call takes the caller's readable scopes. Nodes with no ``scope`` property are
seeded company knowledge and count as ``org_shared``. Tickets, chats, and decisions
written from Slack carry the scope of the room they came from, and are invisible
outside it.

``ask`` is the keyword router: it picks a tool from the question's words when no
model is configured. The agent in ``agent.py`` picks tools with a model instead and
calls the same functions through the catalogue.
"""

import re
from dataclasses import dataclass, field

from src.db.falkordb_client import connect
from src.domain.knowledge.dto import PathStep, ToolResult

ORG_SHARED = "org_shared"
SCOPED = "coalesce({var}.scope, 'org_shared') IN $scopes"

STOPWORDS = {
    "who", "what", "why", "how", "did", "does", "the", "and", "for", "with",
    "this", "that", "from", "into", "about", "should", "look", "owns", "own",
    "knows", "know", "worked", "work", "break", "breaks", "goes", "down",
    "if", "when", "new", "ticket", "just", "landed", "show", "path", "a", "an",
    "of", "on", "to", "in", "is", "are", "we", "our", "me", "please", "else",
    "has", "it", "move", "moved", "off", "service", "owner", "owned", "runs",
}

_OWNS = re.compile(r"\b(?:owns?|owner|owned|responsible)\b", re.IGNORECASE)
_WIDER_THAN_OWNER = re.compile(r"\b(?:else|also|worked|works|knows?|reviewed|touched)\b", re.IGNORECASE)


@dataclass
class TicketCreated:
    id: str
    about_name: str
    path: list[PathStep] = field(default_factory=list)


def ask(question: str, scopes: list[str]) -> ToolResult:
    """Pick a tool from the question's words. The fallback when no model is configured."""
    text = question.strip()
    if not text:
        return ToolResult(tool="none", answer="Ask a question about the company graph.")

    lowered = text.lower()
    if any(word in lowered for word in ("why", "decision", "decide", "decided")):
        return decision_trace(text, scopes)
    if any(word in lowered for word in ("break", "breaks", "goes down", "outage", "depend", "blast")):
        return blast_radius(text, scopes)
    if _OWNS.search(lowered) and not _WIDER_THAN_OWNER.search(lowered):
        return who_owns(text, scopes)
    return who_knows(text, scopes)


def ingest_ticket(title: str, about: str, scope: str) -> TicketCreated:
    _, graph = connect()
    target = _best_node(graph, about or title, labels=("Service",), scopes=[ORG_SHARED])
    if target is None:
        target = _node_by_id(graph, "api-gateway")
    ticket_id = _next_ticket_id(graph)
    graph.query(
        """
        CREATE (ticket:Ticket {id: $id, name: $id, title: $title, summary: $title, scope: $scope})
        WITH ticket
        MATCH (service:Service {id: $service})
        MERGE (ticket)-[:ABOUT]->(service)
        """,
        {"id": ticket_id, "title": title, "service": target["id"], "scope": scope},
    )
    step = PathStep(ticket_id, "ABOUT", target["name"], from_id=ticket_id, to_id=target["id"], from_label="Ticket", to_label="Service")
    return TicketCreated(id=ticket_id, about_name=target["name"], path=[step])


def who_owns(question: str, scopes: list[str]) -> ToolResult:
    _, graph = connect()
    target = _best_node(graph, question, labels=("Service", "Document", "Ticket"), scopes=scopes)
    if target is None:
        return _not_found("who_owns", "Nothing in the graph matches that. Name a service, document, or ticket.")

    result = ToolResult(tool="who_owns", answer="")
    service = target
    if target["label"] != "Service":
        rows = graph.query(
            """
            MATCH (node {id: $id})-[rel:ABOUT|DESCRIBES]->(service:Service)
            RETURN service.id, service.name, type(rel)
            LIMIT 1
            """,
            {"id": target["id"]},
        ).result_set
        if rows:
            service = {"id": rows[0][0], "name": rows[0][1], "label": "Service"}
            result.add(_step(target, rows[0][2], service))

    owners = graph.query(
        """
        MATCH (person:Person)-[:OWNS]->(service:Service {id: $id})
        RETURN person.id, person.name
        ORDER BY person.name
        """,
        {"id": service["id"]},
    ).result_set
    teams = graph.query(
        """
        MATCH (team:Team)-[:RUNS]->(service:Service {id: $id})
        RETURN team.id, team.name
        ORDER BY team.name
        """,
        {"id": service["id"]},
    ).result_set
    for person_id, person_name in owners:
        result.add(_step({"id": person_id, "name": person_name, "label": "Person"}, "OWNS", service))
    for team_id, team_name in teams:
        result.add(_step({"id": team_id, "name": team_name, "label": "Team"}, "RUNS", service))

    parts = []
    if service is not target:
        parts.append(f"{target['name']} is about {service['name']}.")
    if owners:
        parts.append(f"{service['name']} is owned by {', '.join(name for _, name in owners)}.")
    elif teams:
        parts.append(f"{service['name']} has no named owner.")
    else:
        parts.append(f"{service['name']} is in the graph, and no owner or team is recorded for it.")
    if teams:
        parts.append(f"Run by the {', '.join(name for _, name in teams)} team.")
    result.answer = " ".join(parts)
    return result


def who_knows(question: str, scopes: list[str]) -> ToolResult:
    _, graph = connect()
    target = _best_node(graph, question, labels=("Service", "Document", "Ticket"), scopes=scopes)
    if target is None:
        return _not_found("who_knows", "Nothing in the graph matches that. Try a service, document, or ticket.")

    result = ToolResult(tool="who_knows", answer="")
    rows = graph.query(
        """
        MATCH (person:Person)-[rel:OWNS|WORKED_ON|AUTHORED|KNOWS]->(node {id: $id})
        RETURN person.id, person.name, type(rel), node.name
        ORDER BY person.name, type(rel)
        """,
        {"id": target["id"]},
    ).result_set
    for person_id, person, rel, node in rows:
        result.add(PathStep(person, rel, node, from_id=person_id, to_id=target["id"], from_label="Person", to_label=target["label"]))
    # GitHub people on an issue-backed ticket: who opened it and who is assigned.
    github_rows = graph.query(
        """
        MATCH (gh:GitHubUser)-[rel:OPENED|ASSIGNED]->(node {id: $id})
        OPTIONAL MATCH (gh)-[:IS]->(person:Person)
        RETURN coalesce(person.id, gh.id), coalesce(person.name, '@' + gh.login), type(rel), node.name
        ORDER BY type(rel)
        """,
        {"id": target["id"]},
    ).result_set
    for who_id, who, rel, node in github_rows:
        result.add(PathStep(who, rel, node, from_id=who_id, to_id=target["id"], from_label="Person", to_label=target["label"]))

    if target["label"] != "Service":
        links = graph.query(
            """
            MATCH (node {id: $id})-[rel:ABOUT|DESCRIBES]->(service:Service)
            RETURN node.name, type(rel), service.id, service.name
            """,
            {"id": target["id"]},
        ).result_set
        for source, rel, service_id, service in links:
            result.add(PathStep(source, rel, service, from_id=target["id"], to_id=service_id, from_label=target["label"], to_label="Service"))
        via = graph.query(
            """
            MATCH (person:Person)-[rel:OWNS|WORKED_ON|KNOWS]->(service:Service)<-[:DESCRIBES|ABOUT]-(node {id: $id})
            RETURN person.id, person.name, type(rel), service.id, service.name
            ORDER BY person.name
            """,
            {"id": target["id"]},
        ).result_set
        for person_id, person, rel, service_id, service in via:
            result.add(PathStep(person, rel, service, from_id=person_id, to_id=service_id, from_label="Person", to_label="Service"))
    else:
        tickets = graph.query(
            f"""
            MATCH (ticket:Ticket)-[:ABOUT]->(service:Service {{id: $id}})
            WHERE {SCOPED.format(var="ticket")}
            OPTIONAL MATCH (person:Person)-[:WORKED_ON]->(ticket)
            RETURN ticket.id, ticket.name, service.name, person.id, person.name
            ORDER BY ticket.name
            """,
            {"id": target["id"], "scopes": scopes},
        ).result_set
        for ticket_id, ticket, service, person_id, person in tickets:
            result.add(PathStep(ticket, "ABOUT", service, from_id=ticket_id, to_id=target["id"], from_label="Ticket", to_label="Service"))
            if person:
                result.add(PathStep(person, "WORKED_ON", ticket, from_id=person_id, to_id=ticket_id, from_label="Person", to_label="Ticket"))
        # Pull requests that changed this service's code, and who wrote or reviewed them.
        pull_requests = graph.query(
            f"""
            MATCH (gh:GitHubUser)-[rel:AUTHORED|REVIEWED]->(pr:PullRequest)-[:TOUCHES]->(service:Service {{id: $id}})
            WHERE {SCOPED.format(var="pr")}
            OPTIONAL MATCH (gh)-[:IS]->(person:Person)
            RETURN coalesce(person.id, gh.id), coalesce(person.name, '@' + gh.login), type(rel), pr.id, pr.name, service.name
            ORDER BY pr.name
            """,
            {"id": target["id"], "scopes": scopes},
        ).result_set
        for who_id, who, rel, pr_id, pull_request, service in pull_requests:
            result.add(PathStep(pull_request, "TOUCHES", service, from_id=pr_id, to_id=target["id"], from_label="PullRequest", to_label="Service"))
            result.add(PathStep(who, rel, pull_request, from_id=who_id, to_id=pr_id, from_label="Person", to_label="PullRequest"))

    if not result.path:
        result.answer = f"{target['name']} is in the graph, and no person is linked to it yet."
        return result

    path = result.path
    owners = [step.from_name for step in path if step.rel == "OWNS"]
    workers = [step.from_name for step in path if step.rel in {"WORKED_ON", "AUTHORED", "ASSIGNED", "OPENED"} and step.from_name not in owners]
    reviewers = [step.from_name for step in path if step.rel == "REVIEWED" and step.from_name not in owners + workers]
    knowers = [step.from_name for step in path if step.rel == "KNOWS"]
    open_tickets = [step.from_name for step in path if step.rel == "ABOUT" and step.to_name == target["name"]]
    parts = []
    if owners:
        parts.append(f"{target['name']} is owned by {', '.join(_unique(owners))}.")
    else:
        parts.append(f"{target['name']} is in the graph.")
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
    target = _best_node(graph, question, labels=("Decision",), scopes=scopes)
    if target is None:
        return _not_found("decision_trace", "No decision you can see matches that question.")

    people = graph.query(
        """
        MATCH (person:Person)-[rel:PROPOSED|APPROVED]->(decision:Decision {id: $id})
        RETURN person.id, person.name, type(rel), decision.name
        ORDER BY type(rel) DESC
        """,
        {"id": target["id"]},
    ).result_set
    cites = graph.query(
        f"""
        MATCH (decision:Decision {{id: $id}})-[rel:CITES|ABOUT]->(source)
        WHERE {SCOPED.format(var="source")}
        RETURN decision.name, type(rel), source.id, source.name, labels(source)[0]
        """,
        {"id": target["id"], "scopes": scopes},
    ).result_set
    chats = graph.query(
        f"""
        MATCH (chat:Chat)-[:ABOUT]->(decision:Decision {{id: $id}})
        WHERE {SCOPED.format(var="chat")}
        OPTIONAL MATCH (author:Person)-[:AUTHORED]->(chat)
        RETURN author.id, author.name, chat.id, chat.name
        """,
        {"id": target["id"], "scopes": scopes},
    ).result_set

    summary_rows = graph.query("MATCH (d:Decision {id: $id}) RETURN d.summary", {"id": target["id"]}).result_set
    summary = summary_rows[0][0] if summary_rows else ""
    proposed = [row[1] for row in people if row[2] == "PROPOSED"]
    approved = [row[1] for row in people if row[2] == "APPROVED"]
    sources = [row[3] for row in cites if row[1] == "CITES"]
    answer = f"{target['name']}. {summary}"
    if proposed:
        answer += f" Proposed by {', '.join(proposed)}."
    if approved:
        answer += f" Approved by {', '.join(approved)}."
    if sources:
        answer += f" Sources: {', '.join(sources)}."

    result = ToolResult(tool="decision_trace", answer=answer)
    for person_id, person, rel, decision in people:
        result.add(PathStep(person, rel, decision, from_id=person_id, to_id=target["id"], from_label="Person", to_label="Decision"))
    for decision, rel, source_id, source, label in cites:
        result.add(PathStep(decision, rel, source, from_id=target["id"], to_id=source_id, from_label="Decision", to_label=label))
    for author_id, author, chat_id, chat in chats:
        if author:
            result.add(PathStep(author, "AUTHORED", chat, from_id=author_id, to_id=chat_id, from_label="Person", to_label="Chat"))
        result.add(PathStep(chat, "ABOUT", target["name"], from_id=chat_id, to_id=target["id"], from_label="Chat", to_label="Decision"))
    return result


def blast_radius(question: str, scopes: list[str]) -> ToolResult:
    _, graph = connect()
    target = _best_node(graph, question, labels=("Service",), scopes=scopes)
    if target is None:
        return _not_found("blast_radius", "Name a service that exists in the graph, such as Auth Service or Billing Service.")

    edges = graph.query(
        """
        MATCH (child:Service)-[:DEPENDS_ON]->(parent:Service)
        RETURN child.id, child.name, parent.id, parent.name
        """
    ).result_set
    dependents: dict[str, list[tuple[str, str, str]]] = {}
    for child_id, child_name, parent_id, parent_name in edges:
        dependents.setdefault(parent_id, []).append((child_id, child_name, parent_name))

    result = ToolResult(tool="blast_radius", answer="")
    seen = {target["id"]}
    queue = [target["id"]]
    while queue:
        current = queue.pop(0)
        for child_id, child_name, parent_name in dependents.get(current, []):
            result.add(PathStep(child_name, "DEPENDS_ON", parent_name, from_id=child_id, to_id=current, from_label="Service", to_label="Service"))
            if child_id not in seen:
                seen.add(child_id)
                queue.append(child_id)

    if not result.path:
        result.answer = f"Nothing in the graph depends on {target['name']}."
        return result
    affected = _unique(step.from_name for step in result.path)
    result.answer = f"If {target['name']} goes down, these stop with it: {', '.join(affected)}."
    return result


def match_service(text: str) -> dict | None:
    """The org-wide service a piece of text is most likely about, or None."""
    _, graph = connect()
    return _best_node(graph, text, labels=("Service",), scopes=[ORG_SHARED])


def _not_found(tool: str, answer: str) -> ToolResult:
    return ToolResult(tool=tool, answer=answer, found=False)


def _step(source: dict, rel: str, target: dict) -> PathStep:
    return PathStep(
        source["name"], rel, target["name"],
        from_id=source["id"], to_id=target["id"], from_label=source.get("label"), to_label=target.get("label"),
    )


def _best_node(graph, question: str, labels: tuple[str, ...], scopes: list[str]) -> dict | None:
    rows = graph.query(
        f"""
        MATCH (n)
        WHERE labels(n)[0] IN $labels AND n.name IS NOT NULL AND {SCOPED.format(var="n")}
        RETURN n.id, n.name, labels(n)[0], coalesce(n.aliases, ''), coalesce(n.title, '')
        """,
        {"labels": list(labels), "scopes": scopes},
    ).result_set
    question_l = question.lower()
    tokens = {token for token in re.split(r"[^a-z0-9]+", question_l) if len(token) > 2 and token not in STOPWORDS}
    best = None
    best_score = 0
    for node_id, name, label, aliases, title in rows:
        alias_list = [alias.strip().lower() for alias in aliases.split(",") if alias.strip()]
        haystack = " ".join((name, aliases, title)).lower()
        score = 0
        if name and name.lower() in question_l:
            score += 10 + len(name)
        for alias in alias_list:
            if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", question_l):
                score += 6 + len(alias)
        for token in tokens:
            if token in haystack:
                score += 2
        if score > best_score:
            best_score = score
            best = {"id": node_id, "name": name, "label": label}
    if best_score < 2:
        return None
    return best


def _node_by_id(graph, node_id: str) -> dict:
    row = graph.query(
        "MATCH (n {id: $id}) RETURN n.id, n.name, labels(n)[0]",
        {"id": node_id},
    ).result_set[0]
    return {"id": row[0], "name": row[1], "label": row[2]}


def _next_ticket_id(graph) -> str:
    rows = graph.query("MATCH (ticket:Ticket) RETURN ticket.id").result_set
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
