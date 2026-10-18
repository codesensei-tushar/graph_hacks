import json
import re
from dataclasses import dataclass
from types import ModuleType

from src.db.falkordb_client import connect, graph_name
from src.domain.knowledge.kinds import NOT_DEPENDABLE, normalize_kind
from src.seed import company_brain

# Labels, relationship types and property names are written into the query text, so each
# one must be a plain identifier. Seed modules set them in code and the team importer
# copies only its own field names, so this only ever trips on a bug, never on a file.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
WORK_RELATIONS = frozenset({"OWNS", "WORKED_ON", "KNOWS"})


def _identifier(name: str) -> str:
    if not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"{name!r} is not a plain identifier")
    return name


@dataclass
class SeedReport:
    """What one seed run did to the graph. Every write is a MERGE by id, so a second run of
    the same seed creates nothing and sets nothing; ``nodes`` is the graph's total after."""

    company: str
    nodes: int = 0
    nodes_created: int = 0
    edges_created: int = 0
    properties_set: int = 0

    def count(self, result) -> None:
        self.nodes_created += int(getattr(result, "nodes_created", 0) or 0)
        self.edges_created += int(getattr(result, "relationships_created", 0) or 0)
        self.properties_set += int(getattr(result, "properties_set", 0) or 0)


class _Writer:
    """Runs the seed's queries on one graph and tallies them into a report."""

    def __init__(self, graph, report: SeedReport):
        self.graph = graph
        self.report = report

    def query(self, text: str, params: dict | None = None):
        result = self.graph.query(text, params or {})
        self.report.count(result)
        return result


def _merge_node(graph, label: str, row: dict) -> None:
    props = {_identifier(key): value for key, value in row.items() if key != "id" and value is not None}
    assignments = ", ".join(f"n.{key} = ${key}" for key in props)
    set_clause = f" SET {assignments}" if assignments else ""
    graph.query(f"MERGE (n:{_identifier(label)} {{id: $id}}){set_clause}", {"id": row["id"], **props})


def _link(graph, from_label: str, from_id: str, rel: str, to_label: str, to_id: str) -> None:
    graph.query(
        f"MATCH (a:{_identifier(from_label)} {{id: $a}}), (b:{_identifier(to_label)} {{id: $b}}) MERGE (a)-[:{_identifier(rel)}]->(b)",
        {"a": from_id, "b": to_id},
    )


COMPANIES: dict[str, ModuleType] = {"company_brain": company_brain}


def company_module(name: str) -> ModuleType:
    """The seed module for a company name (``company_brain`` or ``quillfern``)."""
    if name == "quillfern":
        from src.seed import quillfern

        return quillfern
    return COMPANIES[name]


def reset_and_seed(company: ModuleType = company_brain) -> int:
    """Replace the graph with one company. Defaults to our own; tests pass ``quillfern``.

    Deletes everything first, including Slack and GitHub history, so this is for local
    development only. Production refreshes with ``seed_company``.
    """
    db, graph = connect()
    existing = {item.decode() if isinstance(item, bytes) else item for item in db.list_graphs()}
    if graph_name() in existing:
        graph.delete()
    return seed_company(company).nodes


