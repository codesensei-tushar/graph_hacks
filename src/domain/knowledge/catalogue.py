"""The fixed tool catalogue the agent chooses from.

The agent never writes a query. Each tool here is a parameterised traversal written by
hand; the model only picks a tool and fills in its arguments. ``register`` lets other
modules add tools (a decision writer, for example) without editing this file.
"""

from src.domain.knowledge import algorithms, ask_service, bridges, bus_factor, coordination, explore, find, memories, risk_map, ticket_status
from src.domain.knowledge.dto import ToolResult, ToolSpec
from src.domain.knowledge.kinds import SERVICE_KINDS

_TOOLS: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    """Add a tool, replacing any earlier tool with the same name."""
    _TOOLS[spec.name] = spec
    return spec


def list_tools() -> list[ToolSpec]:
    return list(_TOOLS.values())


def get_tool(name: str) -> ToolSpec | None:
    return _TOOLS.get(name)


def tool_names() -> list[str]:
    return list(_TOOLS)


def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


_NAME = {"type": "string", "description": "The service, document, ticket, or team the question is about, copied from the question."}


def _run_who_owns(args: dict, scopes: list[str]) -> ToolResult:
    return ask_service.who_owns(args["name"], scopes)


def _run_who_knows(args: dict, scopes: list[str]) -> ToolResult:
    return ask_service.who_knows(args["name"], scopes, args.get("context"))


def _run_decision_trace(args: dict, scopes: list[str]) -> ToolResult:
    return decisions.decision_trace(args["topic"], scopes)


def _run_decision_pressure(args: dict, scopes: list[str]) -> ToolResult:
    return decisions.decision_pressure(args["topic"], scopes)


def _run_working_groups(_args: dict, scopes: list[str]) -> ToolResult:
    return algorithms.working_groups(scopes)


def _run_blast_radius(args: dict, scopes: list[str]) -> ToolResult:
    return algorithms.blast_radius(args["name"], scopes, args.get("depth"), args.get("direction"))


def _run_who_should_know(args: dict, scopes: list[str]) -> ToolResult:
    return coordination.who_should_know(args["name"], scopes)


def _run_if_person_leaves(args: dict, scopes: list[str]) -> ToolResult:
    return bus_factor.if_person_leaves(args["name"], scopes)


def _run_onboarding_brief(args: dict, scopes: list[str]) -> ToolResult:
    # Imported here: the brief reads the team page, which imports the ask API, which imports this catalogue.
    from src.domain.knowledge import brief

    return brief.onboarding_brief(args["name"], scopes)


def _run_what_changed(args: dict, scopes: list[str]) -> ToolResult:
    # Imported here: what changed follows decisions forward, and decisions registers into this catalogue.
    from src.domain.knowledge import what_changed

    return what_changed.what_changed(args.get("name") or "", scopes, args.get("days"), args.get("only"), args.get("person"))


def _run_reading_list(args: dict, scopes: list[str]) -> ToolResult:
    # Imported here: the reading list follows decisions forward, and decisions registers into this catalogue.
    from src.domain.knowledge import reading_list

    return reading_list.reading_list(args["name"], scopes)


def _run_connectors(args: dict, scopes: list[str]) -> ToolResult:
    return bridges.connectors(args["source"], args.get("target") or "", scopes)


def _run_path_between(args: dict, scopes: list[str]) -> ToolResult:
    return algorithms.path_between(args["source"], args.get("target") or "", scopes)


def _run_load_bearing(args: dict, scopes: list[str]) -> ToolResult:
    return algorithms.load_bearing(args.get("limit", algorithms.DEFAULT_LOAD_BEARING), scopes)


def _run_risk_map(args: dict, scopes: list[str]) -> ToolResult:
    return risk_map.risk_map(scopes, args.get("team"))


def _run_people_ranking(args: dict, scopes: list[str]) -> ToolResult:
    # people_insights reads decisions, which registers into this catalogue on import.
    from src.domain.knowledge import people_insights

    return people_insights.people_ranking(args.get("measure"), scopes, args.get("team"), args.get("limit"))


