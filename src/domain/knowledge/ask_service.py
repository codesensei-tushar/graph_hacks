import re

from app.graph import connect

STOPWORDS = {
    "who", "what", "why", "how", "did", "does", "the", "and", "for", "with",
    "this", "that", "from", "into", "about", "should", "look", "owns", "own",
    "knows", "know", "worked", "work", "break", "breaks", "goes", "down",
    "if", "when", "new", "ticket", "just", "landed", "show", "path", "a", "an",
    "of", "on", "to", "in", "is", "are", "we", "our", "me", "please",
}


def ask(question: str) -> dict:
    text = question.strip()
    if not text:
        return {"tool": "none", "answer": "Ask a question about the company graph.", "path": []}

    lowered = text.lower()
    if lowered.startswith("ingest:") or lowered.startswith("ticket:"):
        title = text.split(":", 1)[1].strip()
        created = ingest_ticket(title, "")
        follow = who_knows(created["about_name"])
        follow["tool"] = "ingest"
        follow["answer"] = f"Added {created['id']} about {created['about_name']}. {follow['answer']}"
        follow["path"] = created["path"] + follow["path"]
        return follow

    if any(word in lowered for word in ("why", "decision", "split")):
        return decision_trace(text)
    if any(word in lowered for word in ("break", "breaks", "down", "depend", "blast")):
        return blast_radius(text)
    return who_knows(text)


def ingest_ticket(title: str, about: str) -> dict:
    _, graph = connect()
    target = _best_node(graph, about or title, labels=("Service",))
    if target is None:
        target = _first_service(graph)
    ticket_id = _next_ticket_id(graph)
    graph.query(
        """
        CREATE (ticket:Ticket {id: $id, name: $id, title: $title, summary: $title})
        WITH ticket
        MATCH (service:Service {id: $service})
        MERGE (ticket)-[:ABOUT]->(service)
        """,
        {"id": ticket_id, "title": title, "service": target["id"]},
    )
    return {
        "id": ticket_id,
        "about_name": target["name"],
        "path": [{"from": ticket_id, "rel": "ABOUT", "to": target["name"]}],
    }


