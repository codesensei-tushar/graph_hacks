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
from src.domain.knowledge import algorithms, kinds, memories, resolve
from src.domain.knowledge.dto import PathStep, TicketCreated, ToolResult
from src.domain.knowledge.resolve import ORG_SHARED, SCOPED, Entity, Resolution, join_names

ANSWER_LABELS = ("Service", "Document", "Ticket")

# How much each edge says about a person's knowledge of a thing. Explainable on purpose:
# PageRank over people comes out flat, because people have almost no incoming edges.
EDGE_WEIGHTS = {"OWNS": 5, "AUTHORED": 3, "WORKED_ON": 2, "REVIEWED": 2, "ASSIGNED": 2, "OPENED": 1, "KNOWS": 1}
DOCUMENT_WEIGHT = 2

_OWNS = re.compile(r"\b(?:owns?|owner|owned|responsible|runs|handles|in charge of)\b", re.IGNORECASE)
_POLICY = re.compile(r"\b(?:policy|policies|playbook|rules?) (?:on|for|about)\b", re.IGNORECASE)
_WIDER_THAN_OWNER = re.compile(r"\b(?:else|also|worked|works|knows?|reviewed|touched)\b", re.IGNORECASE)
_LOAD_BEARING = re.compile(
    r"\b(?:load[- ]bearing|most (?:critical|depended|important|central)|single point of failure|pagerank|central services?)\b", re.IGNORECASE
)
_PATH = re.compile(r"\b(?:connected|connection|path between|related to|linked to|link between|how does .+ reach|route (?:from|between))\b", re.IGNORECASE)
_RELATED_MEMORIES = re.compile(
    r"\b(?:memories|notes|chats?|conversations?|discussions?) (?:about|on|mentioning|around)\b|\bwhat (?:have|has) (?:we|people|anyone) said about\b",
    re.IGNORECASE,
)
_RECALL = re.compile(r"\b(?:remember|recall|what did .+ say|who said|said|discussed|talked about|mentioned)\b", re.IGNORECASE)
_BLAST = re.compile(
    r"\b(?:breaks?|goes down|outage|depends?|dependents?|blast|stops? if|stalls?|is down|happens if|affected if)\b", re.IGNORECASE
)
_DECISION = re.compile(r"\b(?:why|decisions?|decided?)\b", re.IGNORECASE)
_PRESSURE = re.compile(r"\b(?:still the last word|still current|still holds?|still stands|unchallenged|what landed after|left uncited)\b", re.IGNORECASE)
_GROUPS = re.compile(r"\b(?:working groups?|de facto teams?|who (?:actually )?works together|work clusters?)\b", re.IGNORECASE)


