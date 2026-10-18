"""General graph tools: everything around one node, or one relationship walked from it.

The specific tools each answer one shape of question. These two answer the rest, and the
agent loop or an MCP client can chain them. The model only picks the node, the relationship
type and the direction; every query here is written by hand and takes them as parameters.

``explore(name, depth)`` resolves any node a question names (a service, person, team,
document, ticket, decision or pull request, or its id) and lists what it is linked to,
grouped by relationship and direction in plain words ("owned by Arjun Mehta; depends on
Auth Service and Postgres"), each edge cited. Depth 2 adds the second ring, counted by
label, with a few of its edges cited. ``neighbors(name, relation, direction, depth)``
follows one relationship type from an allowlist, out, in or both ways, for up to three
hops, grouped by hop.

Both walk hop by hop, one query per hop, so the depth is a loop bound in Python and never
part of a query. The relationship type and the direction travel as parameters (``$rel``,
``$dir``). Every node shown passes the scope clause, and a forgotten memory is never shown.
GitHub and Slack accounts are not things in their own right: a pull request shows the person
who wrote it, through ``IS``, as ``who_knows`` does, and a person's own list includes what
their accounts did. Scopes, tags and workspace records are never shown.

A decision shown is checked with ``decisions.current_decision``, so one that was replaced
says so. A pattern such as ``exists((d)<-[:SUPERSEDES]-())`` is never used for that: on
FalkorDB 4.22.1 it is true for every decision.
"""

import re
from dataclasses import dataclass

from src.db.falkordb_client import connect
from src.domain.knowledge import ask_service, kinds, resolve
from src.domain.knowledge.dto import MemoryLine, PathStep, ToolResult, day_from
from src.domain.knowledge.memories import _snippet as snippet
from src.domain.knowledge.resolve import SCOPED, Entity, Resolution, join_names

EXPLORE = "explore"
NEIGHBORS = "neighbors"

# What a name may resolve to. Persons included: the tool explores anyone, and only the
# refusal fallback hands persons to person_profile instead.
EXPLORABLE = (*resolve.DEFAULT_LABELS, "PullRequest")
# What a walk shows. Accounts (ACCOUNTS) are shown as the person they belong to.
SHOWN = ("Service", "Person", "Team", "Document", "Ticket", "Decision", "PullRequest", "Chat", "Memory", "Repo", "Channel")
ACCOUNTS = ("GitHubUser", "SlackUser", "ExternalUser")
# Edges about identity, access or filing, not about the work. Never shown.
HIDDEN_RELS = ("IS", "CAN_READ", "IN_SCOPE", "HAS_TAG", "FILED_UNDER", "PARENT", "LINKS", "PART_OF")
# The relationship types neighbors may follow. Checked here, then passed as $rel.
RELATIONS = (
    "OWNS", "WORKED_ON", "KNOWS", "MEMBER_OF", "RUNS", "DEPENDS_ON", "AUTHORED", "REVIEWED", "DESCRIBES", "ABOUT",
    "BASED_ON", "CITES", "SUPERSEDES", "PROPOSED", "APPROVED", "TOUCHES", "CLOSES", "MENTIONS", "DECIDED_IN",
    "OPENED", "ASSIGNED", "IN_REPO", "BLOCKS", "REFERENCES",
)
OUT, IN, BOTH = "out", "in", "both"
DIRECTIONS = (OUT, IN, BOTH)

MAX_EXPLORE_DEPTH = 2
MAX_NEIGHBOR_DEPTH = 3
# Names listed per group before "and N more".
GROUP_CAP = 8
# Edges cited per answer, and how many of them may come from the second ring.
PATH_CAP = 40
RING_CITES = 10
MEMORY_LINES = 5

_VISIBLE = SCOPED + " AND NOT coalesce({var}.is_forgotten, false)"
_WANTED = "NOT type(r) IN $hidden AND ($rel IS NULL OR type(r) = $rel)"
_DIRECTED = "($dir = 'both' OR ($dir = 'out' AND startNode(r) = {var}) OR ($dir = 'in' AND endNode(r) = {var}))"