def _run_go_to(args: dict, scopes: list[str]) -> ToolResult:
    from src.domain.knowledge import people_insights

    return people_insights.go_to(args["name"], scopes)


def _run_team_dependencies(args: dict, scopes: list[str]) -> ToolResult:
    # org_health reads decisions, which registers into this catalogue on import.
    from src.domain.knowledge import org_health

    return org_health.team_dependencies(args.get("name"), scopes, args.get("direction"))


def _run_decision_health(args: dict, scopes: list[str]) -> ToolResult:
    from src.domain.knowledge import org_health

    return org_health.decision_health(args.get("name"), scopes)


def _run_person_profile(args: dict, scopes: list[str]) -> ToolResult:
    # person_profile reads decisions, which registers into this catalogue on import.
    from src.domain.knowledge import person_profile

    return person_profile.person_profile(args["name"], scopes)


def _run_related_memories(args: dict, scopes: list[str]) -> ToolResult:
    return memories.related_memories(args["name"], scopes, args.get("k", memories.DEFAULT_K))


def _run_recall(args: dict, scopes: list[str]) -> ToolResult:
    return memories.recall(args["query"], scopes, args.get("k", memories.DEFAULT_K))


def _run_search(args: dict, scopes: list[str]) -> ToolResult:
    # Imported here: search reads decisions, which imports this catalogue.
    from src.domain.knowledge import search

    return search.search(args["text"], scopes)


def _run_explore(args: dict, scopes: list[str]) -> ToolResult:
    return explore.explore(args["name"], scopes, args.get("depth"))


def _run_neighbors(args: dict, scopes: list[str]) -> ToolResult:
    return explore.neighbors(args["name"], args.get("relation") or "", scopes, args.get("direction"), args.get("depth"))


def _run_find(args: dict, scopes: list[str]) -> ToolResult:
    return find.find(args.get("label"), {key: value for key, value in args.items() if key != "label"}, scopes)


def _find_schema() -> dict:
    """``label`` and one optional property per filter, so the model sees each label's allowlist."""
    by_filter: dict[str, list[str]] = {}
    for label, filters in find.FILTERS.items():
        for name in filters:
            by_filter.setdefault(name, []).append(label)
    words = {
        "kind": ("What kind of service.", {"enum": list(SERVICE_KINDS)}),
        "team": ("The team, copied from the question: the services it runs, the tickets, decisions and documents about them, or its members.", {}),
        "owner": ("The person who owns the services or accounts listed, a Jira or HubSpot user by name, or none for the services nobody owns.", {}),
        "state": ("open or closed for tickets and accounts; open, closed or merged for pull requests.", {"enum": ["open", "closed", "merged"]}),
        "about": ("The service the tickets or decisions are about.", {}),
        "person": ("The person who worked on or is assigned the tickets, or a Jira user by name.", {}),
        "blocked": ("true for the open tickets an open ticket blocks, false for the rest.", {}),
        "project": ("A synced tracker project, by its key (PLAT) or name.", {}),
        "status": ("An account's CRM lifecycle stage: customer, opportunity, lead, ...", {}),
        "stage": ("A deal stage on the account (contract sent, qualified to buy, ...), or open for any deal not closed.", {}),
        "standing": ("true for the decisions that stand today, false for the ones replaced.", {}),
        "proposer": ("The person who proposed the decisions.", {}),
        "approver": ("The person who approved the decisions.", {}),
        "touches": ("The service the pull requests change.", {}),
        "author": ("The person who wrote the documents or pull requests.", {}),
        "reviewer": ("The person who reviewed the pull requests.", {}),
        "describes": ("The service the documents describe.", {}),
        "holds": ("The service the people own, worked on or know.", {}),
    }
    properties: dict = {
        "label": {
            "type": "string",
            "enum": list(find.LABELS),
            "description": "What to list. Filters by label: " + "; ".join(f"{label}: {', '.join(filters) or 'none'}" for label, filters in find.FILTERS.items()) + ".",
        }
    }
    for name in find.FILTER_NAMES:
        text, extra = words[name]
        properties[name] = {"type": "boolean" if name in ("standing", "blocked") else "string", **extra, "description": f"{text} For {', '.join(by_filter[name])}."}
    properties["limit"] = {
        "type": "integer", "minimum": 1, "maximum": find.MAX_ROWS,
        "description": "How many to name; decisions come newest first, so 1 is the newest decision.",
    }
    return _schema(properties, ["label"])


