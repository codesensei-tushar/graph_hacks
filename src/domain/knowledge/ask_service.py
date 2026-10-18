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
from src.domain.knowledge import algorithms, bridges, coordination, fallback, find, kinds, memories, resolve, ticket_status
from src.domain.knowledge.dto import PathStep, TicketCreated, ToolResult
from src.domain.knowledge.resolve import ORG_SHARED, SCOPED, Entity, Resolution, join_names

ANSWER_LABELS = ("Service", "Document", "Ticket")

# How much each edge says about a person's knowledge of a thing. Explainable on purpose:
# PageRank over people comes out flat, because people have almost no incoming edges.
EDGE_WEIGHTS = {"OWNS": 5, "AUTHORED": 3, "WORKED_ON": 2, "REVIEWED": 2, "ASSIGNED": 2, "OPENED": 1, "KNOWS": 1}
DOCUMENT_WEIGHT = 2
# A person's account in another tool: GitHub, or any connector's (Jira, HubSpot). It counts as
# its linked Person when it has one, and under its own handle when it does not.
ACCOUNT_LABELS = ("GitHubUser", "ExternalUser")
# What a person's account may have done to a thing. GitHub only ever opens or is assigned.
ACCOUNT_RELS = "OPENED|ASSIGNED|OWNS|WORKED_ON"
_ACCOUNT_ID = "coalesce(person.id, account.id)"
_ACCOUNT_NAME = "coalesce(person.name, CASE WHEN labels(account)[0] = 'GitHubUser' THEN '@' + account.login ELSE account.name END)"
_ACCOUNT_LABEL = "CASE WHEN person IS NOT NULL THEN 'Person' ELSE labels(account)[0] END"
# An unlinked connector account is named with its tool, so it never reads as a person the
# company knows by the same name: "Om Sherikar (Jira)".
SOURCE_NAMES = {"jira": "Jira", "hubspot": "HubSpot"}
# Tickets about a service that who_knows lists: open ones first; then the ones with no sync
# time (seeded, or filed from Slack: few, and written by hand); then the most recently updated.
MAX_TICKETS = 8
_UPDATED = "coalesce(ticket.source_updated_at, ticket.updated_at, '')"
_TICKET_ORDER = (
    f"CASE WHEN coalesce(ticket.state, 'open') = 'closed' THEN 1 ELSE 0 END, CASE WHEN {_UPDATED} = '' THEN 0 ELSE 1 END, {_UPDATED} DESC, ticket.name"
)
# Edges into a ticket that bridges to what was asked about: they count once per person, at the
# heaviest, however many tickets there are, so five assigned tickets never outweigh owning it.
TICKET_RELS = frozenset({"WORKED_ON", "ASSIGNED", "OPENED"})