def seed_company(company: ModuleType = company_brain) -> SeedReport:
    """Merge one company's seed into the graph as it is: every node and edge is MERGEd by
    id, so re-running it refreshes names, summaries and new decisions and leaves
    everything else (memories, workspaces, pull requests, tickets written from Slack)
    alone. Nothing here deletes."""
    _, real_graph = connect()
    report = SeedReport(company=company.COMPANY["id"])
    graph = _Writer(real_graph, report)
    _merge_node(graph, "Company", company.COMPANY)
    for person in company.PEOPLE:
        _merge_node(graph, "Person", person)

    for team in company.TEAMS:
        # ``scope`` is set only by a team onboarded from a private room; seeds leave it out.
        _merge_node(graph, "Team", {"id": team["id"], "name": team["name"], "summary": team.get("summary"), "scope": team.get("scope")})
        _link(graph, "Team", team["id"], "PART_OF", "Company", company.COMPANY["id"])
        for member in team["members"]:
            _link(graph, "Person", member, "MEMBER_OF", "Team", team["id"])

    kind_of = {service["id"]: normalize_kind(service.get("kind")) for service in company.SERVICES}
    for child, parent in company.DEPENDS_ON:
        # Accounts carry OWNS, WORKED_ON and ABOUT edges only; nothing depends on an account.
        for end in (child, parent):
            if kind_of.get(end) in NOT_DEPENDABLE:
                raise ValueError(f"{end} is an {kind_of[end]}; an {kind_of[end]} cannot take part in DEPENDS_ON")

    for service in company.SERVICES:
        row = {key: value for key, value in service.items() if key != "team"}
        row["kind"] = kind_of[service["id"]]
        _merge_node(graph, "Service", row)
        _link(graph, "Team", service["team"], "RUNS", "Service", service["id"])

    for child, parent in company.DEPENDS_ON:
        _link(graph, "Service", child, "DEPENDS_ON", "Service", parent)

    for person, rel, service in company.WORK:
        if rel not in WORK_RELATIONS:
            raise ValueError(f"{rel!r} is not one of {', '.join(sorted(WORK_RELATIONS))}")
        _link(graph, "Person", person, rel, "Service", service)

    # A team file can name each member's Slack user, so Slack answers and mentions find the person.
    for slack_user, person in getattr(company, "SLACK_USERS", ()):
        graph.query(
            "MERGE (u:SlackUser {id: $u}) WITH u MATCH (p:Person {id: $p}) MERGE (u)-[:IS]->(p)",
            {"u": slack_user, "p": person},
        )

    for document in company.DOCUMENTS:
        _merge_node(
            graph,
            "Document",
            {"id": document["id"], "name": document["name"], "summary": document["summary"]},
        )
        _link(graph, "Document", document["id"], "DESCRIBES", "Service", document["service"])
        _link(graph, "Person", document["author"], "AUTHORED", "Document", document["id"])

    for ticket in company.TICKETS:
        _merge_node(
            graph,
            "Ticket",
            {"id": ticket["id"], "name": ticket["name"], "title": ticket["title"], "summary": ticket["summary"], "aliases": ticket.get("aliases")},
        )
        _link(graph, "Ticket", ticket["id"], "ABOUT", "Service", ticket["service"])
        _link(graph, "Person", ticket["owner"], "WORKED_ON", "Ticket", ticket["id"])

    # Nodes first: a decision may supersede one listed after it.
    for decision in company.DECISIONS:
        _merge_node(graph, "Decision", {key: decision.get(key) for key in ("id", "name", "summary", "aliases", "decided_at", "scope")})
    for decision in company.DECISIONS:
        if decision.get("proposer"):
            _link(graph, "Person", decision["proposer"], "PROPOSED", "Decision", decision["id"])
        if decision.get("approver"):
            _link(graph, "Person", decision["approver"], "APPROVED", "Decision", decision["id"])
        # One service id in the seed modules; a list from a team file.
        about = decision["about"]
        for service in [about] if isinstance(about, str) else about:
            _link(graph, "Decision", decision["id"], "ABOUT", "Service", service)
        for source in decision.get("based_on") or []:
            graph.query(
                "MATCH (d:Decision {id: $d}), (source {id: $s}) MERGE (d)-[:BASED_ON]->(source)",
                {"d": decision["id"], "s": source},
            )
        if decision.get("supersedes"):
            _link(graph, "Decision", decision["id"], "SUPERSEDES", "Decision", decision["supersedes"])
        if decision.get("decided_in"):
            graph.query(
                "MATCH (d:Decision {id: $d}), (place {id: $p}) MERGE (d)-[:DECIDED_IN]->(place)",
                {"d": decision["id"], "p": decision["decided_in"]},
            )

    for chat in company.CHATS:
        _merge_node(graph, "Chat", {key: chat[key] for key in ("id", "name", "summary")})
        _link(graph, "Person", chat["author"], "AUTHORED", "Chat", chat["id"])
        _link(graph, "Chat", chat["id"], "ABOUT", "Decision", chat["about"])

    # Last, so a memory can mention anything above.
    _merge_memories(graph, real_graph, company)

    try:
        from src.domain.memory.memory_service import ensure_indexes
    except ImportError:
        ensure_indexes = None
    if ensure_indexes is not None:
        ensure_indexes()

    report.nodes = int(real_graph.query("MATCH (n) RETURN count(n)").result_set[0][0])
    return report


def _merge_memories(graph, real_graph, company: ModuleType) -> None:
    """A seed's ``MEMORIES`` as Slack ingest writes a kept public message: a ``Memory`` in
    ``org_shared`` (IN_SCOPE its Scope), SAID by the speaker's ``SlackUser``, MENTIONS
    whatever its text names, matched the way ingest matches it. Dated by ``said_at``, and
    without a channel id, since the channel is not in the workspace the graph serves.

    Merged by id, so a second run creates nothing. Only the wording is written back; a
    memory forgotten since keeps its flag, and a Slack user keeps the name Slack gave it."""
    from src.domain.memory.memory_service import mentioned_ids
    from src.domain.memory.writeback import SHARED_TEAM_BRAIN_CONTAINER_TAG as SHARED

    names = {person["id"]: person["name"] for person in company.PEOPLE}
    metadata = json.dumps({"source": "company-brain", "source_type": "slack-message"})
    for memory in getattr(company, "MEMORIES", ()):
        text = memory["text"]
        graph.query(
            """
            MERGE (scope:Scope {id: $scope})
            MERGE (m:Memory {id: $id})
            ON CREATE SET m.custom_id = $id, m.scope = $scope, m.metadata = $metadata, m.source_type = 'slack-message',
                          m.brain_tags = [], m.brain_tag_labels = [], m.buckets = [], m.ts = $ts, m.is_forgotten = false,
                          m.created_at = $at, m.updated_at = $at, m.source_count = 1
            SET m.content = $text, m.text = $text, m.name = $name
            MERGE (m)-[:IN_SCOPE]->(scope)
            MERGE (u:SlackUser {id: $user})
            ON CREATE SET u.name = $speaker
            MERGE (u)-[:SAID]->(m)
            """,
            {
                "scope": SHARED,
                "id": memory["id"],
                "metadata": metadata,
                "ts": f"{memory['said_at'] / 1000:.6f}",
                "at": memory["said_at"],
                "text": text,
                "name": _memory_name(text),
                "user": memory["slack_user"],
                "speaker": names[memory["author"]],
            },
        )
        for node_id in mentioned_ids(real_graph, text, [SHARED]):
            graph.query("MATCH (m:Memory {id: $m}), (n {id: $n}) MERGE (m)-[:MENTIONS]->(n)", {"m": memory["id"], "n": node_id})


def _memory_name(text: str, size: int = 60) -> str:
    """A memory's short name, as ingest names one (``documents._short``)."""
    text = " ".join(text.split())
    return text if len(text) <= size else text[: size - 1] + "…"
