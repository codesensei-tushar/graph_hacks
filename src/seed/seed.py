from types import ModuleType

from src.db.falkordb_client import connect, graph_name
from src.seed import company_brain


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


def reset_and_seed(company: ModuleType = company_brain) -> int:
    """Replace the graph with one company. Defaults to our own; tests pass ``quillfern``."""
    db, graph = connect()
    existing = {item.decode() if isinstance(item, bytes) else item for item in db.list_graphs()}
    if graph_name() in existing:
        graph.delete()
        graph = db.select_graph(graph_name())

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
            {"id": ticket["id"], "name": ticket["name"], "title": ticket["title"], "summary": ticket["summary"]},
        )
        _link(graph, "Ticket", ticket["id"], "ABOUT", "Service", ticket["service"])
        _link(graph, "Person", ticket["owner"], "WORKED_ON", "Ticket", ticket["id"])

    for decision in company.DECISIONS:
        _merge_node(graph, "Decision", {key: decision[key] for key in ("id", "name", "summary", "aliases")})
        _link(graph, "Person", decision["proposer"], "PROPOSED", "Decision", decision["id"])
        _link(graph, "Person", decision["approver"], "APPROVED", "Decision", decision["id"])
        _link(graph, "Decision", decision["id"], "ABOUT", "Service", decision["about"])
        for cited in decision["cites"]:
            graph.query(
                "MATCH (d:Decision {id: $d}), (source {id: $s}) MERGE (d)-[:CITES]->(source)",
                {"d": decision["id"], "s": cited},
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

    return int(graph.query("MATCH (n) RETURN count(n)").result_set[0][0])