# One hop out of the nodes in $ids. Each returns: the node walked from, the edge type,
# whether the edge points away from it, and the node reached (label, id, name, kind).
_DIRECT = f"""
MATCH (n)-[r]-(m)
WHERE n.id IN $ids AND labels(m)[0] IN $shown AND {_WANTED} AND {_DIRECTED.format(var="n")} AND {_VISIBLE.format(var="m")}
RETURN n.id, type(r), startNode(r) = n, labels(m)[0], m.id, coalesce(m.name, m.id), m.kind
"""
# The node reached is an account: show the person it belongs to, or the account when it has none.
_THROUGH_ACCOUNT = f"""
MATCH (n)-[r]-(a)
WHERE n.id IN $ids AND labels(a)[0] IN $accounts AND {_WANTED} AND {_DIRECTED.format(var="n")} AND {SCOPED.format(var="a")}
OPTIONAL MATCH (a)-[:IS]->(p:Person)
RETURN n.id, type(r), startNode(r) = n, CASE WHEN p IS NULL THEN labels(a)[0] ELSE 'Person' END,
       coalesce(p.id, a.id), coalesce(p.name, '@' + a.login, a.name, a.id), CASE WHEN p IS NULL THEN a.source END
"""
# The node walked from is a person: what their accounts did is theirs.
_OWN_ACCOUNTS = f"""
MATCH (n:Person)<-[:IS]-(a)-[r]-(m)
WHERE n.id IN $ids AND labels(a)[0] IN $accounts AND labels(m)[0] IN $shown AND {_WANTED} AND {_DIRECTED.format(var="a")}
  AND {SCOPED.format(var="a")} AND {_VISIBLE.format(var="m")}
RETURN n.id, type(r), startNode(r) = a, labels(m)[0], m.id, coalesce(m.name, m.id), m.kind
"""
_BY_ID = f"""
MATCH (n {{id: $id}})
WHERE labels(n)[0] IN $labels AND n.name IS NOT NULL AND {SCOPED.format(var="n")}
RETURN n.id, n.name, labels(n)[0], n.kind
"""
_DETAILS = "MATCH (n {id: $id}) RETURN coalesce(n.title, ''), n.state, n.merged, n.decided_at"
_MEMORIES = f"""
MATCH (m:Memory)
WHERE m.id IN $ids AND {_VISIBLE.format(var="m")}
OPTIONAL MATCH (a:SlackUser)-[:SAID]->(m)
RETURN m.id, m.text, m.scope, m.channel_id, m.created_at, m.ts, coalesce(a.name, a.id)
ORDER BY coalesce(m.created_at, 0) DESC
"""