_OWNS = re.compile(r"\b(?:owns?|owner|owned|responsible|runs|handles|in charge of)\b", re.IGNORECASE)
_POLICY = re.compile(r"\b(?:policy|policies|playbook|rules?) (?:on|for|about)\b", re.IGNORECASE)
_WIDER_THAN_OWNER = re.compile(r"\b(?:else|also|worked|works|knows?|reviewed|touched)\b", re.IGNORECASE)
_LOAD_BEARING = re.compile(
    r"\b(?:load[- ]bearing|most (?:critical|depended|important|central)|single point of failure|pagerank|central services?|matters? (?:the )?most)\b", re.IGNORECASE
)
_PATH = re.compile(
    r"\b(?:connected|connection|path between|related to|linked to|link between|how does .+ reach|route (?:from|between)|path from|what connects|how (?:is|are) .+ (?:related|linked))\b", re.IGNORECASE
)
_RELATED_MEMORIES = re.compile(
    r"\b(?:memories|notes|chats?|conversations?|discussions?) (?:about|on|mentioning|around)\b|\bwhat (?:have|has) (?:we|people|anyone) said about\b",
    re.IGNORECASE,
)
_RECALL = re.compile(r"\b(?:remember|recall|what did .+ say|who said|said|discussed|talked about|mentioned)\b", re.IGNORECASE)
_BLAST = re.compile(
    r"\b(?:breaks?|goes down|outage|depends?|dependents?|rel(?:y|ies) on|blast|stops? if|stalls?|is down|happens if|affected if)\b", re.IGNORECASE
)
# "who approved moving the memory layer" asks for the decision and its people, not who knows the layer.
_DECISION = re.compile(r"\b(?:why|decisions?|decided?)\b|\bwho (?:approved|proposed|signed off on|decided)\b", re.IGNORECASE)
_PRESSURE = re.compile(r"\b(?:still the last word|still current|still holds?|still stands|still valid|unchallenged|what landed after|left uncited)\b", re.IGNORECASE)
_GROUPS = re.compile(
    r"\b(?:working groups?|de facto teams?|who (?:actually )?works together|work clusters?"
    r"|who works with (?:who|whom)|(?:people|who) (?:actually )?work together|collaborates? with (?:who|whom)|who collaborates)\b",
    re.IGNORECASE,
)
# Risk across the company, not one person or one service: "what are our riskiest services",
# "bus factor across the company", "what is undocumented". Before _LEAVES, which also matches
# "bus factor"; "the bus factor on arjun" names a person and stays there.
_RISK = re.compile(
    r"\brisk ?map\b|\briskiest\b|\b(?:biggest|worst|highest) (?:risks?|bus[- ]factor(?: risks?)?)\b"
    r"|\bbus[- ]factor (?:risks?|map|heat ?map|across|overall|company[- ]wide)\b|\bknowledge gaps?\b|\bundocumented\b"
    r"|\b(?:no|missing|without) (?:a )?(?:runbooks?|docs?|documentation)\b|\bone person (?:away )?from an outage\b",
    re.IGNORECASE,
)
# Teams depending on teams, not one service's dependents: "which teams depend on platform",
# "what does payments need from other teams", "cross-team dependencies". Before blast radius,
# which shares "depend on"; "what does the invoice worker depend on" names no team and stays there.
_TEAM_DEPS = re.compile(
    r"\bwhich teams? (?:depends?|rel(?:y|ies)) on\b|\bwho depends on (?:the )?\w+ team\b"
    r"|\bwhat does (?:the )?\w+(?: team)? (?:need|use|depend on|rely on) from (?:other|another) teams?\b"
    r"|\bwhat (?:do|does) (?:the )?\w+ team (?:depend|rely) on\b|\b(?:cross[- ]team|team[- ]level|inter[- ]team) dependenc(?:y|ies)\b"
    r"|\bdependenc(?:y|ies) between teams\b|\bteams? dependenc(?:y|ies)\b",
    re.IGNORECASE,
)
# The team is the subject: what it needs from others, rather than who needs it.
_TEAM_NEEDS = re.compile(
    r"\bwhat does (?:the )?\w+(?: team)? (?:need|use|depend on|rely on) from (?:other|another) teams?\b"
    r"|\bwhat (?:do|does) (?:the )?\w+ team (?:depend|rely) on\b",
    re.IGNORECASE,
)
# Every standing decision audited, not one decision traced: "which decisions are stale", "are any
# decisions in conflict". "is the billing retries decision still the last word" stays decision_pressure.
_DECISION_HEALTH = re.compile(
    r"\b(?:stale|conflicting|contradictory|contradicting|outdated|orphaned|unsupported) decisions?\b"
    r"|\bdecisions? (?:that )?(?:conflict|contradict|clash|are stale|are outdated|lack evidence|need(?:s)? (?:attention|review))\b"
    r"|\bdecision health\b|\b(?:any|which) decisions (?:in conflict|conflict|are stale|are outdated|need)\b|\bhealth of (?:our )?decisions\b",
    re.IGNORECASE,
)
# A person leaving, not a service failing. "who is the only one who knows billing" names a
# service and stays on who_knows; "what is Arjun the only one who knows" names a person, and so
# does "the bus factor on arjun"; "the bus factor for billing" names a service and stays on who_knows.
_LEAVES = re.compile(
    r"\b(?:leaves|leaving|quits?|quitting|resigns?|on leave|hit by a bus|bus factor(?! for\b)|covers? for|takes? over (?:from|for))\b"
    r"|\bwhat\b.*\bthe only (?:one|person) who knows\b"
    r"|\b(?:nobody|no one) else (?:knows|does)\b",
    re.IGNORECASE,
)
# Joining a team, or asking what a team is. "what should I know about billing" names no team and
# stays on who_knows. "what does the payments team own" is the team, not Billing Service, which the
# graph also knows as "payments"; the brief names its people and everything it runs.
_BRIEF = re.compile(
    r"\b(?:just joined|i(?:'m| am) new (?:to|on)|new (?:to|on) the|brief me|onboard me|onboarding brief|up to speed)\b"
    r"|\bwhat should (?:i|a new \w+) know about the \w+(?: \w+)? team\b"
    r"|\bwhat (?:does|do) (?:the )?\w+(?: \w+)? team (?:own|run|do|work on|handle)\b|\bwhat (?:does|do) (?:the )?\w+ own\b"
    r"|\bwho(?:'s| is| are)? (?:on|in) (?:the )?\w+(?: \w+)? team\b|\bmembers of (?:the )?\w+(?: \w+)? team\b"
    r"|\b(?:summari[sz]e|overview of|tell me about|describe) (?:the )?\w+(?: \w+)? team\b",
    re.IGNORECASE,
)
# A change and the people it should reach: "who should know about notebook#45", "who else should
# review it", "who should we loop in". After the decision and memory routes, so "who needs to know
# why we moved off postgres" stays a decision question; before blast radius and owners, which share
# its words. A ticket is asked about, not changed: "who should know about TKT-101" stays on who_knows,
# but "PR-45" is a pull request. A document or ticket the tool resolves is answered as who_knows there.
_SHOULD_KNOW = re.compile(
    r"\bwho (?:else )?(?:should|needs to) (?:know|review|be told|hear)\b|\bwho should (?:i|we) (?:loop in|tell)\b",
    re.IGNORECASE,
)
_TICKET_ID = re.compile(r"\b(?!pr-\d)[a-z]{2,}-\d+\b", re.IGNORECASE)
_TKT_ID = re.compile(r"^TKT-(\d+)$")
# Someone to go to: "who should we ping about the auth outage" wants the people who know Auth,
# not what breaks, so it is routed before blast radius, which shares "outage". Pinging about a
# pull request ("who should I ping about notebook#45") is about a change.
_ASK_WHO = re.compile(r"\bwho should (?:i|we) (?:ping|ask|talk to)\b", re.IGNORECASE)
# People between teams. "how is maya connected to the invoice worker" is a path question.
_CONNECTORS = re.compile(r"\bwho (?:connects|bridges|links)\b|\b(?:bridges?|connectors?|go-betweens?) between\b", re.IGNORECASE)
# Words to look for, not a thing to resolve: "search for webhooks", "look up refunds", "anything about
# PDF layout", "mentions of TKT-102". "search-service" and "search brain" name things and stay where they were.
_SEARCH = re.compile(
    r"^\s*(?:search(?: the graph| everything)? for|look ?up)\b|\banything (?:about|on|mentioning)\b|\bmentions? of\b",
    re.IGNORECASE,
)
# What changed lately, around a thing, a team or the company. A time word is what makes "what
# happened" or "decided" a question about change; "what is new hire onboarding" names a process.
_WHEN = r"(?:recently|lately|today|yesterday|this (?:week|month)|last (?:week|month)|past (?:week|month))"
_CHANGED = re.compile(
    r"\bwhat(?:'s| has)? changed\b|\bwhat'?s new\b(?! hire)|\bwhat is new\b(?= in\b| on\b| for\b| with\b| this\b| since\b|\s*\??$)"
    r"|\brecent changes?\b|\bchangelog\b|\bcatch me up\b|\bwhat did i miss\b"
    rf"|\bwhat happened\b.*\b{_WHEN}\b|\b(?:decided?|decisions?)\b.*\b{_WHEN}\b"
    r"|\b(?:failing|broken|erroring|flaky)\b.*\bchanged?\b",
    re.IGNORECASE,
)
# Everything around one thing: "explore billing", "what do we know about the sales team", "everything
# on TKT-102". After the routes that name a question shape (risk, leaving, briefs, paths, reading
# lists, search), before decisions: "explore the billing retries decision" wants the decision's
# neighbourhood, not its trace. Public: explore strips it from the question before resolving the name.
EXPLORE = re.compile(
    r"\bexplore\b|\bwhat do (?:we|you) know about\b|\beverything (?:we know )?(?:about|on)\b|\bneighbou?rhood of\b|\bwhat(?:'s| is) around\b",
    re.IGNORECASE,
)
_LISTED = r"(?:tickets|issues|accounts|services|systems|processes|documents|docs|pull requests|prs)"
# A list by filter: "which tickets are open", "list the services in payments", "what decisions have we
# made". Before decisions, which shares "decisions"; "what decisions have we made about billing" names
# one thing and stays on decision_trace, as does any "list ... about X".
_FIND = re.compile(
    r"^\s*(?:what|which) decisions (?:have|did) we (?:made|make)\s*\??\s*$"
    r"|\b(?:list|show)(?: me)? (?:all |the |our )?(?:open |closed |merged |standing )?"
    r"(?:decisions|tickets|issues|pull requests|prs|services|systems|processes|playbooks|documents|docs|people|teams)\b(?!.*\babout\b)"
    r"|^\s*(?:what|which) (?:are|is) (?:the |our )?open (?:tickets|issues|pull requests|prs)\b"
    r"|^\s*which (?:tickets|issues|pull requests|prs) are open\b"
    r"|^\s*(?:what|which) (?:tickets|issues) are (?:still )?blocked\b|\bblocked (?:tickets|issues)\b"
    # Whose list: "what tickets are assigned to Om Sherikar", "which accounts does Yash Patil own".
    rf"|^\s*(?:what|which|list|show(?: me)?) (?:all |the )?(?:open |closed )?{_LISTED} (?:are |is )?(?:assigned to|worked on by|owned by|opened by)\b"
    rf"|^\s*(?:what|which) {_LISTED} (?:does|do) .+ (?:own|have|hold|handle|work on)\b"
    # The CRM: "show me our customers", "open opportunities", "deals in contract sent", "the pipeline".
    r"|^\s*(?:(?:show|list|give)(?: me)?|who are|what are|which are|which|what)?\s*(?:all |the |our |my )?(?:open |closed |current )?"
    r"(?:customers|opportunities|accounts|deals|(?:sales )?pipeline)\b"
    r"|\bdeals? (?:are )?(?:in|at) (?:the )?\w",
    re.IGNORECASE,
)
# What to read before a change. "what should I know about billing" stays on who_knows. Public:
# reading_list strips it from the question before resolving the name.
READING_LIST = re.compile(
    r"\b(?:what|which) (?:should|do|must) (?:i|we|you|a new \w+) read\b|\breading list\b|\bread up on\b"
    r"|\b(?:what|which) (?:docs?|documents?|documentation|runbooks?) (?:cover|covers|describe|describes|explain|explains|exist|are there)\b"
    r"|\b(?:what|which) (?:docs?|documents?|documentation|runbooks?) (?:should|do|must) (?:i|we|you|a new \w+) read\b"
    r"|\b(?:docs?|documentation|context) (?:to read )?before (?:i |we )?(?:touch|touching|edit|editing|change|changing|work on|working on)\b",
    re.IGNORECASE,
)


