"""Scoped memory in FalkorDB. Port of company-brain `brain/memory/index.ts` (memory writes).

Two ways a memory lands, both as a ``Memory`` node in exactly one container:

- ``write``: a raw Slack message kept as context (or an explicit ``remember:``).
- ``write_memory`` / ``write_memories``: a model-proposed ``MemoryDoc`` (title, content,
  tags). Tags are canonicalized against the container's existing tags, the write is
  upserted by custom id, tags are registered, the memory is filed under each
  topic-tree node, and an oversized node is split. Every step checks the reset epoch
  so a write from before a reset can't land after it.

    (SlackUser)-[:SAID]->(Memory)-[:IN_SCOPE]->(Scope)
                         (Memory)-[:HAS_TAG]->(Tag)<-[:FILED_UNDER]-(Memory)
                         (Memory)-[:MENTIONS]->(Service | Document | Ticket | Decision)

MENTIONS edges tie chat into the company graph. Every read takes the caller's
readable containers and filters on them; nothing is returned outside them.
"""

import logging
import re
import threading
from dataclasses import dataclass

from src.db.falkordb_client import connect
from src.domain.memory import tree
from src.domain.memory.cleanup import delete_stale_brain_memory_document
from src.domain.memory.documents import upsert_brain_document
from src.domain.memory.profile_sync import clear_brain_profile_sync
from src.domain.memory.search_brain import search_container
from src.domain.memory.split import maybe_split_node
from src.domain.memory.tags import canonicalize_proposed_tags, list_brain_memory_tags, register_brain_memory_tags
from src.domain.memory.writeback import (
    SHARED_TEAM_BRAIN_CONTAINER_TAG,
    MemoryDocInput,
    SlackMemoryScope,
    build_slack_memory_write_request,
    memory_docs_from_writeback,
    slack_memory_container_tag,
)

log = logging.getLogger(__name__)

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
        "CREATE INDEX FOR (m:Memory) ON (m.id)",
        "CREATE INDEX FOR (m:Memory) ON (m.custom_id)",
        "CREATE INDEX FOR (t:Tag) ON (t.id)",
        "CREATE INDEX FOR (t:Tag) ON (t.container)",
        "CREATE INDEX FOR (s:Scope) ON (s.id)",
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


# --- raw message memory ------------------------------------------------------------------


def write(text: str, scope: str, author_id: str, channel_id: str, ts: str, mention_scopes: list[str] | None = None) -> Memory:
    """Keep one Slack message in ``scope``.

    ``mention_scopes`` limits which scoped nodes (tickets) this memory may link to.
    Defaults to org_shared plus the memory's own container, so a memory never links
    to something its readers could not see.
    """
    result = upsert_brain_document(
        content=text,
        container_tag=scope,
        metadata={"source": "company-brain", "source_type": "slack-message", "slack_channel_id": channel_id},
        author_id=author_id,
        channel_id=channel_id,
        ts=ts,
    )
    _, graph = connect()
    _link_mentions(graph, result.id, text, mention_scopes or [SHARED_TEAM_BRAIN_CONTAINER_TAG, scope])
    return Memory(id=result.id, text=text, scope=scope, author=author_id, channel_id=channel_id, ts=ts)


def search(query: str, scopes: list[str], limit: int = 5) -> list[dict]:
    """Best memories across ``scopes`` for a query: [{text, scope, channel_id, ts, author_id, author}]."""
    if not scopes:
        return []
    by_id: dict[str, dict] = {}
    for scope in dict.fromkeys(scopes):
        for result in search_container(query, scope, limit=limit):
            if result["id"] not in by_id or result["similarity"] > by_id[result["id"]]["similarity"]:
                by_id[result["id"]] = result
    best = sorted(by_id.values(), key=lambda r: (-r["similarity"], -(r["updated_at"] or 0)))[:limit]
    return [
        {
            "id": r["id"],
            "text": r["memory"],
            "scope": r["container_tag"],
            "channel_id": r["channel_id"],
            "ts": r["ts"],
            "author_id": r["author_id"],
            "author": r["author"],
            "similarity": r["similarity"],
        }
        for r in best
    ]


def forget_scope(scope: str, channel_id: str | None = None) -> int:
    """Delete a container's memories. ``channel_id`` narrows org_shared to one public channel."""
    _, graph = connect()
    rows = graph.query(
        """
        MATCH (m:Memory {scope: $scope})
        WHERE $channel IS NULL OR m.channel_id = $channel
        WITH m, m.id AS id DETACH DELETE m RETURN count(id)
        """,
        {"scope": scope, "channel": channel_id},
    ).result_set
    return int(rows[0][0]) if rows else 0


