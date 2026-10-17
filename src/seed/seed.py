from dataclasses import dataclass
from types import ModuleType

from src.db.falkordb_client import connect, graph_name
from src.seed import company_brain


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
    props = {key: value for key, value in row.items() if key != "id" and value is not None}
    assignments = ", ".join(f"n.{key} = ${key}" for key in props)
    set_clause = f" SET {assignments}" if assignments else ""
    graph.query(f"MERGE (n:{label} {{id: $id}}){set_clause}", {"id": row["id"], **props})


def _link(graph, from_label: str, from_id: str, rel: str, to_label: str, to_id: str) -> None:
    graph.query(
        f"MATCH (a:{from_label} {{id: $a}}), (b:{to_label} {{id: $b}}) MERGE (a)-[:{rel}]->(b)",
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
        _merge_node(graph, "Team", {"id": team["id"], "name": team["name"]})
        _link(graph, "Team", team["id"], "PART_OF", "Company", company.COMPANY["id"])
        for member in team["members"]:
            _link(graph, "Person", member, "MEMBER_OF", "Team", team["id"])

    for service in company.SERVICES:
        _merge_node(graph, "Service", {key: value for key, value in service.items() if key != "team"})
        _link(graph, "Team", service["team"], "RUNS", "Service", service["id"])

    for child, parent in company.DEPENDS_ON:
        _link(graph, "Service", child, "DEPENDS_ON", "Service", parent)

    for person, rel, service in company.WORK:
        _link(graph, "Person", person, rel, "Service", service)

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
        _merge_node(graph, "Decision", {key: decision.get(key) for key in ("id", "name", "summary", "aliases", "decided_at")})
    for decision in company.DECISIONS:
        _link(graph, "Person", decision["proposer"], "PROPOSED", "Decision", decision["id"])
        _link(graph, "Person", decision["approver"], "APPROVED", "Decision", decision["id"])
        _link(graph, "Decision", decision["id"], "ABOUT", "Service", decision["about"])
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

    try:
        from src.domain.memory.memory_service import ensure_indexes
    except ImportError:
        ensure_indexes = None
    if ensure_indexes is not None:
        ensure_indexes()

    report.nodes = int(real_graph.query("MATCH (n) RETURN count(n)").result_set[0][0])
    return report