def _run_ingest_ticket(args: dict, scopes: list[str]) -> ToolResult:
    title = args["title"]
    created = ask_service.ingest_ticket(title, args.get("about") or "", args["scope"])
    if created.about_name is None:
        return ToolResult(
            tool="ingest_ticket",
            answer=(
                f"Filed *{created.id}*, not linked to a service: nothing in the graph matched “{title}”. "
                f"Say `ticket: {title} about <service>` to link it."
            ),
        )
    follow = ask_service.who_knows(created.about_name, scopes)
    result = ToolResult(tool="ingest_ticket", answer=f"Filed *{created.id}* about {created.about_name}. {follow.answer}")
    result.extend(created.path)
    result.extend(follow.path)
    return result


def _preview_ingest_ticket(args: dict, _scopes: list[str]) -> str:
    title = args["title"]
    target = ask_service.ticket_target(title, args.get("about") or "")
    if target is None:
        return f"File a ticket titled *{title}* in the company graph, not linked to any service: nothing in the graph matches it"
    return f"File a ticket titled *{title}* in the company graph, linked to *{target.name}*"


register(
    ToolSpec(
        name="who_owns",
        description="Who owns a service (or the service a document or ticket is about), and which team runs it.",
        args_schema=_schema({"name": _NAME}, ["name"]),
        run=_run_who_owns,
    )
)
register(
    ToolSpec(
        name="who_knows",
        description=(
            "Who to ask about a service, document, or ticket: its owner, and the people who worked on it, "
            "wrote or reviewed pull requests touching it, filed tickets about it, or are marked as knowing it, "
            "ranked by weighted edges (owns 5, wrote a pull request 3, worked on, reviewed, or assigned 2, knows or opened 1)."
        ),
        args_schema=_schema({"name": _NAME}, ["name"]),
        run=_run_who_knows,
    )
)
register(
    ToolSpec(
        name="decision_trace",
        description="Why a decision was made: who proposed and approved it, and the documents, tickets, and conversations it came from.",
        args_schema=_schema(
            {"topic": {"type": "string", "description": "The decision or the thing it was about, copied from the question."}},
            ["topic"],
        ),
        run=_run_decision_trace,
    )
)
register(
    ToolSpec(
        name="decision_pressure",
        description=(
            "Whether the standing decision is still the last word: tickets and pull requests about the same "
            "service that the decision does not cite. Use when the question asks if a decision still holds, "
            "not when it asks why the decision was made."
        ),
        args_schema=_schema(
            {"topic": {"type": "string", "description": "The decision the question is checking, copied from the question."}},
            ["topic"],
        ),
        run=_run_decision_pressure,
    )
)
register(
    ToolSpec(
        name="working_groups",
        description=(
            "Who actually clusters together from shared work. Label propagation over people who share a service, "
            "and where that differs from the teams on the org chart."
        ),
        args_schema=_schema({}, []),
        run=_run_working_groups,
    )
)
register(
    ToolSpec(
        name="blast_radius",
        description=(
            "What stops working if a service goes down: the services that depend on it, following DEPENDS_ON edges "
            "for a number of hops, with the main connector: the one most of the others reach it through. With "
            "direction 'dependencies', the other way: what the service itself depends on (\"what does the invoice "
            "worker depend on\", \"billing's dependencies\")."
        ),
        args_schema=_schema(
            {
                "name": _NAME,
                "depth": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": algorithms.MAX_DEPTH,
                    "description": "How many dependency hops to follow when the question says (one hop, two hops out). Omit to follow all.",
                },
                "direction": {
                    "type": "string",
                    "enum": [algorithms.DEPENDENTS, algorithms.DEPENDENCIES],
                    "description": "'dependents' (the default): what depends on the service. 'dependencies': what the service depends on.",
                },
            },
            ["name"],
        ),
        run=_run_blast_radius,
    )
)
register(
    ToolSpec(
        name="who_should_know",
        description=(
            "Who should know about a change but wasn't involved: owners of what a pull request or service change touches "
            "and of everything that depends on it, up to two hops, minus the author and reviewers, with whether each has "
            "worked with the author before."
        ),
        args_schema=_schema(
            {
                "name": {
                    "type": "string",
                    "description": "The pull request (notebook#45, #45 or PR 45) or the service that changes, copied from the question.",
                }
            },
            ["name"],
        ),
        run=_run_who_should_know,
    )
)
register(
    ToolSpec(
        name="if_person_leaves",
        description=(
            "What rests on one person alone if they leave: the things they are the only owner of or the only one "
            "who knows (services, processes, accounts, anything a team owns), the standing decisions only they "
            "proposed or approved, and the open tickets only they worked on, ranked by how much depends on each. "
            "Use when the question names a person leaving, quitting, or away, or asks what someone is the only one "
            "who knows. For a service going down, use blast_radius."
        ),
        args_schema=_schema(
            {"name": {"type": "string", "description": "The person the question is about, copied from the question."}},
            ["name"],
        ),
        run=_run_if_person_leaves,
    )
)
register(
    ToolSpec(
        name="onboarding_brief",
        description=(
            "A brief for someone joining a team, any team: who is on it, what it runs grouped by kind (services, "
            "systems, processes, playbooks, accounts), its standing decisions and what each replaced, who to ask "
            "first (who_knows summed over everything the team runs), and its open tickets. Use when someone says "
            "they just joined a team, asks to be briefed on or brought up to speed on a team, or asks what a team owns, runs or works on."
        ),
        args_schema=_schema(
            {"name": {"type": "string", "description": "The team the question is about, copied from the question."}},
            ["name"],
        ),
        run=_run_onboarding_brief,
    )
)
register(
    ToolSpec(
        name="reading_list",
        description=(
            "What to read before working on or changing a service, system, or process: the documents that describe "
            "it and everything it depends on, nearest first, the standing decisions about them and what each is based "
            "on, and what nothing documents, with who to ask. Use for 'what should I read before…', 'reading list "
            "for…', 'what docs cover…'. For who to ask about it, use who_knows; for a whole team, onboarding_brief."
        ),
        args_schema=_schema(
            {"name": {"type": "string", "description": "The service, system, or process the question is about, copied from the question."}},
            ["name"],
        ),
        run=_run_reading_list,
    )
)
register(
    ToolSpec(
        name="what_changed",
        description=(
            "What changed lately, as a dated digest, newest first: decisions made (and what they replaced), pull "
            "requests opened or merged, tickets filed or updated, and what people said, grouped by the thing they are "
            "about. For one service it also reads what that service depends on, up to three hops, so a failure is "
            "traced to the change upstream; for a team, everything it runs; with nothing named, the whole company. Use "
            "for 'what changed this week', 'what's new in payments', 'billing is failing, what changed', 'what did "
            "sales decide recently'. A service a search hit named is a good name to pass. For why one decision was made, "
            "use decision_trace; for a list of what exists now (the open tickets, every decision), not this."
        ),
        args_schema=_schema(
            {
                "name": {"type": "string", "description": "The service or team the question is about, copied from the question; omit for the whole company."},
                "days": {"type": "integer", "minimum": 1, "maximum": 365, "description": "How far back, when the question says (today 1, this week 7, this month 30, recently 14)."},
                "only": {"type": "string", "enum": ["decisions", "tickets", "pull_requests", "memories"], "description": "One kind of change inside the window, when the question asks for one."},
                "person": {"type": "string", "description": "Whose things to read instead of a team or service: a person the question names ('what changed around Arjun's things'). Leave out otherwise."},
            },
            [],
        ),
        run=_run_what_changed,
    )
)
register(
    ToolSpec(
        name="connectors",
        description=(
            "Who connects two teams, or one team to the rest: people on one team who own or worked on what another "
            "team runs, or who are on both, ranked by how many cross-team edges they have, each with the edges that "
            "make the bridge. Use for questions like who connects Sales and Engineering or who bridges two teams. "
            "For how two particular things are connected, use path_between."
        ),
        args_schema=_schema(
            {
                "source": {"type": "string", "description": "The first team named, copied from the question."},
                "target": {"type": "string", "description": "The second team named, if any, copied from the question."},
            },
            ["source"],
        ),
        run=_run_connectors,
    )
)
register(
    ToolSpec(
        name="path_between",
        description="Why two things are connected: the shortest route between two services, people, teams, documents, tickets, or decisions.",
        args_schema=_schema(
            {
                "source": {"type": "string", "description": "The first thing named, copied from the question."},
                "target": {"type": "string", "description": "The second thing named, copied from the question."},
            },
            ["source", "target"],
        ),
        run=_run_path_between,
    )
)
register(
    ToolSpec(
        name="load_bearing",
        description="Which services the company leans on most: PageRank over DEPENDS_ON, with how many services depend on each.",
        args_schema=_schema(
            {"limit": {"type": "integer", "minimum": 1, "maximum": algorithms.MAX_LOAD_BEARING, "description": "How many services to list."}},
            [],
        ),
        run=_run_load_bearing,
    )
)
register(
    ToolSpec(
        name="risk_map",
        description=(
            "Where risk concentrates across the company: every owned thing (service, system, process, playbook, account) "
            "ranked by how much depends on it times its gaps: only one person knows it, no document describes it, or its "
            "owner is on a different team from the one that runs it. Use for the riskiest things, bus-factor risk across "
            "the company, knowledge gaps or what is undocumented. For one person leaving use if_person_leaves; for what "
            "one service takes down, blast_radius; for dependency weight alone, load_bearing."
        ),
        args_schema=_schema(
            {"team": {"type": "string", "description": "A team to narrow the map to what it runs, copied from the question. Leave it out for the whole company."}},
            [],
        ),
        run=_run_risk_map,
    )
)
register(
    ToolSpec(
        name="person_profile",
        description=(
            "Who a person is and what they hold: role and team, the things they own, work on or know (with how much "
            "depends on each), documents they wrote, their pull requests and reviews, open tickets, the decisions they "
            "proposed or approved and whether each still stands, who they work with most, and what rests on them alone. "
            "Use for 'who is X', 'what does X own / work on / know', 'what is X working on', 'who does X work with', "
            "'what has X decided'. X may be someone only Jira or HubSpot knows. For X leaving, use if_person_leaves; "
            "for who knows a service, who_knows."
        ),
        args_schema=_schema(
            {"name": {"type": "string", "description": "The person the question is about, copied from the question."}},
            ["name"],
        ),
        run=_run_person_profile,
    )
)
register(
    ToolSpec(
        name="team_dependencies",
        description=(
            "Which teams depend on which, through what they run: cross-team DEPENDS_ON links, mutual dependencies, "
            "and a company view. For one service's dependents use blast_radius."
        ),
        args_schema=_schema(
            {
                "name": {"type": "string", "description": "The team the question is about, copied from the question. Leave it out for the whole company."},
                "direction": {
                    "type": "string",
                    "enum": ["dependents", "dependencies"],
                    "description": "dependents: the teams that depend on this one (the default). dependencies: what this team needs from other teams.",
                },
            },
            [],
        ),
        run=_run_team_dependencies,
    )
)
register(
    ToolSpec(
        name="decision_health",
        description=(
            "Audit of standing decisions: no evidence, no approver, two standing decisions about the same thing, uncited "
            "later work, age, plus what replaced what. For one decision, use decision_pressure or decision_trace."
        ),
        args_schema=_schema(
            {"name": {"type": "string", "description": "A service or team to narrow the audit to, copied from the question. Leave it out for every decision."}},
            [],
        ),
        run=_run_decision_health,
    )
)
register(
    ToolSpec(
        name="people_ranking",
        description=(
            "Who leads on one measure across the company, or within a team: holds (things they own, work on or know), "
            "owns, reviews, authored (pull requests and documents), decisions (proposed or approved), load (things "
            "owned, open tickets and open pull requests now) or bridges (other teams whose things they hold). Use for "
            "'who reviewed the most', 'who is the busiest', 'who owns the most', 'top contributors'. For one person, "
            "use person_profile."
        ),
        args_schema=_schema(
            {
                "measure": {"type": "string", "enum": ["holds", "owns", "reviews", "authored", "decisions", "load", "bridges"], "description": "What to count per person."},
                "team": {"type": "string", "description": "A team to narrow the field to its members, copied from the question. Leave it out for everyone."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "description": "How many people to name (3 by default)."},
            },
            ["measure"],
        ),
        run=_run_people_ranking,
    )
)
register(
    ToolSpec(
        name="go_to",
        description=(
            "Who to ask about a service, document or ticket now, with dates: who_knows' ranking, each person with their "
            "last dated touch on it (a pull request, something they said, a decision, a ticket), and the most recent "
            "touch by anyone. Use for 'who is the go-to for X right now', 'who last worked on X', 'when did we last "
            "touch X'. For everyone linked to it without dates, use who_knows."
        ),
        args_schema=_schema({"name": _NAME}, ["name"]),
        run=_run_go_to,
    )
)
register(
    ToolSpec(
        name="related_memories",
        description=(
            "What has been said in chat about a service, document, ticket, or decision: memories that mention it, "
            "then memories about things one hop away, each with the day it was learned."
        ),
        args_schema=_schema(
            {
                "name": _NAME,
                "k": {"type": "integer", "minimum": 1, "maximum": memories.MAX_K, "description": "How many memories to return."},
            },
            ["name"],
        ),
        run=_run_related_memories,
    )
)
register(
    ToolSpec(
        name="search",
        description=(
            "Search what people wrote and said for words that name no service, document, ticket, person or team "
            "(\"who knows about refunds\", \"search for webhooks\", \"anything about PDF layout\"): full-text over the "
            "documents, tickets, pull requests, decisions, and chat memories the asker can read, then the people who wrote, "
            "worked on, reviewed, proposed, approved, or said each hit, ranked by those edges, with the thing each is about. "
            "Prefer who_knows for a named service, document, or ticket, explore for a named node's neighbourhood, and "
            "recall for chat memories alone. Each hit names the service it is about: pass that name to what_changed, "
            "reading_list or who_knows next."
        ),
        args_schema=_schema(
            {"text": {"type": "string", "description": "The words to search for, copied from the question (refunds, PDF layout, webhooks)."}},
            ["text"],
        ),
        run=_run_search,
    )
)
register(
    ToolSpec(
        name="explore",
        description=(
            "Everything the graph links to one thing: a service, person, team, document, ticket, decision or pull "
            "request (or a node id another tool returned), grouped by relationship (owned by, depends on, described "
            "by, tickets about it, replaced by), every edge cited; depth 2 adds what lies two hops out, counted. Use "
            "for a node no specific tool covers, or to see everything around it (\"explore billing\", \"what do we "
            "know about the sales team\"); for owners use who_owns or who_knows, for outages blast_radius, for a "
            "person person_profile."
        ),
        args_schema=_schema(
            {
                "name": {"type": "string", "description": "The thing to explore, copied from the question, or a node id."},
                "depth": {"type": "integer", "minimum": 1, "maximum": 2, "description": "1 for what it links to directly (the default); 2 adds the next ring."},
            },
            ["name"],
        ),
        run=_run_explore,
    )
)
register(
    ToolSpec(
        name="ticket_status",
        description=(
            "One ticket as its tracker shows it: title, open or closed with the tracker's status, priority, type, who it "
            "is assigned to and who opened it, what it is about, what blocks it and what it blocks, and the pull requests "
            "that reference it. Use when the question names one ticket by its key (PAY-12, TKT-101) and asks what it is, "
            "its status, whether it is fixed, who is working on it, or what blocks it."
        ),
        args_schema=_schema({"name": {"type": "string", "description": "The ticket's key, copied from the question (PAY-12)."}}, ["name"]),
        run=lambda args, scopes: ticket_status.ticket_status(args["name"], scopes),
    )
)
register(
    ToolSpec(
        name="find",
        description=(
            "Lists by filter: open or blocked tickets, the tickets assigned to someone, the accounts someone owns, the "
            "tickets or decisions about a service, a team's services, systems or processes, the standing decisions, pull requests touching a service, documents describing it, people on "
            "a team (\"which tickets are open\", \"list the services in payments\", \"what decisions have we "
            "made\"). Each label takes its own filters; every value is matched to the graph first. For what changed "
            "recently use what_changed; for one decision's reasons, decision_trace."
        ),
        args_schema=_find_schema(),
        run=_run_find,
    )
)
register(
    ToolSpec(
        name="neighbors",
        description=(
            "Follow one relationship type, chosen by the caller, from one thing: out along the edge, in against it, or "
            "both, for 1 to 3 hops, grouped by hop with every edge cited (who a team's people are, what cites a "
            "document, what a pull request closes). For dependencies prefer blast_radius; for everything around a "
            "thing, explore."
        ),
        args_schema=_schema(
            {
                "name": {"type": "string", "description": "The thing to start from, copied from the question, or a node id."},
                "relation": {"type": "string", "enum": list(explore.RELATIONS), "description": "The relationship type to follow."},
                "direction": {
                    "type": "string",
                    "enum": list(explore.DIRECTIONS),
                    "description": "'out' follows the edge from the thing (Billing Service DEPENDS_ON out: what it depends on), 'in' against it, 'both' either way (the default).",
                },
                "depth": {"type": "integer", "minimum": 1, "maximum": 3, "description": "How many hops to follow (1 by default)."},
            },
            ["name", "relation"],
        ),
        run=_run_neighbors,
    )
)
register(
    ToolSpec(
        name="recall",
        description=(
            "Search memory text for a free-form question. Only when the question names no service, document, ticket, "
            "person, or decision; otherwise use a graph tool."
        ),
        args_schema=_schema(
            {
                "query": {"type": "string", "description": "The words to search for, copied from the question."},
                "k": {"type": "integer", "minimum": 1, "maximum": memories.MAX_K, "description": "How many memories to return."},
            },
            ["query"],
        ),
        run=_run_recall,
    )
)
register(
    ToolSpec(
        name="ingest_ticket",
        description="File a ticket in the company graph and link it to the service it is about. Writes the graph, so it needs approval.",
        args_schema=_schema(
            {
                "title": {"type": "string", "description": "The ticket title."},
                "about": {"type": "string", "description": "The service the ticket is about, if the title does not say."},
            },
            ["title"],
        ),
        run=_run_ingest_ticket,
        needs_approval=True,
        preview=_preview_ingest_ticket,
    )
)

# Registers record_decision and owns decision_trace; imported last so the catalogue above exists.
from src.domain.knowledge import decisions  # noqa: E402, F401

# Registers onboard_team, the write behind `onboard team:` in Slack.
from src.domain.onboarding import onboard_command  # noqa: E402, F401