# --- model-proposed memory (index.ts) ----------------------------------------------------


def write_memory(
    doc: MemoryDocInput,
    scope: SlackMemoryScope | None = None,
    allowed_person_slack_user_ids: list[str] | None = None,
    expected_reset_epoch: int | None = None,
    author_id: str | None = None,
    split_in_background: bool = True,
) -> dict:
    """Port of ``writeMemory``. Returns {"written": bool, "id": str | None}."""
    reset_epoch = tree.get_brain_memory_reset_epoch() if expected_reset_epoch is None else expected_reset_epoch
    if not tree.is_brain_memory_reset_epoch_current(reset_epoch):
        return {"written": False, "id": None}

    container_tag = slack_memory_container_tag(scope)
    existing = list_brain_memory_tags([container_tag]) if container_tag else []
    effective = doc
    if existing:
        effective = MemoryDocInput(**{**doc.__dict__, "tags": canonicalize_proposed_tags(existing, doc.tags)})

    request = build_slack_memory_write_request(
        effective,
        scope,
        allowed_person_slack_user_ids=allowed_person_slack_user_ids,
        expected_reset_epoch=reset_epoch,
    )
    if request is None:
        return {"written": False, "id": None}
    if not request.tags:
        log.warning('[slack] memory write skipped: no valid tags after normalization title="%s"', doc.title)
        return {"written": False, "id": None}

    try:
        result = upsert_brain_document(
            content=request.content,
            memory=request.memory,
            container_tag=request.container_tag,
            custom_id=request.custom_id,
            metadata=request.metadata,
            title=doc.title,
            buckets=request.buckets,
            author_id=author_id,
            channel_id=scope.channel_id if scope else None,
        )
    except Exception:
        log.exception("[slack-memory] write failed")
        return {"written": False, "id": None}
    if result.status != "done":
        return {"written": False, "id": None}

    if not tree.is_brain_memory_reset_epoch_current(reset_epoch):
        delete_stale_brain_memory_document(result.id)
        return {"written": False, "id": None}
    try:
        register_brain_memory_tags(request.container_tag, request.tags)
    except Exception:
        log.exception("[brain-memory-tags] register failed")
    _, graph = connect()
    _link_mentions(graph, result.id, request.memory, [SHARED_TEAM_BRAIN_CONTAINER_TAG, request.container_tag])
    for node_path in tree.pick_node_paths(request.tags):
        mapped = tree.upsert_brain_memory_node(
            tree.BrainNodeMapping(document_id=result.id, container_tag=request.container_tag, node_path=node_path),
            reset_epoch,
        )
        if mapped:
            if split_in_background:
                threading.Thread(target=maybe_split_node, args=(request.container_tag, node_path, reset_epoch), daemon=True).start()
            else:
                maybe_split_node(request.container_tag, node_path, reset_epoch)
    return {"written": True, "id": result.id}


def write_memories(memory, scope: SlackMemoryScope | None = None, **options) -> dict:
    """Port of ``writeMemories``. Returns {"written": n, "total": m}."""
    docs = memory_docs_from_writeback(memory)
    written = sum(1 for doc in docs if write_memory(doc, scope, **options)["written"])
    return {"written": written, "total": len(docs)}


def reset_memory_registry() -> int:
    """The memory-layer half of company-brain's ``resetMemoryRegistry``.

    Advances the reset epoch (in-flight writes from before it are dropped), clears
    the profile-sync throttle and the tag/topic registry. Returns the new epoch.
    Channel-observe and post-turn-reflect state belong to their own modules.
    """
    epoch = tree.advance_brain_memory_reset_epoch()
    clear_brain_profile_sync()
    tree.clear_brain_memory_registry()
    return epoch


# --- mentions ----------------------------------------------------------------------------


def _link_mentions(graph, memory_id: str, text: str, scopes: list[str]) -> None:
    for node_id in mentioned_ids(graph, text, scopes):
        graph.query(
            "MATCH (m:Memory {id: $memory}), (n {id: $node}) MERGE (m)-[:MENTIONS]->(n)",
            {"memory": memory_id, "node": node_id},
        )


def mentioned_ids(graph, text: str, scopes: list[str]) -> list[str]:
    """The services, documents, tickets and decisions in ``scopes`` that ``text`` names, by name or alias."""
    rows = graph.query(
        """
        MATCH (n)
        WHERE labels(n)[0] IN $labels AND n.name IS NOT NULL
          AND coalesce(n.scope, $shared) IN $scopes
        RETURN n.id, n.name, coalesce(n.aliases, '')
        """,
        {"labels": list(MENTIONABLE), "scopes": scopes, "shared": SHARED_TEAM_BRAIN_CONTAINER_TAG},
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
