"""``onboard team:`` in Slack, and ``onboard_team``, the write it waits on.

    onboard team: Sales | members: @priya @dev | owns: Renewals (process), HubSpot (system, @dev)
        | decision: annual plans only, decided 2026-09-01

One line reads into the same team data a team file holds (team_import.py checks and
merges it), so a team onboarded from Slack and one imported from a file are the same nodes
and edges. The command suspends for an Approve card that lists everything it will create;
``onboard_team`` writes it after the click, in the room's scope, and the reply cites the
edges.

Members are Slack mentions, never names: a mention resolves only through the person its
Slack user is already linked to, and a Slack user with no person yet becomes a new one. A
name is never matched to someone already here, because that link is what a DM's access to
private repos hangs on. Anything the team runs that names no owner is owned by the first
member listed.
"""

import re
from datetime import UTC, datetime
from types import SimpleNamespace

from src.db.falkordb_client import connect
from src.domain.knowledge import catalogue
from src.domain.knowledge.dto import ToolResult, ToolSpec
from src.domain.knowledge.kinds import SERVICE_KINDS, capitalized
from src.domain.onboarding.team_import import (
    NO_COMPANY,
    ORG_SHARED,
    GraphLookup,
    TeamFileError,
    check_team_data,
    counted,
    company_id,
    describe,
    edges,
    import_team_data,
)

TOOL_NAME = "onboard_team"
COMMAND = re.compile(r"^onboard(?:\s+(?:a|the|my|our))?\s+team\s*[:\-]\s*(.+)$", re.IGNORECASE | re.DOTALL)
MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
_ITEM = re.compile(r"^(?P<name>[^()]+?)\s*(?:\((?P<inner>[^()]*)\))?$")
_DECIDED = re.compile(r"^(?P<name>.+?)(?:\s*[,;]?\s*\bdecided(?:\s+on)?\s+(?P<day>\d{4}-\d{2}-\d{2}))?\s*$", re.IGNORECASE | re.DOTALL)
# What each part of the line may be called, and the tool argument it fills.
_PARTS = {"members": "members", "member": "members", "owns": "runs", "runs": "runs", "decision": "decisions", "decisions": "decisions", "summary": "summary"}


def _split(text: str, separator: str) -> list[str]:
    """``text`` split on ``separator`` outside parentheses and outside Slack's ``<...>`` markup."""
    parts, depth, current = [], 0, []
    for char in text:
        if char in "(<":
            depth += 1
        elif char in ")>":
            depth = max(0, depth - 1)
        if char == separator and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return [part.strip() for part in parts]


def command_args(body: str) -> dict:
    """The tool's arguments from the text after ``onboard team:``, mentions kept as ``<@U...>``."""
    parts = _split(body, "|")
    args: dict = {"team": capitalized(parts[0]), "members": [], "runs": [], "decisions": []}
    problems = [] if parts[0] else ["say the team's name first, like `onboard team: Sales | members: @priya`"]
    for part in parts[1:]:
        key, _, value = part.partition(":")
        field = _PARTS.get(key.strip().lower())
        if field is None or not value.strip():
            problems.append(f"I can't read `{part[:60]}`; each part after the name is `members:`, `owns:`, `decision:` or `summary:`")
        elif field == "members":
            args["members"] += [f"<@{user}>" for user in MENTION.findall(value)]
            if MENTION.sub("", value).strip(" ,@"):
                problems.append("mention each member with @ so Slack links them, like `members: @priya @dev`")
        elif field == "runs":
            args["runs"] += [item for item in _split(value, ",") if item]
        elif field == "decisions":
            args["decisions"].append(" ".join(value.split()))
        else:
            args["summary"] = " ".join(value.split())
    if not args["members"] and not problems:
        problems.append("name at least one member, like `members: @priya`")
    if problems:
        raise TeamFileError(problems)
    return args


def mentioned_users(args: dict) -> list[str]:
    """Every Slack user the arguments mention, once, in order."""
    return list(dict.fromkeys(MENTION.findall(" ".join([*args.get("members", []), *args.get("runs", [])]))))


def unlinked_mentions(args: dict, scopes: list[str] | None = None) -> list[str]:
    """The Slack users in the arguments with no person yet: the caller looks up their names."""
    _, graph = connect()
    lookup = GraphLookup(graph, scopes)
    return [user for user in mentioned_users(args) if lookup.linked_person(user) is None]


def _handle_like(name: str | None) -> bool:
    """A Slack login handle rather than a person's name: one lowercase word, like ``amy`` or ``amy.member``."""
    return not name or (" " not in name.strip() and name == name.lower())


def team_data(args: dict, lookup: GraphLookup) -> dict:
    """The arguments as team-file data, each mention resolved to a member row."""
    profiles = args.get("profiles") or {}
    members: dict[str, dict] = {}
    problems = []

    def member(user: str) -> dict | None:
        if user not in members:
            linked = lookup.linked_person(user)
            if linked is not None:
                # A stored name that is only a handle ("amy") gives way to the name Slack shows; any other
                # stored name stays. The caller gives a linked person's real or display name, never the handle.
                stored = linked[1]
                name = profiles.get(user) if profiles.get(user) and _handle_like(stored) else stored
                members[user] = {"id": linked[0], "name": name, "slack_id": user}
            elif profiles.get(user):
                # A new person, never someone already here matched by name.
                members[user] = {"id": f"slack-{user.lower()}", "name": profiles[user], "slack_id": user}
            else:
                problems.append(f"<@{user}> is not in Company Brain yet and I couldn't read their Slack profile")
                return None
        return members[user]

    for mention in args.get("members") or []:
        for user in MENTION.findall(mention):
            member(user)
    first = next(iter(members.values()), None)
    runs = []
    for item in args.get("runs") or []:
        match = _ITEM.match(MENTION.sub(lambda m: f"<@{m.group(1)}>", item.strip()))
        if not match:
            problems.append(f"I can't read `{item[:60]}`; write each thing as `Name (kind)`, like `Renewals (process)`")
            continue
        inner = [token.strip() for token in (match.group("inner") or "").split(",") if token.strip()]
        kinds = [token for token in inner if not MENTION.fullmatch(token)]
        owners = [member(user) for token in inner for user in MENTION.findall(token)]
        owner = owners[0] if owners else first
        row = {"name": " ".join(match.group("name").split()), "kind": kinds[0] if kinds else None}
        if owner:
            row["owner"] = owner["id"]
        runs.append(row)
    decisions = [_decision(text, args, runs, members.get(args.get("asker_slack_user_id") or "")) for text in args.get("decisions") or []]
    if decisions and not runs:
        problems.append("a decision is about what the team runs, so add `owns:` with at least one thing")
    if problems:
        raise TeamFileError(problems)
    team = {"name": args.get("team") or ""}
    if args.get("summary"):
        team["summary"] = args["summary"]
    return {"team": team, "members": list(members.values()), "runs": runs, "decisions": [d for d in decisions if d]}


def _decision(text: str, args: dict, runs: list[dict], asker: dict | None) -> dict | None:
    match = _DECIDED.match(text.strip())
    if not match:
        return None
    decision = {"name": capitalized(match.group("name").strip(" ,;")), "about": [row["name"] for row in runs]}
    day = match.group("day") or _day(args.get("decided_at"))
    if day:
        decision["decided_on"] = day
    if asker:
        decision["proposer"] = asker["id"]
    return decision


def _day(millis) -> str | None:
    try:
        return datetime.fromtimestamp(int(millis) / 1000, UTC).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def check(args: dict, scope: str, scopes: list[str]) -> SimpleNamespace:
    """The team the arguments would write, checked against the graph; ``TeamFileError`` otherwise."""
    _, graph = connect()
    data = team_data(args, GraphLookup(graph, scopes))
    return check_team_data(data, company_id(), scope, scopes)


def card(team: SimpleNamespace) -> str:
    """Everything the import will create, for the Approve card."""
    name = team.TEAMS[0]["name"]
    people = {person["id"]: person["name"] for person in team.PEOPLE}
    parts = [f"{len(people)} {'person' if len(people) == 1 else 'people'} ({_listed(list(people.values()))})"]
    for kind in SERVICE_KINDS:
        things = [s["name"] for s in team.SERVICES if s["kind"] == kind]
        if things:
            parts.append(f"{counted(len(things), kind)} ({_listed(things)})")
    for decision in team.DECISIONS:
        when = f", {_long_day(decision['decided_at'])}" if decision.get("decided_at") else ""
        parts.append(f"the decision *{decision['name']}*{when}")
    owners: dict[str, list[str]] = {}
    for person, rel, thing in team.WORK:
        if rel == "OWNS":
            owners.setdefault(people.get(person, person), []).append(next(s["name"] for s in team.SERVICES if s["id"] == thing))
    text = f"Onboard *{name}*: {', '.join(parts)}"
    if len(owners) == 1:
        text += f". {next(iter(owners))} owns {'it' if len(team.SERVICES) == 1 else 'all of them'}"
    elif owners:
        text += ". Owners: " + "; ".join(f"{who} ({_listed(things)})" for who, things in owners.items())
    return text


def _listed(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _long_day(millis: int) -> str:
    return datetime.fromtimestamp(millis / 1000, UTC).strftime("%-d %b %Y")


def for_slack(problems: list[str]) -> str:
    """The importer's messages, without the team file's section names."""
    text = "; ".join(problems)
    for section, plain in (("[[runs]] '", "'"), ("[[members]] '", "'"), ("[[decisions]] '", "the decision '"), ("[team]", "the team")):
        text = text.replace(section, plain)
    return text


def preview_onboard_team(args: dict, scopes: list[str]) -> str:
    try:
        return card(check(args, ORG_SHARED, scopes))
    except TeamFileError as error:
        if NO_COMPANY in error.problems:
            return NO_COMPANY
        return f"Onboard *{args.get('team') or 'a team'}*, which will be refused: {for_slack(error.problems)}"


def onboard_team(args: dict, scopes: list[str]) -> ToolResult:
    scope = args.get("scope") or ORG_SHARED
    try:
        _, graph = connect()
        data = team_data(args, GraphLookup(graph, scopes))
        result = import_team_data(data, company_id(), scope, scopes)
    except TeamFileError as error:
        if NO_COMPANY in error.problems:
            # The org was never set up; the rest of the line can wait until it is.
            return ToolResult(tool=TOOL_NAME, answer=NO_COMPANY, found=False)
        return ToolResult(tool=TOOL_NAME, answer=f"Not onboarded: {for_slack(error.problems)}.", found=False)
    report = result.report
    name, _, counts = describe(result.team).partition(": ")
    answer = f"Onboarded *{name}*: {counts}. {report.nodes_created} new nodes and {report.edges_created} new edges; nothing was deleted."
    tool_result = ToolResult(tool=TOOL_NAME, answer=answer)
    for step in edges(result.team):
        tool_result.add(step)
    return tool_result


catalogue.register(
    ToolSpec(
        name=TOOL_NAME,
        description=(
            "Add a new team to the graph: its members, what it runs (processes, systems, playbooks, accounts) and its decisions. "
            "Only when the message asks to add or onboard a team, never for a question about one. Writes the graph, so it needs approval."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "team": {"type": "string", "description": "The team's name."},
                "summary": {"type": "string", "description": "What the team does, in a sentence, if the message says."},
                "members": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Each member as the Slack mention in the message, like <@U012ABCDEF>.",
                },
                "runs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"Each thing the team runs as 'Name (kind)', kind one of {', '.join(SERVICE_KINDS)}.",
                },
                "decisions": {"type": "array", "items": {"type": "string"}, "description": "Each decision, as 'what was decided, decided YYYY-MM-DD'."},
            },
            "required": ["team", "members"],
            "additionalProperties": False,
        },
        run=onboard_team,
        needs_approval=True,
        preview=preview_onboard_team,
    )
)