def who_knows(question: str) -> dict:
    _, graph = connect()
    target = _best_node(graph, question, labels=("Service", "Document", "Ticket"))
    if target is None:
        return {
            "tool": "who_knows",
            "answer": "Nothing in the graph matches that. Try a service, file, or ticket name.",
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

    if target["label"] != "Service":
        via = graph.query(
            """
            MATCH (person:Person)-[rel:OWNS|WORKED_ON|AUTHORED|KNOWS]->(service:Service)<-[:DESCRIBES|ABOUT]-(node {id: $id})
            RETURN person.name, type(rel), service.name, node.name
            ORDER BY person.name
            """,
            {"id": target["id"]},
        ).result_set
    else:
        via = []

    path = [{"from": person, "rel": rel, "to": node} for person, rel, node in rows]
    links = graph.query(
        """
        MATCH (node {id: $id})-[rel:ABOUT|DESCRIBES]->(other)
        RETURN node.name, type(rel), other.name
        """,
        {"id": target["id"]},
    ).result_set
    path.extend({"from": source, "rel": rel, "to": other} for source, rel, other in links)
    path.extend(
        {"from": person, "rel": rel, "to": service}
        for person, rel, service, _node in via
        if {"from": person, "rel": rel, "to": service} not in path
    )
    if not path:
        return {
            "tool": "who_knows",
            "answer": f"{target['name']} is in the graph, and no person is linked to it yet.",
            "path": [],
        }

    owners = [step["from"] for step in path if step["rel"] == "OWNS"]
    workers = [step["from"] for step in path if step["rel"] in {"WORKED_ON", "AUTHORED"}]
    knowers = [step["from"] for step in path if step["rel"] == "KNOWS"]
    parts = [f"{target['name']} is in the graph."]
    if owners:
        parts.append(f"Owned by {', '.join(_unique(owners))}.")
    if workers:
        parts.append(f"Worked on by {', '.join(_unique(workers))}.")
    if knowers:
        parts.append(f"Also known by {', '.join(_unique(knowers))}.")
    return {"tool": "who_knows", "answer": " ".join(parts), "path": path}


def decision_trace(question: str) -> dict:
    _, graph = connect()
    target = _best_node(graph, question, labels=("Decision",))
    if target is None:
        return {
            "tool": "decision_trace",
            "answer": "No decision in the graph matches that question.",
            "path": [],
        }

    people = graph.query(
        """
        MATCH (person:Person)-[rel:PROPOSED|APPROVED]->(decision:Decision {id: $id})
        RETURN person.name, type(rel), decision.name, decision.summary
        ORDER BY type(rel)
        """,
        {"id": target["id"]},
    ).result_set
    cites = graph.query(
        """
        MATCH (decision:Decision {id: $id})-[rel:CITES|ABOUT]->(source)
        RETURN decision.name, type(rel), source.name, labels(source)[0]
        """,
        {"id": target["id"]},
    ).result_set
    chats = graph.query(
        """
        MATCH (chat:Chat)-[:ABOUT]->(decision:Decision {id: $id})
        OPTIONAL MATCH (author:Person)-[:AUTHORED]->(chat)
        RETURN author.name, chat.name, chat.summary
        """,
        {"id": target["id"]},
    ).result_set

    summary = people[0][3] if people else ""
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


def blast_radius(question: str) -> dict:
    _, graph = connect()
    target = _best_node(graph, question, labels=("Service",))
    if target is None:
        return {
            "tool": "blast_radius",
            "answer": "Name a service that exists in the graph, such as PostgreSQL or PullRequestStateService.",
            "path": [],
        }

    edges = graph.query(
        """
        MATCH (child:Service)-[rel:DEPENDS_ON]->(parent:Service)
        RETURN child.id, child.name, parent.id, parent.name
        """
    ).result_set
    dependents = {}
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


def _best_node(graph, question: str, labels: tuple[str, ...]) -> dict | None:
    rows = graph.query(
        """
        MATCH (n)
        WHERE labels(n)[0] IN $labels AND n.name IS NOT NULL
        RETURN n.id, n.name, labels(n)[0], coalesce(n.path, ''), coalesce(n.aliases, ''), coalesce(n.title, '')
        """,
        {"labels": list(labels)},
    ).result_set
    question_l = question.lower()
    tokens = {token for token in re.split(r"[^a-z0-9]+", question_l) if len(token) > 2 and token not in STOPWORDS}
    best = None
    best_score = 0
    for node_id, name, label, path, aliases, title in rows:
        haystack = " ".join((name, path, aliases, title)).lower()
        score = 0
        if name and name.lower() in question_l:
            score += 10 + len(name)
        if path and path.lower() in question_l:
            score += 8 + len(path)
        for token in tokens:
            if token in haystack:
                score += 2
        if label == "Ticket" and "ticket" in question_l:
            score += 6
        if score > best_score:
            best_score = score
            best = {"id": node_id, "name": name, "label": label}
    if best_score < 2:
        return None
    return best


def _first_service(graph) -> dict:
    row = graph.query(
        "MATCH (n:Service) RETURN n.id, n.name, labels(n)[0] ORDER BY n.name LIMIT 1"
    ).result_set[0]
    return {"id": row[0], "name": row[1], "label": row[2]}


def _next_ticket_id(graph) -> str:
    rows = graph.query("MATCH (ticket:Ticket) RETURN ticket.id").result_set
    numbers = []
    for (ticket_id,) in rows:
        match = re.search(r"(\d+)$", str(ticket_id))
        if match:
            numbers.append(int(match.group(1)))
    return f"DEV-{max(numbers, default=900) + 1}"


def _unique(items) -> list[str]:
    seen = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen
