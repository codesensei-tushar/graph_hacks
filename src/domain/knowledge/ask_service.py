"""Graph tools that answer from FalkorDB and return the path they walked.

Every call takes the caller's readable scopes. Nodes with no ``scope`` property are
seeded company knowledge and count as ``org_shared``. Tickets, chats, and decisions
written from Slack carry the scope of the room they came from, and are invisible
outside it.
"""

import re

from src.db.falkordb_client import connect

ORG_SHARED = "org_shared"
SCOPED = "coalesce({var}.scope, 'org_shared') IN $scopes"

STOPWORDS = {
    "who", "what", "why", "how", "did", "does", "the", "and", "for", "with",
    "this", "that", "from", "into", "about", "should", "look", "owns", "own",
    "knows", "know", "worked", "work", "break", "breaks", "goes", "down",
    "if", "when", "new", "ticket", "just", "landed", "show", "path", "a", "an",
    "of", "on", "to", "in", "is", "are", "we", "our", "me", "please", "else",
    "has", "it", "move", "moved", "off", "service",
}


def ask(question: str, scopes: list[str]) -> dict:
    text = question.strip()
    if not text:
        return {"tool": "none", "answer": "Ask a question about the company graph.", "path": []}

    lowered = text.lower()
    if any(word in lowered for word in ("why", "decision", "decide", "decided")):
        return decision_trace(text, scopes)
    if any(word in lowered for word in ("break", "breaks", "goes down", "outage", "depend", "blast")):
        return blast_radius(text, scopes)
    return who_knows(text, scopes)


def ingest_ticket(title: str, about: str, scope: str) -> dict:
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
    return {
        "id": ticket_id,
        "about_name": target["name"],
        "path": [{"from": ticket_id, "rel": "ABOUT", "to": target["name"]}],
    }


def who_knows(question: str, scopes: list[str]) -> dict:
    _, graph = connect()
    target = _best_node(graph, question, labels=("Service", "Document", "Ticket"), scopes=scopes)
    if target is None:
        return {
            "tool": "who_knows",
            "answer": "Nothing in the graph matches that. Try a service, document, or ticket.",
            "path": [],
        }

    rows = graph.query(
        """
        MATCH (person:Person)-[rel:OWNS|WORKED_ON|AUTHORED|KNOWS]->(node {id: $id})
        RETURN person.name, type(rel), node.name
        ORDER BY person.name, type(rel)
        """,
        {"id": target["id"]},
    ).result_set
    path = [{"from": person, "rel": rel, "to": node} for person, rel, node in rows]
    # GitHub people on an issue-backed ticket: who opened it and who is assigned.
    github_rows = graph.query(
        """
        MATCH (gh:GitHubUser)-[rel:OPENED|ASSIGNED]->(node {id: $id})
        OPTIONAL MATCH (gh)-[:IS]->(person:Person)
        RETURN coalesce(person.name, '@' + gh.login), type(rel), node.name
        ORDER BY type(rel)
        """,
        {"id": target["id"]},
    ).result_set
    path.extend({"from": who, "rel": rel, "to": node} for who, rel, node in github_rows)

    if target["label"] != "Service":
        links = graph.query(
            """
            MATCH (node {id: $id})-[rel:ABOUT|DESCRIBES]->(service:Service)
            RETURN node.name, type(rel), service.name
            """,
            {"id": target["id"]},
        ).result_set
        path.extend({"from": source, "rel": rel, "to": other} for source, rel, other in links)
        via = graph.query(
            """
            MATCH (person:Person)-[rel:OWNS|WORKED_ON|KNOWS]->(service:Service)<-[:DESCRIBES|ABOUT]-(node {id: $id})
            RETURN person.name, type(rel), service.name
            ORDER BY person.name
            """,
            {"id": target["id"]},
        ).result_set
        for person, rel, service in via:
            step = {"from": person, "rel": rel, "to": service}
            if step not in path:
                path.append(step)
    else:
        tickets = graph.query(
            f"""
            MATCH (ticket:Ticket)-[:ABOUT]->(service:Service {{id: $id}})
            WHERE {SCOPED.format(var="ticket")}
            OPTIONAL MATCH (person:Person)-[:WORKED_ON]->(ticket)
            RETURN ticket.name, ticket.title, service.name, person.name
            ORDER BY ticket.name
            """,
            {"id": target["id"], "scopes": scopes},
        ).result_set
        for ticket, _title, service, person in tickets:
            path.append({"from": ticket, "rel": "ABOUT", "to": service})
            if person:
                path.append({"from": person, "rel": "WORKED_ON", "to": ticket})
        # Pull requests that changed this service's code, and who wrote or reviewed them.
        pull_requests = graph.query(
            f"""
            MATCH (gh:GitHubUser)-[rel:AUTHORED|REVIEWED]->(pr:PullRequest)-[:TOUCHES]->(service:Service {{id: $id}})
            WHERE {SCOPED.format(var="pr")}
            OPTIONAL MATCH (gh)-[:IS]->(person:Person)
            RETURN coalesce(person.name, '@' + gh.login), type(rel), pr.name, service.name
            ORDER BY pr.name
            """,
            {"id": target["id"], "scopes": scopes},
        ).result_set
        for who, rel, pull_request, service in pull_requests:
            for step in ({"from": pull_request, "rel": "TOUCHES", "to": service}, {"from": who, "rel": rel, "to": pull_request}):
                if step not in path:
                    path.append(step)

    if not path:
        return {
            "tool": "who_knows",
            "answer": f"{target['name']} is in the graph, and no person is linked to it yet.",
            "path": [],
        }

    owners = [step["from"] for step in path if step["rel"] == "OWNS"]
    workers = [step["from"] for step in path if step["rel"] in {"WORKED_ON", "AUTHORED", "ASSIGNED", "OPENED"} and step["from"] not in owners]
    reviewers = [step["from"] for step in path if step["rel"] == "REVIEWED" and step["from"] not in owners + workers]
    knowers = [step["from"] for step in path if step["rel"] == "KNOWS"]
    open_tickets = [step["from"] for step in path if step["rel"] == "ABOUT" and step["to"] == target["name"]]
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
    return {"tool": "who_knows", "answer": " ".join(parts), "path": path}


