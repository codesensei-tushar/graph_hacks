"""Scoped memory in FalkorDB.

A Slack message becomes a Memory node in exactly one scope:

    (SlackUser)-[:SAID]->(Memory)-[:IN_SCOPE]->(Scope)
                         (Memory)-[:MENTIONS]->(Service | Document | Ticket | Decision)

MENTIONS edges tie chat into the company graph, so a later ownership or decision
question can walk from a service to what people said about it. Every read takes the
caller's readable scopes and filters on them; nothing is returned outside those scopes.
"""

import re
import time
import uuid
from dataclasses import dataclass

from src.db.falkordb_client import connect

MENTIONABLE = ("Service", "Document", "Ticket", "Decision")
MIN_WORDS = 3


@dataclass
class Memory:
    id: str
    text: str
    scope: str
    author: str
    channel_id: str
    ts: str


def ensure_indexes() -> None:
    _, graph = connect()
    for statement in (
        "CREATE INDEX FOR (m:Memory) ON (m.scope)",
        "CREATE INDEX FOR (u:SlackUser) ON (u.id)",
        "CREATE INDEX FOR (c:Channel) ON (c.id)",
        "CALL db.idx.fulltext.createNodeIndex('Memory', 'text')",
    ):
        try:
            graph.query(statement)
        except Exception as error:  # already exists
            if "already" not in str(error).lower():
                raise


def worth_keeping(text: str) -> bool:
    return len(text.split()) >= MIN_WORDS


def write(text: str, scope: str, author_id: str, channel_id: str, ts: str, mention_scopes: list[str] | None = None) -> Memory:
    """``mention_scopes`` limits which scoped nodes (tickets) this memory may link to. Defaults to its own scope."""
    _, graph = connect()
    memory = Memory(id=uuid.uuid4().hex, text=text, scope=scope, author=author_id, channel_id=channel_id, ts=ts)
    graph.query(
        """
        MERGE (scope:Scope {id: $scope})
        MERGE (author:SlackUser {id: $author})
        CREATE (m:Memory {id: $id, name: $name, text: $text, scope: $scope, channel_id: $channel, ts: $ts, created_at: $now})
        CREATE (author)-[:SAID]->(m)
        CREATE (m)-[:IN_SCOPE]->(scope)
        """,
        {
            "id": memory.id,
            "name": _short(text),
            "text": text,
            "scope": scope,
            "author": author_id,
            "channel": channel_id,
            "ts": ts,
            "now": int(time.time()),
        },
    )
    for node_id in _mentioned_ids(graph, text, mention_scopes or [scope]):
        graph.query(
            "MATCH (m:Memory {id: $memory}), (n {id: $node}) MERGE (m)-[:MENTIONS]->(n)",
            {"memory": memory.id, "node": node_id},
        )
    return memory


def search(query: str, scopes: list[str], limit: int = 5) -> list[dict]:
    terms = [term for term in re.split(r"[^A-Za-z0-9]+", query) if len(term) > 2]
    if not terms or not scopes:
        return []
    _, graph = connect()
    rows = graph.query(
        """
        CALL db.idx.fulltext.queryNodes('Memory', $q) YIELD node, score
        WHERE node.scope IN $scopes
        OPTIONAL MATCH (author:SlackUser)-[:SAID]->(node)
        RETURN node.text, node.scope, node.channel_id, node.ts, author.id, author.name, score
        ORDER BY score DESC
        LIMIT $limit
        """,
        {"q": "|".join(terms), "scopes": scopes, "limit": limit},
    ).result_set
    return [
        {
            "text": text,
            "scope": scope,
            "channel_id": channel_id,
            "ts": ts,
            "author_id": author_id,
            "author": author_name or author_id,
        }
        for text, scope, channel_id, ts, author_id, author_name, _score in rows
    ]


def forget_scope(scope: str) -> int:
    _, graph = connect()
    rows = graph.query(
        "MATCH (m:Memory {scope: $scope}) WITH m, m.id AS id DETACH DELETE m RETURN count(id)",
        {"scope": scope},
    ).result_set
    return int(rows[0][0]) if rows else 0


def _mentioned_ids(graph, text: str, scopes: list[str]) -> list[str]:
    rows = graph.query(
        """
        MATCH (n)
        WHERE labels(n)[0] IN $labels AND n.name IS NOT NULL
          AND coalesce(n.scope, 'org_shared') IN $scopes
        RETURN n.id, n.name, coalesce(n.aliases, '')
        """,
        {"labels": list(MENTIONABLE), "scopes": scopes},
    ).result_set
    lowered = text.lower()
    found = []
    for node_id, name, aliases in rows:
        names = [name] + [alias.strip() for alias in aliases.split(",") if alias.strip()]
        if any(len(candidate) > 3 and _contains_word(lowered, candidate.lower()) for candidate in names):
            found.append(node_id)
    return found


def _contains_word(text: str, word: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None


def _short(text: str, size: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= size else text[: size - 1] + "…"
