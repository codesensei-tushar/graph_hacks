"""A new joiner's brief on one team: who is on it, what it runs, what stands, who to ask, what is open.

``onboarding_brief`` resolves a team and composes reads that already exist, for any team:

- the team page (``team_page.team_page``): members, what the team runs grouped by kind, the
  standing decisions with what each replaced, and the open tickets;
- ``who_knows`` (``ask_service.knowers``) over each thing the team runs, summed per person
  with the same weights, for who to ask first. Someone outside the team can rank: that is
  who actually knows the team's things.

The path is the page's own edges (``MEMBER_OF``, ``RUNS``, ``OWNS``), the ``ABOUT`` and
``SUPERSEDES`` edges of the decisions told, the ``ABOUT`` edges of the tickets told, and the
edges that rank each person named.
"""

from src.db.falkordb_client import connect
from src.domain.knowledge import ask_service, fallback, kinds, resolve, team_page
from src.domain.knowledge.dto import PathStep, ToolResult
from src.domain.knowledge.resolve import Entity, join_names

TOOL = "onboarding_brief"
MAX_MEMBERS = 6
MAX_PER_KIND = 4
MAX_DECISIONS = 2
MAX_PEOPLE = 3
MAX_TICKETS = 3


def onboarding_brief(question: str, scopes: list[str]) -> ToolResult:
    """What someone joining this team should know first, every fact an edge."""
    _, graph = connect()
    resolution = resolve.resolve_name(question, ("Team",), scopes, graph=graph)
    if not resolution.found:
        miss = ToolResult(tool=TOOL, answer=resolve.describe_miss(resolution, "team"), found=False, candidates=resolution.candidate_names())
        return fallback.before_refusing(question, scopes, miss)
    page = team_page.team_page(resolution.entity.id, scopes)
    result = ToolResult(tool=TOOL, answer="", candidates=resolution.candidate_names())
    result.extend(page.path)
    things = {thing.name: thing for group in page.owns for thing in group.items}

    parts = [_lead(page)]
    if page.owns:
        parts.append("It runs " + "; ".join(_kind_phrase(group) for group in page.owns) + ".")
    else:
        parts.append(f"{page.name} runs nothing in the graph yet.")
    if page.accounts_total:
        parts.append(_member_accounts(page))
    if page.decisions:
        parts.append(_decisions(page.decisions[:MAX_DECISIONS], len(page.decisions), things, result))
    ask = _ask_first(graph, list(things.values()), scopes, result)
    if ask:
        parts.append(ask)
    if page.tickets:
        parts.append(_tickets(page.tickets, things, result))
    result.answer = " ".join(parts)
    return result


def _lead(page: team_page.TeamPage) -> str:
    """"Sales: <summary>. The Sales team is Dev Patel (Account manager), ...", or just the members."""
    team = page.name if page.name.lower().endswith(" team") else f"{page.name} team"
    if not page.members:
        members = f"The {team} has no members in the graph yet."
    else:
        shown = [f"{member.name} ({member.role})" if member.role else member.name for member in page.members[:MAX_MEMBERS]]
        more = len(page.members) - MAX_MEMBERS
        members = f"The {team} is {join_names(shown + ([f'{more} others'] if more > 0 else []))}."
    summary = page.summary.strip()
    if not summary:
        return members
    return f"{page.name}: {summary if summary.endswith('.') else summary + '.'} {members}"


def _kind_phrase(group: team_page.KindGroup) -> str:
    """"2 services: Billing Service (owned by Arjun Mehta) and Invoice Worker (owned by Lena Fischer)"."""
    count = len(group.items)
    noun = group.kind if count == 1 else group.title.lower()
    named = [f"{thing.name} ({kinds.owned(thing.kind)} {join_names(thing.owners)})" if thing.owners else thing.name for thing in group.items[:MAX_PER_KIND]]
    if count > MAX_PER_KIND:
        named.append(f"{count - MAX_PER_KIND} more")
    return f"{count} {noun}: {join_names(named)}"


def _member_accounts(page: team_page.TeamPage) -> str:
    """"Its members also own 37 accounts: Acme Corp (owned by Dev Patel), ... and 33 more." The OWNS edges are in the page's path."""
    total = page.accounts_total
    named = [f"{account.name} (owned by {join_names(account.owners)})" for account in page.accounts[:MAX_PER_KIND]]
    if total > len(named):
        named.append(f"{total - len(named)} more")
    return f"Its members also own {total} account{'s' if total != 1 else ''}: {join_names(named)}."


def _decisions(shown: list[team_page.StandingDecision], total: int, things: dict, result: ToolResult) -> str:
    phrases = []
    for decision in shown:
        phrase = f"*{decision.name}*" + (f" ({decision.decided_on})" if decision.decided_on else "")
        if decision.replaced:
            phrase += f", which replaced *{decision.replaced}*" + (f" ({decision.replaced_on})" if decision.replaced_on else "")
            result.add(PathStep(decision.name, "SUPERSEDES", decision.replaced, from_id=decision.id, from_label="Decision", to_label="Decision"))
        for about in decision.about:
            thing = things.get(about)
            result.add(PathStep(decision.name, "ABOUT", about, from_id=decision.id, to_id=thing.id if thing else None, from_label="Decision", to_label="Service"))
        phrases.append(phrase)
    more = f"; and {total - len(shown)} more" if total > len(shown) else ""
    return f"Standing decision{'s' if total > 1 else ''}: {'; '.join(phrases)}{more}."


def _ask_first(graph, things: list, scopes: list[str], result: ToolResult) -> str:
    """Who knows the team's things, by who_knows weight summed over all of them, with what each knows best."""
    totals: dict[str, int] = {}
    knows: dict[str, list[tuple[int, str]]] = {}
    paths: list[PathStep] = []
    for thing in things:
        entity = Entity(thing.id, thing.name, "Service", thing.kind)
        known = ask_service.knowers(graph, entity, scopes)
        paths.extend(known.path)
        for name, score, _reasons in ask_service.rank_people(known.path, entity):
            totals[name] = totals.get(name, 0) + score
            knows.setdefault(name, []).append((score, thing.name))
    if not totals:
        return ""
    ranked = sorted(totals, key=lambda name: (-totals[name], name))[:MAX_PEOPLE]
    # The page cites an owner from outside the team without an id, so compare cited edges, not steps.
    cited = {step.cite() for step in result.path}
    for step in ask_service.edges_of(paths, ranked):
        if step.cite() not in cited:
            cited.add(step.cite())
            result.add(step)
    named = []
    for name in ranked:
        best = [thing for _score, thing in sorted(knows[name], key=lambda pair: (-pair[0], pair[1]))[:2]]
        named.append(f"{name} ({join_names(best)})")
    first, *rest = named
    return f"Ask first: {first}" + (f", then {join_names(rest)}" if rest else "") + "."


def _tickets(tickets: list[team_page.OpenTicket], things: dict, result: ToolResult) -> str:
    shown = tickets[:MAX_TICKETS]
    phrases = []
    for ticket in shown:
        titled = f"“{ticket.title}”, " if ticket.title and ticket.title != ticket.id else ""
        phrases.append(f"{ticket.id} ({titled}about {ticket.about})")
        thing = things.get(ticket.about)
        result.add(PathStep(ticket.id, "ABOUT", ticket.about, from_id=ticket.id, to_id=thing.id if thing else None, from_label="Ticket", to_label="Service"))
    more = f", and {len(tickets) - len(shown)} more" if len(tickets) > len(shown) else ""
    return f"Open ticket{'s' if len(tickets) > 1 else ''}: {join_names(phrases)}{more}."