def decision_trace(question: str, scopes: list[str]) -> dict:
    _, graph = connect()
    target = _best_node(graph, question, labels=("Decision",), scopes=scopes)
    if target is None:
        return {
            "tool": "decision_trace",
            "answer": "No decision you can see matches that question.",
            "path": [],
        }

    people = graph.query(
        """
        MATCH (person:Person)-[rel:PROPOSED|APPROVED]->(decision:Decision {id: $id})
        RETURN person.name, type(rel), decision.name, decision.summary
        ORDER BY type(rel) DESC
        """,
        {"id": target["id"]},
    ).result_set
    cites = graph.query(
        f"""
        MATCH (decision:Decision {{id: $id}})-[rel:CITES|ABOUT]->(source)
        WHERE {SCOPED.format(var="source")}
        RETURN decision.name, type(rel), source.name, labels(source)[0]
        """,
        {"id": target["id"], "scopes": scopes},
    ).result_set
    chats = graph.query(
        f"""
        MATCH (chat:Chat)-[:ABOUT]->(decision:Decision {{id: $id}})
        WHERE {SCOPED.format(var="chat")}
        OPTIONAL MATCH (author:Person)-[:AUTHORED]->(chat)
        RETURN author.name, chat.name, chat.summary
        """,
        {"id": target["id"], "scopes": scopes},
    ).result_set

    summary_rows = graph.query("MATCH (d:Decision {id: $id}) RETURN d.summary", {"id": target["id"]}).result_set
    summary = summary_rows[0][0] if summary_rows else ""
    proposed = [row[0] for row in people if row[1] == "PROPOSED"]
    approved = [row[0] for row in people if row[1] == "APPROVED"]
    sources = [row[2] for row in cites if row[1] == "CITES"]
    answer = f"{target['name']}. {summary}"
    if proposed:
        answer += f" Proposed by {', '.join(proposed)}."
    if approved:
        answer += f" Approved by {', '.join(approved)}."
    if sources:
        answer += f" Sources: {', '.join(sources)}."

    path = [{"from": person, "rel": rel, "to": decision} for person, rel, decision, _summary in people]
    path.extend({"from": decision, "rel": rel, "to": source} for decision, rel, source, _label in cites)
    for author, chat, _summary in chats:
        if author:
            path.append({"from": author, "rel": "AUTHORED", "to": chat})
        path.append({"from": chat, "rel": "ABOUT", "to": target["name"]})
    return {"tool": "decision_trace", "answer": answer, "path": path}


def blast_radius(question: str, scopes: list[str]) -> dict:
    _, graph = connect()
    target = _best_node(graph, question, labels=("Service",), scopes=scopes)
    if target is None:
        return {
            "tool": "blast_radius",
            "answer": "Name a service that exists in the graph, such as Auth Service or Billing Service.",
            "path": [],
        }

    edges = graph.query(
        """
        MATCH (child:Service)-[:DEPENDS_ON]->(parent:Service)
        RETURN child.id, child.name, parent.id, parent.name
        """
    ).result_set
    dependents: dict[str, list[tuple[str, str, str]]] = {}
    for child_id, child_name, parent_id, parent_name in edges:
        dependents.setdefault(parent_id, []).append((child_id, child_name, parent_name))

    path = []
    seen = {target["id"]}
    queue = [target["id"]]
    while queue:
        current = queue.pop(0)
        for child_id, child_name, parent_name in dependents.get(current, []):
            path.append({"from": child_name, "rel": "DEPENDS_ON", "to": parent_name})
            if child_id not in seen:
                seen.add(child_id)
                queue.append(child_id)

    if not path:
        return {
            "tool": "blast_radius",
            "answer": f"Nothing in the graph depends on {target['name']}.",
            "path": [],
        }
    affected = _unique(step["from"] for step in path)
    return {
        "tool": "blast_radius",
        "answer": f"If {target['name']} goes down, these stop with it: {', '.join(affected)}.",
        "path": path,
    }


def match_service(text: str) -> dict | None:
    """The org-wide service a piece of text is most likely about, or None."""
    _, graph = connect()
    return _best_node(graph, text, labels=("Service",), scopes=[ORG_SHARED])


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