def route(question: str, tickets: bool = True) -> tuple[str, dict]:
    """Pick a tool and its arguments from the question's words. The fallback when no model is configured.

    ``tickets=False`` skips the single-ticket route: ``ticket_status`` asks again that way when the
    key it was given ("q3-2026", "utf-8") is no ticket the asker can read."""
    text = question.strip()
    if not text:
        return "none", {}
    if _GROUPS.search(text):
        return "working_groups", {}
    if _RISK.search(text):
        return "risk_map", {}
    if _TEAM_DEPS.search(text):
        return "team_dependencies", {"name": text, "direction": "dependencies" if _TEAM_NEEDS.search(text) else "dependents"}
    if _DECISION_HEALTH.search(text):
        return "decision_health", {"name": text}
    if _CHANGED.search(text):
        from src.domain.knowledge.what_changed import only_in

        only = only_in(text)
        return "what_changed", {"name": text, **({"only": only} if only else {})}
    if _LEAVES.search(text):
        # Before blast radius: "what breaks if Arjun leaves" is about a person.
        return "if_person_leaves", {"name": text}
    if _BRIEF.search(text):
        return "onboarding_brief", {"name": text}
    if _PRESSURE.search(text):
        return "decision_pressure", {"topic": text}
    if _LOAD_BEARING.search(text):
        return "load_bearing", {"limit": algorithms.DEFAULT_LOAD_BEARING}
    if _CONNECTORS.search(text):
        return "connectors", {"source": text, "target": ""}
    if _PATH.search(text):
        return "path_between", {"source": text, "target": ""}
    if READING_LIST.search(text):
        return "reading_list", {"name": text}
    if _SEARCH.search(text):
        # Before decisions and memories: "search for decisions about refunds" wants every hit, not one decision.
        return "search", {"text": text}
    if _FIND.search(text):
        return "find", find.parse(text) or {"label": "Decision"}
    if EXPLORE.search(text):
        args = {"name": text}
        depth = algorithms.depth_in(text)
        if depth is not None:
            args["depth"] = min(depth, 2)
        return "explore", args
    if tickets and ticket_status.asks_about_one_ticket(text):
        # "what is PAY-12", "is PAY-12 fixed", "who is working on PAY-11", "what blocks PAY-11": the
        # ticket itself, before the people and decision routes that would only list who knows it. A key
        # that is no readable ticket ("billing for q3-2026") falls through to the route below it.
        return "ticket_status", {"name": text}
    if _DECISION.search(text):
        # Before the memory routes: "why did we cut vector recall" asks for a decision, not a memory search.
        return "decision_trace", {"topic": text}
    if _RELATED_MEMORIES.search(text):
        return "related_memories", {"name": text}
    if _RECALL.search(text):
        return "recall", {"query": text}
    about_a_change = _SHOULD_KNOW.search(text) or (_ASK_WHO.search(text) and coordination.pr_reference(text))
    if about_a_change and not _TICKET_ID.search(text):
        return "who_should_know", {"name": text}
    if _ASK_WHO.search(text):
        return "who_knows", {"name": text}
    needs = algorithms.asks_for_dependencies(text)
    if needs or _BLAST.search(text):
        # "what does the invoice worker depend on" walks forward; "what depends on it" stays the default.
        args = {"name": text, "direction": algorithms.DEPENDENCIES} if needs else {"name": text}
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
        "CREATE (ticket:Ticket {id: $id, name: $id, title: $title, summary: $title, scope: $scope, created_at: $now})",
        {"id": ticket_id, "title": title, "scope": scope, "now": _now_millis()},
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
        return fallback.before_refusing(question, scopes, _miss("who_owns", resolution))
    target = resolution.entity

    result = ToolResult(tool="who_owns", answer="", candidates=resolution.candidate_names())
    service = target
    if target.label != "Service":
        rows = graph.ro_query(
            f"""
            MATCH (node {{id: $id}})-[rel:ABOUT|DESCRIBES]->(service:Service)
            WHERE {SCOPED.format(var="service")}
            RETURN service.id, service.name, type(rel), service.kind
            LIMIT 1
            """,
            {"id": target.id, "scopes": scopes},
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
    # A connected tool's owner the company does not know yet (HubSpot): named with its tool.
    unlinked = graph.ro_query(
        f"""
        MATCH (account:ExternalUser)-[:OWNS]->(:Service {{id: $id}})
        WHERE {SCOPED.format(var="account")} AND NOT (account)-[:IS]->(:Person)
        RETURN account.id, account.name, account.source
        ORDER BY account.name
        """,
        {"id": service.id, "scopes": scopes},
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
    for account_id, account, source in unlinked:
        name = account_name(account, "ExternalUser", source)
        owners.append([account_id, name])
        result.add(_step(Entity(account_id, name, "ExternalUser"), "OWNS", service))
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
        # "who knows about refunds" names a topic, not a thing: the graph may still answer.
        return fallback.before_refusing(question, scopes, _miss("who_knows", resolution))
    result = knowers(graph, resolution.entity, scopes)
    result.candidates = resolution.candidate_names()
    return result


def knowers(graph, target: Entity, scopes: list[str]) -> ToolResult:
    """``who_knows`` for a node already resolved. ``if_person_leaves`` ranks successors with it, so both agree."""
    result = ToolResult(tool="who_knows", answer="")
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
    # Accounts in other tools: who opened a ticket or is assigned it (GitHub, Jira), who owns or
    # worked on an account but is not linked to a person (HubSpot).
    account_rows = graph.ro_query(
        f"""
        MATCH (account)-[rel:{ACCOUNT_RELS}]->(node {{id: $id}})
        WHERE labels(account)[0] IN $accounts AND {SCOPED.format(var="account")}
        OPTIONAL MATCH (account)-[:IS]->(person:Person)
        RETURN {_ACCOUNT_ID}, {_ACCOUNT_NAME}, {_ACCOUNT_LABEL}, account.source, type(rel), node.name
        ORDER BY type(rel)
        """,
        {"id": target.id, "accounts": list(ACCOUNT_LABELS), "scopes": scopes},
    ).result_set
    for who_id, who, who_label, source, rel, node in account_rows:
        result.add(PathStep(account_name(who, who_label, source), rel, node, from_id=who_id, to_id=target.id, from_label=who_label, to_label=target.label))

    about: list[str] = []
    open_tickets: list[str] = []
    more_open = 0
    if target.label != "Service":
        links = graph.ro_query(
            f"""
            MATCH (node {{id: $id}})-[rel:ABOUT|DESCRIBES]->(service:Service)
            WHERE {SCOPED.format(var="service")}
            RETURN node.name, type(rel), service.id, service.name
            """,
            {"id": target.id, "scopes": scopes},
        ).result_set
        for source, rel, service_id, service in links:
            about.append(service)
            result.add(PathStep(source, rel, service, from_id=target.id, to_id=service_id, from_label=target.label, to_label="Service"))
        via = graph.ro_query(
            f"""
            MATCH (person:Person)-[rel:OWNS|WORKED_ON|KNOWS]->(service:Service)<-[:DESCRIBES|ABOUT]-(node {{id: $id}})
            WHERE {SCOPED.format(var="service")}
            RETURN person.id, person.name, type(rel), service.id, service.name
            ORDER BY person.name
            """,
            {"id": target.id, "scopes": scopes},
        ).result_set
        for person_id, person, rel, service_id, service in via:
            result.add(PathStep(person, rel, service, from_id=person_id, to_id=service_id, from_label="Person", to_label="Service"))
    else:
        # A synced tracker can hold hundreds of tickets about one service: list a few, open ones first.
        tickets = graph.ro_query(
            f"""
            MATCH (ticket:Ticket)-[:ABOUT]->(service:Service {{id: $id}})
            WHERE {SCOPED.format(var="ticket")}
            WITH ticket, service
            ORDER BY {_TICKET_ORDER}
            LIMIT {MAX_TICKETS}
            OPTIONAL MATCH (person:Person)-[:WORKED_ON]->(ticket)
            RETURN ticket.id, ticket.name, service.name, person.id, person.name, coalesce(ticket.state, 'open') <> 'closed'
            ORDER BY ticket.name
            """,
            {"id": target.id, "scopes": scopes},
        ).result_set
        for ticket_id, ticket, service, person_id, person, is_open in tickets:
            result.add(PathStep(ticket, "ABOUT", service, from_id=ticket_id, to_id=target.id, from_label="Ticket", to_label="Service"))
            if is_open and ticket not in open_tickets:
                open_tickets.append(ticket)
            if person:
                result.add(PathStep(person, "WORKED_ON", ticket, from_id=person_id, to_id=ticket_id, from_label="Person", to_label="Ticket"))
        if len(open_tickets) == MAX_TICKETS:
            total = graph.ro_query(
                f"""
                MATCH (ticket:Ticket)-[:ABOUT]->(:Service {{id: $id}})
                WHERE {SCOPED.format(var="ticket")} AND coalesce(ticket.state, 'open') <> 'closed'
                RETURN count(DISTINCT ticket)
                """,
                {"id": target.id, "scopes": scopes},
            ).result_set
            more_open = max(0, (total[0][0] if total else 0) - len(open_tickets))
        # Who a connected tracker (Jira) has those tickets assigned to. GitHub assignees count on the ticket itself only.
        assigned = graph.ro_query(
            f"""
            MATCH (account:ExternalUser)-[rel:ASSIGNED]->(ticket:Ticket)
            WHERE ticket.id IN $tickets AND {SCOPED.format(var="account")}
            OPTIONAL MATCH (account)-[:IS]->(person:Person)
            RETURN {_ACCOUNT_ID}, {_ACCOUNT_NAME}, {_ACCOUNT_LABEL}, account.source, type(rel), ticket.id, ticket.name
            ORDER BY ticket.name
            """,
            {"tickets": [row[0] for row in tickets], "scopes": scopes},
        ).result_set
        for who_id, who, who_label, source, rel, ticket_id, ticket in assigned:
            result.add(PathStep(account_name(who, who_label, source), rel, ticket, from_id=who_id, to_id=ticket_id, from_label=who_label, to_label="Ticket"))
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
        if target.label == "Ticket":
            ticket = ticket_status.read(graph, target, scopes, ToolResult(tool="who_knows", answer=""))
            result.answer = f"{ticket_status.lead_line(ticket)} No person is linked to it yet."
        else:
            result.answer = f"{target.name} is in the graph, and no person is linked to it yet."
        return result

    owners = _unique(step.from_name for step in result.path if step.rel == "OWNS" and step.to_name == target.name)
    if target.label == "Ticket":
        # A ticket leads with what its tracker shows: title, status, priority and assignee.
        ticket = ticket_status.read(graph, target, scopes, ToolResult(tool="who_knows", answer=""))
        parts = [ticket_status.lead_line(ticket)]
        if about:
            parts.append(f"About {join_names(_unique(about))}.")
    elif target.label != "Service":
        parts = [f"{target.name} is about {join_names(_unique(about))}." if about else f"{target.name} is in the graph."]
    else:
        named = kinds.capitalized(kinds.subject(target.name, target.kind))
        parts = [f"{named} is {kinds.owned(target.kind)} {join_names(owners)}." if owners else f"{named} has no named owner."]
    ranked = rank_people(result.path, target)
    if ranked:
        first, *rest = [f"{name} ({reasons})" for name, _score, reasons in ranked]
        parts.append(f"Ask first: {first}" + (f", then {join_names(rest)}" if rest else "") + ".")
    if open_tickets:
        parts.append(f"Open tickets: {join_names(open_tickets + ([f'{more_open} more'] if more_open else []))}.")
    result.answer = " ".join(parts)
    return result


def account_name(name: str, label: str, source: str | None) -> str:
    """How an account is named in a reply: a person's name, ``@login``, or a connector account with its tool."""
    if label != "ExternalUser":
        return name
    # A connector account synced before names fell back to the email's local part may still be
    # named by its address: an answer never shows one.
    shown = name.split("@", 1)[0].strip() if "@" in (name or "") else name
    return f"{shown or 'unnamed account'} ({SOURCE_NAMES.get(source or '', (source or 'external').capitalize())})"


def rank_people(path: list[PathStep], target: Entity) -> list[tuple[str, int, str]]:
    """People in a path, heaviest edges first, each with the reasons in plain words. The path lines are the proof."""
    return [(name, score, reasons) for _who, name, score, reasons in rank_people_keyed(path, target)]


def rank_people_keyed(path: list[PathStep], target: Entity) -> list[tuple[tuple[str, str], str, int, str]]:
    """``rank_people`` with each person's ``(label, id)`` first, to tell people apart.

    People are told apart by label and id, never by name: an unlinked Jira account named like a
    person the company knows is someone else until it is linked. Work on tickets that lead to the
    target counts once per person, at its heaviest edge.
    """
    scores: dict[tuple[str, str], int] = {}
    ticket_work: dict[tuple[str, str], int] = {}
    names: dict[tuple[str, str], str] = {}
    reasons: dict[tuple[str, str], dict[str, list[str]]] = {}
    for step in path:
        if step.from_label not in ("Person", *ACCOUNT_LABELS):
            continue
        who = (step.from_label, step.from_id or step.from_name)
        names.setdefault(who, step.from_name)
        weight = DOCUMENT_WEIGHT if step.rel == "AUTHORED" and step.to_label == "Document" else EDGE_WEIGHTS.get(step.rel, 1)
        target_id = getattr(target, "id", None)
        leads_to_target = step.to_id != target_id if step.to_id and target_id else step.to_name != target.name
        if step.to_label == "Ticket" and step.rel in TICKET_RELS and leads_to_target:
            ticket_work[who] = max(ticket_work.get(who, 0), weight)
            scores.setdefault(who, 0)
        else:
            scores[who] = scores.get(who, 0) + weight
        verb, what = _reason(step, target)
        objects = reasons.setdefault(who, {}).setdefault(verb, [])
        if what not in objects:
            objects.append(what)
    for who, weight in ticket_work.items():
        scores[who] += weight
    ranked = []
    for who in sorted(scores, key=lambda who: (-scores[who], names[who], who)):
        # "worked on it and TKT-102, wrote notebook#52": one clause per verb, "it" first.
        clauses = [f"{verb} {join_names(sorted(objects, key=lambda what: what != 'it'))}" for verb, objects in reasons[who].items()]
        ranked.append((who, names[who], scores[who], ", ".join(clauses)))
    return ranked


def edges_of(path: list[PathStep], people: list[str]) -> list[PathStep]:
    """The steps that rank these people, each followed by the step that ties it to the target
    ("Lena -[WORKED_ON]-> TKT-102", then "TKT-102 -[ABOUT]-> Billing Service")."""
    steps: list[PathStep] = []
    for step in path:
        if step.from_name in people:
            steps.append(step)
            steps.extend(bridge for bridge in path if bridge.from_name == step.to_name)
    return steps


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


def if_person_leaves(question: str, scopes: list[str]) -> ToolResult:
    """Lives in ``bus_factor.py``, which ranks successors with ``knowers`` here. Kept here for the keyword router."""
    from src.domain.knowledge import bus_factor

    return bus_factor.if_person_leaves(question, scopes)


def risk_map(scopes: list[str]) -> ToolResult:
    """Lives in ``risk_map.py``, which ranks people with ``knowers`` here. Kept here for the keyword router."""
    from src.domain.knowledge import risk_map as risk

    return risk.risk_map(scopes)


def team_dependencies(name: str | None, scopes: list[str], direction: str | None = None) -> ToolResult:
    """Lives in ``org_health.py``, which audits decisions too. Kept here for the keyword router."""
    from src.domain.knowledge import org_health

    return org_health.team_dependencies(name, scopes, direction)


def decision_health(name: str | None, scopes: list[str]) -> ToolResult:
    """Lives in ``org_health.py``, which reads decisions (and so the catalogue). Kept here for the keyword router."""
    from src.domain.knowledge import org_health

    return org_health.decision_health(name, scopes)


def person_profile(question: str, scopes: list[str]) -> ToolResult:
    """Lives in ``person_profile.py``, which reads decisions and the bus factor. Kept here for the keyword router."""
    from src.domain.knowledge import person_profile as profile

    return profile.person_profile(question, scopes)


def onboarding_brief(question: str, scopes: list[str]) -> ToolResult:
    """Lives in ``brief.py``, which reads the team page (and so the ask API). Kept here for the keyword router."""
    from src.domain.knowledge import brief

    return brief.onboarding_brief(question, scopes)


def _now_millis() -> int:
    """The decisions clock, so a filed ticket is dated the way a recorded decision is."""
    from src.domain.knowledge.decisions import now_millis

    return now_millis()


def what_changed(question: str, scopes: list[str], days=None, only=None, person=None) -> ToolResult:
    """Lives in ``what_changed.py``, which follows decisions forward (and so imports the catalogue). Kept here for the keyword router."""
    from src.domain.knowledge import what_changed as changed

    return changed.what_changed(question, scopes, days, only, person)


def reading_list(question: str, scopes: list[str]) -> ToolResult:
    """Lives in ``reading_list.py``, which follows decisions forward (and so imports the catalogue). Kept here for the keyword router."""
    from src.domain.knowledge import reading_list as reading

    return reading.reading_list(question, scopes)


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
    "risk_map": lambda args, scopes: risk_map(scopes),
    "person_profile": lambda args, scopes: person_profile(args["name"], scopes),
    "team_dependencies": lambda args, scopes: team_dependencies(args.get("name"), scopes, args.get("direction")),
    "decision_health": lambda args, scopes: decision_health(args.get("name"), scopes),
    "if_person_leaves": lambda args, scopes: if_person_leaves(args["name"], scopes),
    "onboarding_brief": lambda args, scopes: onboarding_brief(args["name"], scopes),
    "reading_list": lambda args, scopes: reading_list(args["name"], scopes),
    "what_changed": lambda args, scopes: what_changed(args.get("name") or "", scopes, args.get("days"), args.get("only"), args.get("person")),
    "connectors": lambda args, scopes: bridges.connectors(args["source"], args.get("target") or "", scopes),
    "blast_radius": lambda args, scopes: algorithms.blast_radius(args["name"], scopes, args.get("depth"), args.get("direction")),
    "who_should_know": lambda args, scopes: coordination.who_should_know(args["name"], scopes),
    "who_owns": lambda args, scopes: who_owns(args["name"], scopes),
    "who_knows": lambda args, scopes: who_knows(args["name"], scopes),
    "path_between": lambda args, scopes: algorithms.path_between(args["source"], args.get("target") or "", scopes),
    "load_bearing": lambda args, scopes: algorithms.load_bearing(args.get("limit"), scopes),
    "related_memories": lambda args, scopes: memories.related_memories(args["name"], scopes, args.get("k")),
    "recall": lambda args, scopes: memories.recall(args["query"], scopes, args.get("k")),
    "explore": lambda args, scopes: _explore().explore(args["name"], scopes, args.get("depth")),
    "ticket_status": lambda args, scopes: ticket_status.ticket_status(args["name"], scopes),
    "find": lambda args, scopes: find.find(args.get("label"), {key: value for key, value in args.items() if key != "label"}, scopes),
    "search": lambda args, scopes: _search(args["text"], scopes),
}


def _explore():
    """``explore.py`` imports this module for ``EXPLORE``; imported here when a question is routed to it."""
    from src.domain.knowledge import explore

    return explore


def _search(text: str, scopes: list[str]) -> ToolResult:
    """Lives in ``search.py``, which reads decisions (and so the catalogue). Kept here for the keyword router."""
    from src.domain.knowledge import search

    return search.search(text, scopes)


def _miss(tool: str, resolution: Resolution, kind: str = "service, document, or ticket") -> ToolResult:
    return ToolResult(tool=tool, answer=resolve.describe_miss(resolution, kind), found=False, candidates=resolution.candidate_names())


def _step(source: Entity, rel: str, target: Entity) -> PathStep:
    return PathStep(source.name, rel, target.name, from_id=source.id, to_id=target.id, from_label=source.label, to_label=target.label)


def _next_ticket_id(graph) -> str:
    """The next ``TKT-<n>``. Only those ids count: a GitHub issue or a Jira key never moves the numbering."""
    rows = graph.ro_query("MATCH (ticket:Ticket) WHERE ticket.id STARTS WITH 'TKT-' RETURN ticket.id").result_set
    numbers = []
    for (ticket_id,) in rows:
        match = _TKT_ID.match(str(ticket_id))
        if match:
            numbers.append(int(match.group(1)))
    return f"TKT-{max(numbers, default=100) + 1}"


def _unique(items) -> list[str]:
    seen = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen
