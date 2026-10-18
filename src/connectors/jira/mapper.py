"""Jira JSON to the connector's records, as pure functions: no I/O, no graph.

    project              -> Container (kind 'project')
    issue                -> Item(label='Ticket', name='PAY-12', title=<summary>, state from statusCategory)
    reporter / assignee  -> ExternalUser, with OPENED / ASSIGNED edges to the ticket
    "Blocks" issue link  -> (blocker)-[:BLOCKS]->(blocked), a path edge, never DEPENDS_ON
    components           -> ``about`` hints, resolved to Services by graph.py; the title when there are none

An issue with a security level is never mapped: ``skip_reason`` says so and the connector
deletes it instead, in case it was synced before the level was set. Emails are kept only
when Jira shows them, and only to link a Person by an exact, unique match.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from src.connectors.base import Container, Edge, ExternalUser, Install, Item

SOURCE = "jira"
BLOCKS = "Blocks"
SECURITY = "security level"
OTHER_PROJECT = "other project"


@dataclass
class Mapped:
    """One issue's records: the ticket, the people on it, and every edge that belongs to it."""

    item: Item
    users: list[ExternalUser] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)


def container(install: Install, project: dict, base_url: str) -> Container:
    return Container(
        id=install.container_id(project["id"]),
        name=project.get("name") or project["key"],
        external_id=str(project["id"]),
        url=f"{base_url}/browse/{project['key']}",
        kind="project",
    )


# Jira's accountType for a bot ("Automation for Jira", an installed app's user): never a person.
BOT_ACCOUNT_TYPES = frozenset({"app"})


def user(install: Install, raw: dict | None, people: dict[str, str] | None = None) -> ExternalUser | None:
    """The account, with the email Jira shows, else the one an admin mapped to its accountId
    (``CONNECTOR_JIRA_PEOPLE``). Either way it links to a Person only on a unique email match.
    None for no account, and for a bot (``accountType`` "app"): its tickets are written without it."""
    account_id = (raw or {}).get("accountId")
    if not account_id:
        return None
    account_type = str(raw.get("accountType") or "").strip().lower()
    if account_type in BOT_ACCOUNT_TYPES:
        return None
    shown = (raw.get("emailAddress") or "").strip()
    mapped = ((people or {}).get(account_id) or "").strip()
    return ExternalUser(
        id=install.user_id(account_id),
        source=SOURCE,
        external_id=account_id,
        name=raw.get("displayName") or account_id,
        email=shown or mapped,
        email_from="api" if shown or not mapped else "admin",
        account_type=account_type,
    )


def skip_reason(raw: dict, project: Container) -> str | None:
    """Why an issue is not written: a security level, or it now lives in another project. None to write it."""
    fields = raw.get("fields") or {}
    if fields.get("security"):
        return SECURITY
    if str((fields.get("project") or {}).get("id") or project.external_id) != project.external_id:
        return OTHER_PROJECT
    return None


def issue(install: Install, raw: dict, project: Container, base_url: str, people: dict[str, str] | None = None) -> Mapped:
    fields = raw.get("fields") or {}
    key = raw["key"]
    item_id = install.item_id(raw["id"])
    status = fields.get("status") or {}
    components = tuple(part["name"] for part in fields.get("components") or [] if part.get("name"))
    title = (fields.get("summary") or "").strip()
    props = {
        "project_key": key.rsplit("-", 1)[0],
        "issue_type": (fields.get("issuetype") or {}).get("name") or "",
        "priority": (fields.get("priority") or {}).get("name") or "",
        "parent_key": (fields.get("parent") or {}).get("key") or "",
        "labels": [str(label) for label in fields.get("labels") or []],
        "components": list(components),
    }
    mapped = Mapped(
        Item(
            label="Ticket",
            id=item_id,
            external_id=str(raw["id"]),
            container=project.id,
            name=key,
            title=title,
            url=f"{base_url}/browse/{key}",
            state="closed" if (status.get("statusCategory") or {}).get("key") == "done" else "open",
            status=status.get("name") or "",
            updated_at=updated_at(raw),
            summary=adf_text(fields.get("description")),
            about=components or ((title,) if title else ()),
            props=props,
        )
    )
    for rel, who in (("OPENED", fields.get("reporter")), ("ASSIGNED", fields.get("assignee"))):
        person = user(install, who, people)
        if person is not None:
            if person not in mapped.users:
                mapped.users.append(person)
            mapped.edges.append(Edge(rel, person.id, item_id))
    for blocker, blocked in blocks(raw):
        mapped.edges.append(Edge("BLOCKS", install.item_id(blocker), install.item_id(blocked)))
    return mapped


def blocks(raw: dict) -> list[tuple[str, str]]:
    """``(blocker id, blocked id)`` for each "Blocks" link on the issue, either way round.

    Read from the issue, a link with ``outwardIssue`` says this issue blocks it, and one with
    ``inwardIssue`` says it blocks this issue. Both are returned: the first belongs to this
    issue, the second to the other one, and is only added when this issue's page is written,
    so a link to a ticket synced earlier, or in another project, is not lost.
    """
    pairs = []
    for link in (raw.get("fields") or {}).get("issuelinks") or []:
        kind = link.get("type") or {}
        if kind.get("name") != BLOCKS and (kind.get("outward") or "").lower() != "blocks":
            continue
        if (other := link.get("outwardIssue")) and other.get("id"):
            pairs.append((str(raw["id"]), str(other["id"])))
        elif (other := link.get("inwardIssue")) and other.get("id"):
            pairs.append((str(other["id"]), str(raw["id"])))
    return pairs


def updated_at(raw: dict) -> str:
    """The issue's ``updated`` in UTC (``2026-10-18T04:30:00.000Z``), or "" when Jira sent none."""
    value = (raw.get("fields") or {}).get("updated")
    parsed = parse_time(value)
    return format_time(parsed) if parsed else ""


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def adf_text(node) -> str:
    """Plain text from an Atlassian Document Format description: text, mentions and links, one line per block."""
    if not node:
        return ""
    if isinstance(node, str):
        return node.strip()
    lines: list[str] = []
    current: list[str] = []

    def walk(part: dict) -> None:
        kind = part.get("type")
        attrs = part.get("attrs") or {}
        if kind == "text":
            current.append(part.get("text") or "")
        elif kind == "hardBreak":
            current.append("\n")
        elif kind in ("mention", "emoji", "status", "date"):
            current.append(str(attrs.get("text") or attrs.get("shortName") or attrs.get("timestamp") or ""))
        elif kind in ("inlineCard", "blockCard", "embedCard"):
            current.append(str(attrs.get("url") or ""))
        for child in part.get("content") or []:
            walk(child)
        if kind in ("paragraph", "heading", "listItem", "codeBlock", "blockquote", "rule", "tableRow") and current:
            lines.append("".join(current).strip())
            current.clear()

    walk(node)
    if current:
        lines.append("".join(current).strip())
    return "\n".join(line for line in lines if line)