# How a group reads, by edge type and whether the edge points away from the node explored.
PHRASES = {
    ("OWNS", True): "owns", ("OWNS", False): "owned by",
    ("RUNS", True): "runs", ("RUNS", False): "run by",
    ("MEMBER_OF", True): "member of", ("MEMBER_OF", False): "members:",
    ("WORKED_ON", True): "worked on", ("WORKED_ON", False): "worked on by",
    ("KNOWS", True): "knows", ("KNOWS", False): "known by",
    ("DEPENDS_ON", True): "depends on", ("DEPENDS_ON", False): "depended on by",
    ("DESCRIBES", True): "describes", ("DESCRIBES", False): "described by",
    ("AUTHORED", True): "wrote", ("AUTHORED", False): "written by",
    ("ABOUT", True): "about", ("ABOUT", False): "what is about it:",
    # BLOCKS points from the blocker to the blocked: PAY-13 -[BLOCKS]-> PAY-11 reads "blocks PAY-11"
    # from PAY-13 and "blocked by PAY-13" from PAY-11, never the same words both ways.
    ("BLOCKS", True): "blocks", ("BLOCKS", False): "blocked by",
    ("REFERENCES", True): "references", ("REFERENCES", False): "referenced by",
    ("IN_CONTAINER", True): "in", ("IN_CONTAINER", False): "holds",
    ("TOUCHES", True): "changes", ("TOUCHES", False): "changed by",
    ("PROPOSED", True): "proposed", ("PROPOSED", False): "proposed by",
    ("APPROVED", True): "approved", ("APPROVED", False): "approved by",
    ("SUPERSEDES", True): "replaced", ("SUPERSEDES", False): "replaced by",
    ("BASED_ON", True): "based on", ("BASED_ON", False): "the basis of",
    ("CITES", True): "cites", ("CITES", False): "cited by",
    ("DECIDED_IN", True): "decided in", ("DECIDED_IN", False): "where these were decided:",
    ("REVIEWED", True): "reviewed", ("REVIEWED", False): "reviewed by",
    ("REVIEW_REQUESTED", True): "asked to review", ("REVIEW_REQUESTED", False): "review asked of",
    ("OPENED", True): "opened", ("OPENED", False): "opened by",
    ("ASSIGNED", True): "assigned", ("ASSIGNED", False): "assigned to",
    ("CLOSES", True): "closes", ("CLOSES", False): "closed by",
    ("IN_REPO", True): "in", ("IN_REPO", False): "holds",
    ("MENTIONS", True): "mentions", ("MENTIONS", False): "mentioned in",
    ("RECORDED_FROM", True): "recorded from",
    ("SAID", True): "quoted in",
}
# Groups in this order; anything else after, by type.
_ORDER = (
    "OWNS", "RUNS", "MEMBER_OF", "WORKED_ON", "KNOWS", "DEPENDS_ON", "DESCRIBES", "AUTHORED", "ABOUT", "TOUCHES",
    "PROPOSED", "APPROVED", "SUPERSEDES", "BASED_ON", "CITES", "DECIDED_IN", "REVIEWED", "REVIEW_REQUESTED",
    "OPENED", "ASSIGNED", "BLOCKS", "CLOSES", "REFERENCES", "IN_REPO", "IN_CONTAINER", "RECORDED_FROM", "SAID", "MENTIONS",
)
_NOUNS = {
    "Service": ("service", "services"), "Person": ("person", "people"), "Team": ("team", "teams"),
    "Document": ("document", "documents"), "Ticket": ("ticket", "tickets"), "Decision": ("decision", "decisions"),
    "PullRequest": ("pull request", "pull requests"), "Chat": ("chat", "chats"), "Memory": ("memory", "memories"),
    "Repo": ("repo", "repos"), "Channel": ("channel", "channels"), "GitHubUser": ("GitHub account", "GitHub accounts"),
    "SlackUser": ("Slack account", "Slack accounts"),
}
_LABEL_ORDER = {label: index for index, label in enumerate((*EXPLORABLE, *SHOWN, *ACCOUNTS))}
# "the sales team": a team named before "team" is the team, though "payments" is also Billing Service's alias.
_TEAM_WORD = re.compile(r"\bteams?\b", re.IGNORECASE)
# A question that asks for decisions: the fallback leaves it refused unless a decision resolves.
_ASKS_FOR_DECISIONS = re.compile(r"\b(?:decisions?|decided)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Link:
    """One edge from a node walked from (``source``) to the node it reaches."""

    source: str
    rel: str
    outgoing: bool
    label: str
    id: str
    name: str
    kind: str | None = None

    @property
    def node(self) -> Entity:
        return Entity(self.id, self.name, self.label, self.kind)


def links(graph, ids: list[str], scopes: list[str], rel: str | None = None, direction: str = BOTH) -> list[Link]:
    """Every edge one hop out of ``ids`` that the asker may read, accounts shown as their person."""
    params = {
        "ids": list(ids), "scopes": list(scopes), "shown": list(SHOWN), "accounts": list(ACCOUNTS),
        "hidden": list(HIDDEN_RELS), "rel": rel, "dir": direction,
    }
    found: list[Link] = []
    seen: set[Link] = set()
    for query in (_DIRECT, _THROUGH_ACCOUNT, _OWN_ACCOUNTS):
        for source, rel_type, outgoing, label, node_id, name, kind in graph.ro_query(query, params).result_set:
            if label == "ExternalUser":
                # An unlinked connector account: named with its tool, which the query returns in place of a kind.
                name, kind = ask_service.account_name(name, label, kind), None
            link = Link(source, rel_type, bool(outgoing), label, node_id, name, kinds.kind_or_default(kind) if label == "Service" else None)
            if node_id and node_id != source and link not in seen:
                seen.add(link)
                found.append(link)
    return found


def resolve_node(graph, text: str, scopes: list[str]) -> Resolution:
    """The node ``text`` names: an id as given (search returns ids), a team before "team", else any explorable name."""
    text = (text or "").strip()
    if text:
        rows = graph.ro_query(_BY_ID, {"id": text, "labels": list(EXPLORABLE), "scopes": list(scopes)}).result_set
        if rows:
            node_id, name, label, kind = rows[0]
            return Resolution(query=text, entity=Entity(node_id, name, label, kinds.kind_or_default(kind) if label == "Service" else None))
    if _TEAM_WORD.search(text):
        team = resolve.resolve_name(text, ("Team",), scopes, graph=graph)
        if team.found:
            return team
    from src.domain.knowledge import coverage  # coverage reads the connectors' cap; imported when needed

    exact = resolve.resolve_name(text, EXPLORABLE, scopes, graph=graph, partial=False)
    if exact.found:
        return exact
    # A synced project by its key or name ("explore PLAT"): what it holds, before part of another name
    # ("plat" is the start of Platform) is taken for it.
    project = coverage.resolve_container(graph, text, scopes)
    if project is not None:
        return Resolution(query=text, entity=project)
    return resolve.resolve_name(text, EXPLORABLE, scopes, graph=graph)


def _miss(tool: str, resolution: Resolution) -> ToolResult:
    kind = "service, person, team, document, ticket, decision, or pull request"
    return ToolResult(tool=tool, answer=resolve.describe_miss(resolution, kind), found=False, candidates=resolution.candidate_names())


def _clamp(value, ceiling: int) -> int:
    try:
        return max(1, min(ceiling, int(value)))
    except (TypeError, ValueError):
        return 1


# --- naming --------------------------------------------------------------------------------


def subject(node: Entity) -> str:
    """How a sentence opens on the node: "Billing Service", "The Renewals process", "The Sales team", "*Decision*"."""
    if node.label == "Service":
        return kinds.capitalized(kinds.subject(node.name, node.kind))
    if node.label == "Team":
        return f"The {node.name} team"
    if node.label == "Decision":
        return f"*{node.name}*"
    return node.name


def item(node: Entity) -> str:
    """How a list names the node: "Renewals process", "the Sales team", "*Decision*"."""
    if node.label == "Service":
        return kinds.listed(node.name, node.kind)
    if node.label == "Team":
        return f"the {node.name} team"
    if node.label == "Decision":
        return f"*{node.name}*"
    return node.name


def cite(nodes: dict[str, Entity], link: Link) -> PathStep:
    """The edge in its true direction, with ids and labels. A memory is cited by its opening words."""
    source = nodes[link.source]
    shown = snippet(link.name) if link.label == "Memory" else link.name
    if link.outgoing:
        return PathStep(source.name, link.rel, shown, from_id=source.id, to_id=link.id, from_label=source.label, to_label=link.label)
    return PathStep(shown, link.rel, source.name, from_id=link.id, to_id=source.id, from_label=link.label, to_label=source.label)


def plural(label: str) -> str:
    """"tickets", "pull requests", "people": a label as a plural noun."""
    return _NOUNS.get(label, (label.lower(), label.lower() + "s"))[1]


def phrase(rel: str, outgoing: bool, label: str) -> str:
    if (rel, outgoing) == ("ABOUT", False):
        return f"{plural(label)} about it:"
    return PHRASES.get((rel, outgoing)) or rel.lower().replace("_", " ")


def _listing(names: list[str], cap: int = GROUP_CAP) -> str:
    shown = names[:cap]
    if len(names) > cap:
        shown.append(f"{len(names) - cap} more")
    return join_names(shown)


def _count(label: str, number: int) -> str:
    singular, plural = _NOUNS.get(label, (label.lower(), label.lower() + "s"))
    return f"{number} {singular if number == 1 else plural}"


def memory_lines(graph, ids: list[str], scopes: list[str], limit: int = MEMORY_LINES) -> list[MemoryLine]:
    """The memories with these ids, newest first, each dated."""
    if not ids:
        return []
    rows = graph.ro_query(_MEMORIES, {"ids": list(ids), "scopes": list(scopes)}).result_set
    return [
        MemoryLine(text=text or "", learned_at=day_from(created, ts), author=author or "someone", scope=scope or resolve.ORG_SHARED, channel_id=channel)
        for _id, text, scope, channel, created, ts, author in rows[:limit]
    ]


def _replaced(graph, decision_ids: list[str], scopes: list[str]) -> set[str]:
    """The decisions among these that no longer stand: a later decision the asker can read replaced them."""
    from src.domain.knowledge import decisions

    replaced = set()
    for decision_id in decision_ids:
        current = decisions.current_decision(graph, decision_id, scopes)
        if current is not None and current.id != decision_id:
            replaced.add(decision_id)
    return replaced


# --- explore -------------------------------------------------------------------------------


def explore(question: str, scopes: list[str], depth=None) -> ToolResult:
    """Everything around the node ``question`` names, grouped by relationship, every edge cited."""
    _, graph = connect()
    # "explore" and "what do we know about" are not part of the name.
    resolution = resolve_node(graph, ask_service.EXPLORE.sub(" ", question or ""), scopes)
    if not resolution.found:
        return _miss(EXPLORE, resolution)
    return explore_node(graph, resolution.entity, scopes, depth, resolution.candidate_names())


def before_refusing(question: str, scopes: list[str]) -> ToolResult | None:
    """The refusal fallback's explore step: the neighbourhood of a node that is not a person.

    None when nothing resolves, when only candidates come back, when the node is a person
    (person_profile answers those), or when the question asks for decisions and the node
    is not one ("what decisions have we made about postgres" says there are none).
    """
    _, graph = connect()
    resolution = resolve_node(graph, question, scopes)
    if not resolution.found or resolution.entity.label == "Person":
        return None
    if _ASKS_FOR_DECISIONS.search(question) and resolution.entity.label != "Decision":
        return None
    return explore_node(graph, resolution.entity, scopes, 1, resolution.candidate_names())


def explore_node(graph, target: Entity, scopes: list[str], depth=None, candidates=()) -> ToolResult:
    wanted = _clamp(depth, MAX_EXPLORE_DEPTH)
    ring_one = links(graph, [target.id], scopes)
    nodes: dict[str, Entity] = {target.id: target}
    for link in ring_one:
        nodes.setdefault(link.id, link.node)
    result = ToolResult(tool=EXPLORE, answer="", candidates=list(candidates), depth=wanted)
    intro = _intro(graph, target, scopes)

    if not ring_one:
        result.answer = f"{intro}{subject(target) if not intro else 'It'} is in the graph, and nothing you can read is linked to it."
        return result

    replaced = _replaced(graph, sorted({link.id for link in ring_one if link.label == "Decision"}), scopes)
    groups: dict[tuple[str, bool, str], list[Link]] = {}
    for link in ring_one:
        # ABOUT into the node splits by label ("tickets about it", "decisions about it"); every other group is one edge type and direction.
        key = (link.rel, link.outgoing, link.label if (link.rel, link.outgoing) == ("ABOUT", False) or link.label == "Memory" else "")
        groups.setdefault(key, []).append(link)
    ordered = sorted(
        groups.items(),
        key=lambda entry: (_ORDER.index(entry[0][0]) if entry[0][0] in _ORDER else len(_ORDER), entry[0][0], not entry[0][1], entry[0][2]),
    )

    clauses: list[str] = []
    budget = PATH_CAP - (RING_CITES if wanted > 1 else 0)
    for (rel, outgoing, label), members in ordered:
        if label == "Memory":
            lines = memory_lines(graph, [link.id for link in members], scopes)
            result.memories.extend(line for line in lines if line not in result.memories)
            clauses.append(f"{phrase(rel, outgoing, label)} {_count('Memory', len(members))}")
            shown = sorted(members, key=lambda link: link.name)[: len(lines)]
        else:
            shown = sorted(members, key=lambda link: (_LABEL_ORDER.get(link.label, 99), link.name.lower()))
            names = [item(link.node) + (" (replaced)" if link.id in replaced else "") for link in shown]
            clauses.append(f"{phrase(rel, outgoing, label)} {_listing(names)}")
            shown = shown[:GROUP_CAP]
        for link in shown:
            if len(result.path) < budget:
                result.add(cite(nodes, link))

    count = len({link.id for link in ring_one})
    lead = "It" if intro else subject(target)
    sentences = [f"{intro}{lead} has {count} direct connection{'s' if count != 1 else ''}: {'; '.join(clauses)}."]
    if wanted > 1:
        sentences.append(_second_ring(graph, nodes, ring_one, scopes, result))
    result.answer = " ".join(sentence for sentence in sentences if sentence)
    return result


def _intro(graph, target: Entity, scopes: list[str]) -> str:
    """A sentence before the groups, for things with a title or a date. Empty for the rest."""
    if target.label not in ("Decision", "Ticket", "PullRequest"):
        return ""
    rows = graph.ro_query(_DETAILS, {"id": target.id}).result_set
    title, state, merged, decided_at = rows[0] if rows else ("", None, None, None)
    if target.label == "Decision":
        from src.domain.knowledge import decisions

        current = decisions.current_decision(graph, target.id, scopes)
        when = decisions.format_date(decisions.to_millis(decided_at))
        if current is not None and current.id != target.id:
            return f"*{target.name}* was decided on {when} and no longer stands: *{current.name}* does. "
        return f"*{target.name}* was decided on {when} and still stands. "
    status = "merged" if merged else (state or "open").lower()
    return f"{target.name} is “{title}”, {status}. " if title else f"{target.name} is {status}. "


def _second_ring(graph, nodes: dict[str, Entity], ring_one: list[Link], scopes: list[str], result: ToolResult) -> str:
    """Two hops out: what the first ring links to, counted by label, a few edges cited."""
    through = list(dict.fromkeys(link.id for link in ring_one if link.label in EXPLORABLE))
    if not through:
        return "Nothing more two hops out."
    first_reach: dict[str, Link] = {}
    for link in links(graph, through, scopes):
        if link.id not in nodes and link.id not in first_reach:
            first_reach[link.id] = link
    if not first_reach:
        return "Nothing more two hops out."
    by_label: dict[str, int] = {}
    for link in first_reach.values():
        by_label[link.label] = by_label.get(link.label, 0) + 1
    counted = [_count(label, by_label[label]) for label in sorted(by_label, key=lambda label: (_LABEL_ORDER.get(label, 99), label))]
    cited = sorted(first_reach.values(), key=lambda link: (_LABEL_ORDER.get(link.label, 99), link.name.lower()))[:RING_CITES]
    for link in cited:
        if len(result.path) < PATH_CAP:
            result.add(cite(nodes, link))
    total = len(first_reach)
    return f"Two hops out there {'is' if total == 1 else 'are'} {total} more: {join_names(counted)}."


# --- neighbors -----------------------------------------------------------------------------


def neighbors(question: str, relation: str, scopes: list[str], direction: str | None = None, depth=None) -> ToolResult:
    """Follow one relationship type from the node ``question`` names, hop by hop, every edge cited."""
    rel = re.sub(r"[\s-]+", "_", (relation or "").strip()).upper()
    if rel not in RELATIONS:
        lead = f"“{relation}” is not a relationship neighbors follows." if relation and relation.strip() else "Name a relationship to follow."
        return ToolResult(tool=NEIGHBORS, answer=f"{lead} Name one of {join_names(list(RELATIONS))}.", found=False)
    way = (direction or BOTH).strip().lower()
    if way not in DIRECTIONS:
        return ToolResult(tool=NEIGHBORS, answer=f"“{direction}” is not a direction. Use out, in or both.", found=False)
    hops = _clamp(depth, MAX_NEIGHBOR_DEPTH)
    _, graph = connect()
    resolution = resolve_node(graph, question, scopes)
    if not resolution.found:
        return _miss(NEIGHBORS, resolution)
    target = resolution.entity

    nodes: dict[str, Entity] = {target.id: target}
    frontier = [target.id]
    by_hop: list[list[Link]] = []
    for _hop in range(hops):
        reached: dict[str, list[Link]] = {}
        for link in links(graph, frontier, scopes, rel, way):
            if link.id not in nodes:
                reached.setdefault(link.id, []).append(link)
        if not reached:
            break
        for node_id, arrivals in reached.items():
            nodes[node_id] = arrivals[0].node
        by_hop.append([link for arrivals in reached.values() for link in arrivals])
        frontier = list(reached)

    result = ToolResult(tool=NEIGHBORS, answer="", candidates=resolution.candidate_names(), depth=hops)
    lead = {OUT: f"Following {rel} out of", IN: f"Following {rel} into", BOTH: f"Following {rel} either way from"}[way]
    named = kinds.subject(target.name, target.kind) if target.label == "Service" else item(target)
    if not by_hop:
        result.answer = f"{lead} {named}: nothing you can read is linked to it that way."
        return result

    groups = []
    memories: list[str] = []
    for index, hop_links in enumerate(by_hop, start=1):
        distinct = list({link.id: link for link in hop_links}.values())
        names = sorted((item(link.node) for link in distinct if link.label != "Memory"), key=str.lower)
        held = [link.id for link in distinct if link.label == "Memory"]
        memories.extend(held)
        listed = _listing(names, GROUP_CAP + 4) if names else ""
        if held:
            listed = join_names([part for part in (listed, _count("Memory", len(held))) if part])
        groups.append(f"{listed} ({index} hop{'s' if index > 1 else ''})")
        for link in sorted(hop_links, key=lambda link: (link.name.lower(), link.source)):
            if len(result.path) < PATH_CAP:
                result.add(cite(nodes, link))
    result.memories = memory_lines(graph, memories, scopes)
    answer = f"{lead} {named}: {'; then '.join(groups)}."
    if len(by_hop) < hops:
        answer += f" Nothing new past {len(by_hop)} hop{'s' if len(by_hop) > 1 else ''}."
    result.answer = answer
    return result