def route(question: str) -> tuple[str, dict]:
    """Pick a tool and its arguments from the question's words. The fallback when no model is configured."""
    text = question.strip()
    if not text:
        return "none", {}
    if _GROUPS.search(text):
        return "working_groups", {}
    if _PRESSURE.search(text):
        return "decision_pressure", {"topic": text}
    if _LOAD_BEARING.search(text):
        return "load_bearing", {"limit": algorithms.DEFAULT_LOAD_BEARING}
    if _PATH.search(text):
        return "path_between", {"source": text, "target": ""}
    if _DECISION.search(text):
        # Before the memory routes: "why did we cut vector recall" asks for a decision, not a memory search.
        return "decision_trace", {"topic": text}
    if _RELATED_MEMORIES.search(text):
        return "related_memories", {"name": text}
    if _RECALL.search(text):
        return "recall", {"query": text}
    if _BLAST.search(text):
        args = {"name": text}
        depth = algorithms.depth_in(text)
        if depth is not None:
            args["depth"] = depth
        return "blast_radius", args
    if _POLICY.search(text):
        # "what is the policy on refunds": the policy node, its owner, and what it says.
        return "who_owns", {"name": text}
    if _OWNS.search(text) and not _WIDER_THAN_OWNER.search(text):
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
            RETURN service.id, service.name, type(rel), service.kind
            LIMIT 1
            """,
            {"id": target.id},
        ).result_set
        if rows:
            service = Entity(rows[0][0], rows[0][1], "Service", kinds.kind_or_default(rows[0][3]))
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
    named = kinds.capitalized(kinds.subject(service.name, service.kind))
    if service != target:
        parts.append(f"{target.name} is about {kinds.subject(service.name, service.kind)}.")
    run_by = f"the {join_names([name for _, name in teams])} team{'s' if len(teams) > 1 else ''}"
    if owners and teams and kinds.kind_or_default(service.kind) == "process":
        # "is run by Priya. Run by the Sales team." stutters; one sentence names both.
        parts.append(f"{named} is run by {join_names([name for _, name in owners])} for {run_by}.")
    else:
        if owners:
            parts.append(f"{named} is {kinds.owned(service.kind)} {join_names([name for _, name in owners])}.")
        elif teams:
            parts.append(f"{named} has no named owner.")
        else:
            parts.append(f"{named} is in the graph, and no owner or team is recorded for it.")
        if teams:
            parts.append(f"Run by {run_by}.")
    if kinds.kind_or_default(service.kind) in ("policy", "playbook"):
        summary = graph.ro_query("MATCH (s:Service {id: $id}) RETURN coalesce(s.summary, '')", {"id": service.id}).result_set
        if summary and summary[0][0].strip():
            parts.append(summary[0][0].strip().rstrip(".") + ".")
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

    about: list[str] = []
    if target.label != "Service":
        links = graph.ro_query(
            """
            MATCH (node {id: $id})-[rel:ABOUT|DESCRIBES]->(service:Service)
            RETURN node.name, type(rel), service.id, service.name
            """,
            {"id": target.id},
        ).result_set
        for source, rel, service_id, service in links:
            about.append(service)
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
            # Person to code to service, top to bottom.
            result.add(PathStep(who, rel, pull_request, from_id=who_id, to_id=pr_id, from_label=who_label, to_label="PullRequest"))
            result.add(PathStep(pull_request, "TOUCHES", service, from_id=pr_id, to_id=target.id, from_label="PullRequest", to_label="Service"))
        documents = graph.ro_query(
            """
            MATCH (person:Person)-[:AUTHORED]->(document:Document)-[:DESCRIBES]->(service:Service {id: $id})
            RETURN person.id, person.name, document.id, document.name, service.name
            ORDER BY document.name
            """,
            {"id": target.id},
        ).result_set
        for person_id, person, document_id, document, service in documents:
            result.add(PathStep(person, "AUTHORED", document, from_id=person_id, to_id=document_id, from_label="Person", to_label="Document"))
            result.add(PathStep(document, "DESCRIBES", service, from_id=document_id, to_id=target.id, from_label="Document", to_label="Service"))

    if not result.path:
        result.answer = f"{target.name} is in the graph, and no person is linked to it yet."
        return result

    owners = _unique(step.from_name for step in result.path if step.rel == "OWNS" and step.to_name == target.name)
    open_tickets = _unique(step.from_name for step in result.path if step.rel == "ABOUT" and step.to_name == target.name)
    if target.label != "Service":
        parts = [f"{target.name} is about {join_names(_unique(about))}." if about else f"{target.name} is in the graph."]
    else:
        named = kinds.capitalized(kinds.subject(target.name, target.kind))
        parts = [f"{named} is {kinds.owned(target.kind)} {join_names(owners)}." if owners else f"{named} has no named owner."]
    ranked = rank_people(result.path, target)
    if ranked:
        first, *rest = [f"{name} ({reasons})" for name, _score, reasons in ranked]
        parts.append(f"Ask first: {first}" + (f", then {join_names(rest)}" if rest else "") + ".")
    if open_tickets:
        parts.append(f"Open tickets: {join_names(open_tickets)}.")
    result.answer = " ".join(parts)
    return result


def rank_people(path: list[PathStep], target: Entity) -> list[tuple[str, int, str]]:
    """People in a path, heaviest edges first, each with the reasons in plain words. The path lines are the proof."""
    scores: dict[str, int] = {}
    reasons: dict[str, dict[str, list[str]]] = {}
    for step in path:
        if step.from_label not in ("Person", "GitHubUser"):
            continue
        weight = DOCUMENT_WEIGHT if step.rel == "AUTHORED" and step.to_label == "Document" else EDGE_WEIGHTS.get(step.rel, 1)
        scores[step.from_name] = scores.get(step.from_name, 0) + weight
        verb, what = _reason(step, target)
        objects = reasons.setdefault(step.from_name, {}).setdefault(verb, [])
        if what not in objects:
            objects.append(what)
    ranked = []
    for name in sorted(scores, key=lambda name: (-scores[name], name)):
        # "worked on it and TKT-102, wrote notebook#52": one clause per verb, "it" first.
        clauses = [f"{verb} {join_names(sorted(objects, key=lambda what: what != 'it'))}" for verb, objects in reasons[name].items()]
        ranked.append((name, scores[name], ", ".join(clauses)))
    return ranked


def _reason(step: PathStep, target: Entity) -> tuple[str, str]:
    verbs = {"OWNS": "owns", "WORKED_ON": "worked on", "KNOWS": "knows", "AUTHORED": "wrote", "REVIEWED": "reviewed", "OPENED": "opened", "ASSIGNED": "is assigned"}
    verb = verbs.get(step.rel, step.rel.lower().replace("_", " "))
    if step.to_name == target.name and step.rel in ("OWNS", "WORKED_ON", "KNOWS"):
        return verb, "it"
    return verb, f"the {step.to_name}" if step.to_label == "Document" else step.to_name


def decision_trace(question: str, scopes: list[str]) -> ToolResult:
    """Lives in ``decisions.py`` (current decision, what it replaced, dates). Kept here for the keyword router."""
    from src.domain.knowledge import decisions

    return decisions.decision_trace(question, scopes)


def decision_pressure(question: str, scopes: list[str]) -> ToolResult:
    """Lives in ``decisions.py``. Tickets and pull requests the standing decision does not cite."""
    from src.domain.knowledge import decisions

    return decisions.decision_pressure(question, scopes)


def blast_radius(question: str, scopes: list[str], depth=None) -> ToolResult:
    return algorithms.blast_radius(question, scopes, depth)


def match_service(text: str) -> dict | None:
    """The org-wide service a piece of ingested text is most likely about, or None. A best guess, for classification."""
    entity = resolve.best_match(text, ("Service",), [ORG_SHARED])
    return {"id": entity.id, "name": entity.name, "label": entity.label} if entity else None


_ROUTED = {
    "decision_trace": lambda args, scopes: decision_trace(args["topic"], scopes),
    "decision_pressure": lambda args, scopes: decision_pressure(args["topic"], scopes),
    "working_groups": lambda args, scopes: algorithms.working_groups(scopes),
    "blast_radius": lambda args, scopes: algorithms.blast_radius(args["name"], scopes, args.get("depth")),
    "who_owns": lambda args, scopes: who_owns(args["name"], scopes),
    "who_knows": lambda args, scopes: who_knows(args["name"], scopes),
    "path_between": lambda args, scopes: algorithms.path_between(args["source"], args.get("target") or "", scopes),
    "load_bearing": lambda args, scopes: algorithms.load_bearing(args.get("limit")),
    "related_memories": lambda args, scopes: memories.related_memories(args["name"], scopes, args.get("k")),
    "recall": lambda args, scopes: memories.recall(args["query"], scopes, args.get("k")),
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
