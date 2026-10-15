"""Port of company-brain `memory/documents.ts`, `memory/memories.ts`, `memory/markdown.ts`,
`compat/routes/v4/memories/forget-matching.ts` and the read side of `routes/memories.ts`.

company-brain wrote documents to supermemory, which extracted memories from them.
There is no extractor here: a FalkorDB ``Memory`` node is both the document and its
one memory. ``id`` is the document id and the memory id; ``custom_id`` is the
caller's stable id for upserts. Metadata lives as a JSON string in ``metadata``,
with the fields the brain filters on promoted to properties (``type``,
``brain_tags``, ``brain_tag_labels``, ``buckets``). supermemory's forget marks a
memory forgotten instead of deleting it; so does this (``is_forgotten``), and every
read skips forgotten and expired (``forget_after``) memories.
"""

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from src.db.falkordb_client import connect
from src.domain.memory.containers import SHARED_TEAM_BRAIN_CONTAINER_TAG, private_container_tag_for
from src.domain.memory.tags import BRAIN_TAG_LABELS_METADATA_KEY, BRAIN_TAGS_METADATA_KEY

log = logging.getLogger(__name__)

# A memory is live when it is not forgotten and its forget horizon has not passed.
LIVE = "coalesce(m.is_forgotten, false) = false AND (m.forget_after IS NULL OR m.forget_after > $now)"

RECENT_LIMIT = 8
EXPORT_LIMIT = 2000


def now_ms() -> int:
    return int(time.time() * 1000)


def iso(ms: int | None) -> str:
    return datetime.fromtimestamp((ms or 0) / 1000, UTC).isoformat().replace("+00:00", "Z")


def _metadata(raw) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


# --- documents --------------------------------------------------------------------------


@dataclass
class BrainDocument:
    id: str
    custom_id: str | None
    content: str | None
    title: str | None
    metadata: dict | None
    created_at: str
    container_tag: str | None = None


_DOC_FIELDS = "m.id, m.custom_id, m.content, m.text, m.title, m.metadata, m.created_at, m.scope"


def _doc(row, include_content: bool = True) -> BrainDocument:
    doc_id, custom_id, content, text, title, metadata, created_at, scope = row
    return BrainDocument(
        id=doc_id,
        custom_id=custom_id,
        content=(content or text) if include_content else None,
        title=title,
        metadata=_metadata(metadata) or None,
        created_at=iso(created_at),
        container_tag=scope,
    )


def _matches(metadata: dict, conditions: list[dict] | None) -> bool:
    for condition in conditions or []:
        if str(metadata.get(condition["key"])) != str(condition["value"]):
            return False
    return True


def list_brain_documents(
    container_tag: str,
    metadata: list[dict] | None = None,
    limit: int = 50,
    include_content: bool = True,
) -> list[BrainDocument]:
    """Documents in one container, newest first. ``metadata`` is [{key, value}] (all must match)."""
    _, graph = connect()
    rows = graph.query(
        f"MATCH (m:Memory) WHERE m.scope = $scope AND {LIVE} RETURN {_DOC_FIELDS} ORDER BY m.created_at DESC",
        {"scope": container_tag, "now": now_ms()},
    ).result_set
    out = []
    for row in rows:
        doc = _doc(row, include_content)
        if _matches(doc.metadata or {}, metadata):
            out.append(doc)
            if len(out) >= limit:
                break
    return out


def find_brain_document(container_tag: str, metadata: list[dict] | None = None) -> BrainDocument | None:
    """The newest document in a container matching every metadata condition."""
    found = list_brain_documents(container_tag, metadata, limit=1)
    return found[0] if found else None


def get_brain_document_by_custom_id(custom_id: str) -> BrainDocument | None:
    _, graph = connect()
    rows = graph.query(
        f"MATCH (m:Memory {{custom_id: $cid}}) WHERE {LIVE} RETURN {_DOC_FIELDS}",
        {"cid": custom_id, "now": now_ms()},
    ).result_set
    return _doc(rows[0]) if rows else None


def get_brain_document(document_id: str) -> BrainDocument | None:
    _, graph = connect()
    rows = graph.query(
        f"MATCH (m:Memory {{id: $id}}) WHERE {LIVE} RETURN {_DOC_FIELDS}",
        {"id": document_id, "now": now_ms()},
    ).result_set
    return _doc(rows[0]) if rows else None


def update_brain_document_metadata(document_id: str, metadata: dict) -> None:
    """Replace a document's metadata, keeping its content and container."""
    scalars = {}
    for key, value in metadata.items():
        if value is None:
            continue
        scalars[key] = ",".join(map(str, value)) if isinstance(value, list) else value
    _, graph = connect()
    graph.query("MATCH (m:Memory {id: $id}) SET m.metadata = $metadata", {"id": document_id, "metadata": json.dumps(scalars)})


def delete_brain_document(document_id: str) -> bool:
    try:
        _, graph = connect()
        graph.query("MATCH (m:Memory {id: $id}) DETACH DELETE m", {"id": document_id})
        return True
    except Exception:
        log.exception("[memory] delete failed id=%s", document_id)
        return False


def bulk_delete_documents_and_memories(ids: list[str]) -> int:
    if not ids:
        return 0
    _, graph = connect()
    rows = graph.query(
        "MATCH (m:Memory) WHERE m.id IN $ids WITH m, m.id AS id DETACH DELETE m RETURN count(id)",
        {"ids": list(dict.fromkeys(ids))},
    ).result_set
    return int(rows[0][0]) if rows else 0


@dataclass
class UpsertResult:
    id: str
    status: str  # "done" | "error"
    created: bool = False
    error: str | None = None


def upsert_brain_document(
    *,
    content: str,
    container_tag: str,
    custom_id: str | None = None,
    metadata: dict | None = None,
    memory: str | None = None,
    title: str | None = None,
    buckets: list[str] | None = None,
    author_id: str | None = None,
    channel_id: str | None = None,
    ts: str | None = None,
    forget_after: int | None = None,
) -> UpsertResult:
    """Port of ``addMemorySingle``: write one document into a container, upserting by custom id."""
    metadata = dict(metadata or {})
    text = (memory or content).strip()
    if not text:
        return UpsertResult(id="", status="error", error="empty content")
    now = now_ms()
    tag_keys = [str(k) for k in metadata.get(BRAIN_TAGS_METADATA_KEY) or []]
    tag_labels = [str(k) for k in metadata.get(BRAIN_TAG_LABELS_METADATA_KEY) or []]
    params = {
        "id": uuid.uuid4().hex,
        "cid": custom_id or uuid.uuid4().hex,
        "scope": container_tag,
        "content": content,
        "text": text,
        "name": _short(title or text),
        "title": title or metadata.get("title"),
        "metadata": json.dumps(metadata),
        "type": metadata.get("type"),
        "source_type": metadata.get("source_type"),
        "tags": tag_keys,
        "labels": tag_labels,
        "buckets": list(buckets or []),
        "channel": channel_id or metadata.get("slack_channel_id"),
        "ts": ts,
        "forget_after": forget_after,
        "now": now,
    }
    _, graph = connect()
    rows = graph.query(
        """
        MERGE (scope:Scope {id: $scope})
        MERGE (m:Memory {custom_id: $cid})
        ON CREATE SET m.id = $id, m.created_at = $now, m.source_count = 0
        SET m.scope = $scope, m.content = $content, m.text = $text, m.name = $name, m.title = $title,
            m.metadata = $metadata, m.type = $type, m.source_type = $source_type,
            m.brain_tags = $tags, m.brain_tag_labels = $labels, m.buckets = $buckets,
            m.channel_id = $channel, m.ts = $ts, m.forget_after = $forget_after,
            m.is_forgotten = false, m.updated_at = $now, m.source_count = m.source_count + 1
        MERGE (m)-[:IN_SCOPE]->(scope)
        RETURN m.id, m.created_at = $now
        """,
        params,
    ).result_set
    memory_id, created = rows[0]
    # A re-upsert may move a memory between containers; keep exactly one IN_SCOPE edge.
    graph.query(
        "MATCH (m:Memory {id: $id})-[r:IN_SCOPE]->(s:Scope) WHERE s.id <> $scope DELETE r",
        {"id": memory_id, "scope": container_tag},
    )
    if author_id:
        graph.query(
            "MATCH (m:Memory {id: $id}) MERGE (u:SlackUser {id: $author}) MERGE (u)-[:SAID]->(m)",
            {"id": memory_id, "author": author_id},
        )
    _link_tags(graph, memory_id, container_tag, tag_keys)
    return UpsertResult(id=memory_id, status="done", created=bool(created))


def _link_tags(graph, memory_id: str, container_tag: str, tag_keys: list[str]) -> None:
    from src.domain.memory.tags import tag_node_id

    graph.query("MATCH (m:Memory {id: $id})-[r:HAS_TAG]->(:Tag) DELETE r", {"id": memory_id})
    for key in tag_keys:
        graph.query(
            """
            MATCH (m:Memory {id: $id})
            MERGE (t:Tag {id: $tag})
            ON CREATE SET t.container = $scope, t.key = $key, t.label = $key, t.kind = 'other',
                          t.implicit = true, t.use_count = 0, t.created_at = $now, t.updated_at = $now, t.last_used_at = $now
            MERGE (m)-[:HAS_TAG]->(t)
            """,
            {"id": memory_id, "tag": tag_node_id(container_tag, key), "scope": container_tag, "key": key, "now": now_ms()},
        )


def _short(text: str, size: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= size else text[: size - 1] + "…"


# --- memories ---------------------------------------------------------------------------


@dataclass
class BrainMemory:
    id: str
    memory: str
    metadata: dict | None
    tags: list[str]
    buckets: list[str]
    document_ids: list[str]
    source_count: int
    updated_at: str
    container_tag: str | None = None


@dataclass
class DerivedMemory:
    id: str
    document_id: str
    container_tag: str | None
    memory: str
    metadata: dict | None
    buckets: list[str]
    updated_at: datetime
    extra: dict = field(default_factory=dict)


_MEMORY_FIELDS = "m.id, m.text, m.metadata, m.brain_tags, m.buckets, m.source_count, m.updated_at, m.scope"


def _brain_memory(row) -> BrainMemory:
    memory_id, text, metadata, tags, buckets, source_count, updated_at, scope = row
    return BrainMemory(
        id=memory_id,
        memory=text or "",
        metadata=_metadata(metadata) or None,
        tags=list(tags or []),
        buckets=list(buckets or []),
        document_ids=[memory_id],
        source_count=int(source_count or 1),
        updated_at=iso(updated_at),
        container_tag=scope,
    )


def list_brain_memories(
    container_tags: list[str],
    limit: int = 50,
    tag_keys: list[str] | None = None,
    with_buckets: bool = False,
) -> list[BrainMemory]:
    """Memories in one or more containers, newest first, optionally only those carrying a brain tag.

    ``with_buckets`` is kept for signature parity; buckets are always on the node here.
    """
    containers = list(dict.fromkeys(tag for tag in container_tags if tag))
    if not containers:
        return []
    _, graph = connect()
    rows = graph.query(
        f"""
        MATCH (m:Memory)
        WHERE m.scope IN $scopes AND {LIVE}
          AND (size($keys) = 0 OR any(k IN coalesce(m.brain_tags, []) WHERE k IN $keys))
          AND m.text IS NOT NULL
        RETURN {_MEMORY_FIELDS}
        ORDER BY m.updated_at DESC
        LIMIT $limit
        """,
        {"scopes": containers, "keys": list(tag_keys or []), "limit": limit, "now": now_ms()},
    ).result_set
    return [_brain_memory(row) for row in rows if (row[1] or "").strip()]


def memories_for_documents(document_ids: list[str]) -> list[DerivedMemory]:
    """The live memories derived from these documents: here, the documents themselves."""
    ids = list(dict.fromkeys(i for i in document_ids if i))
    if not ids:
        return []
    _, graph = connect()
    rows = graph.query(
        f"MATCH (m:Memory) WHERE m.id IN $ids AND {LIVE} RETURN {_MEMORY_FIELDS}",
        {"ids": ids, "now": now_ms()},
    ).result_set
    return [
        DerivedMemory(
            id=memory_id,
            document_id=memory_id,
            container_tag=scope,
            memory=text or "",
            metadata=_metadata(metadata) or None,
            buckets=list(buckets or []),
            updated_at=datetime.fromtimestamp((updated_at or 0) / 1000, UTC),
        )
        for memory_id, text, metadata, _tags, buckets, _count, updated_at, scope in rows
        if (text or "").strip()
    ]


def update_memory_metadata(memory_id: str, container_tag: str, memory: str, metadata: dict) -> None:
    """Replace a memory's metadata (and promoted tag fields). supermemory versioned; this edits in place."""
    _, graph = connect()
    tag_keys = [str(k) for k in metadata.get(BRAIN_TAGS_METADATA_KEY) or []]
    graph.query(
        """
        MATCH (m:Memory {id: $id})
        SET m.metadata = $metadata, m.text = $memory, m.brain_tags = $tags, m.brain_tag_labels = $labels, m.updated_at = $now
        """,
        {
            "id": memory_id,
            "metadata": json.dumps(metadata),
            "memory": memory,
            "tags": tag_keys,
            "labels": [str(k) for k in metadata.get(BRAIN_TAG_LABELS_METADATA_KEY) or []],
            "now": now_ms(),
        },
    )
    _link_tags(graph, memory_id, container_tag, tag_keys)


@dataclass
class BrainDocumentStatus:
    id: str
    status: str
    dreaming_status: str | None = None


def document_statuses(ids: list[str]) -> dict[str, BrainDocumentStatus]:
    """Processing status for documents the brain wrote. FalkorDB writes are synchronous: live ones are done."""
    _, graph = connect()
    rows = graph.query(
        f"MATCH (m:Memory) WHERE m.id IN $ids AND {LIVE} RETURN m.id",
        {"ids": list(dict.fromkeys(ids)), "now": now_ms()},
    ).result_set
    return {row[0]: BrainDocumentStatus(id=row[0], status="done") for row in rows}


def memory_count(container_tag: str) -> int:
    _, graph = connect()
    rows = graph.query(
        f"MATCH (m:Memory) WHERE m.scope = $scope AND {LIVE} RETURN count(m)",
        {"scope": container_tag, "now": now_ms()},
    ).result_set
    return int(rows[0][0]) if rows else 0


# --- forget matching ---------------------------------------------------------------------


@dataclass
class ForgetCandidate:
    id: str
    memory: str
    score: float


@dataclass
class ForgetMatchingResult:
    dry_run: bool
    count: int
    forget_batch_id: str | None
    summary: str
    candidates: list[ForgetCandidate] | None = None
    forgotten: list[ForgetCandidate] | None = None


def forget_matching_memories(
    container_tag: str,
    query: str | None = None,
    dry_run: bool = True,
    threshold: float = 0.5,
    max_forget: int = 100,
    reason: str | None = None,
    ids: list[str] | None = None,
) -> ForgetMatchingResult:
    """Forget memories matching a query (preview with dry_run, then forget exactly the previewed ids)."""
    explicit = list(dict.fromkeys(ids)) if ids else None
    if explicit:
        candidates = [ForgetCandidate(id=i, memory="", score=1.0) for i in explicit]
    else:
        candidates = _search_candidates(container_tag, query, threshold, max_forget)
    if dry_run:
        return ForgetMatchingResult(
            dry_run=True, count=len(candidates), forget_batch_id=None, summary=_summarize(len(candidates), True), candidates=candidates
        )
    _, graph = connect()
    rows = graph.query(
        f"""
        MATCH (m:Memory)
        WHERE m.id IN $ids AND m.scope = $scope AND {LIVE}
        SET m.is_forgotten = true, m.forget_reason = $reason, m.forgotten_at = $now
        RETURN m.id, m.text
        """,
        {"ids": [c.id for c in candidates], "scope": container_tag, "reason": reason, "now": now_ms()},
    ).result_set
    forgotten_ids = {row[0]: row[1] for row in rows}
    forgotten = [
        ForgetCandidate(id=c.id, memory=c.memory or forgotten_ids[c.id] or "", score=c.score) for c in candidates if c.id in forgotten_ids
    ]
    return ForgetMatchingResult(
        dry_run=False,
        count=len(forgotten),
        forget_batch_id=uuid.uuid4().hex,
        summary=_summarize(len(forgotten), False),
        forgotten=forgotten,
    )


def _search_candidates(container_tag: str, query: str | None, threshold: float, max_forget: int) -> list[ForgetCandidate]:
    if not (query or "").strip():
        return []
    from src.domain.memory.search_brain import search_container

    results = search_container(query or "", container_tag, limit=max_forget, threshold=threshold)
    return [ForgetCandidate(id=r["id"], memory=r["memory"], score=r["similarity"]) for r in results if r["similarity"] >= threshold][
        :max_forget
    ]


def _summarize(count: int, preview: bool) -> str:
    if count == 0:
        return "Nothing matched."
    noun = "memory" if count == 1 else "memories"
    return f"Would forget {count} {noun}." if preview else f"Forgot {count} {noun}."


# --- overview, export, graph (routes/memories.ts, routes/graph.ts) -------------------------


def brain_memories_overview(user_id: str) -> dict:
    """The home-page view: shared brain + the viewer's own container, never a teammate's."""
    containers = [SHARED_TEAM_BRAIN_CONTAINER_TAG, private_container_tag_for(user_id)]
    recent = list_brain_memories(containers, limit=RECENT_LIMIT)
    return {
        "count": sum(memory_count(tag) for tag in containers),
        "recent": [{"id": m.id, "memory": m.memory, "updatedAt": m.updated_at} for m in recent],
    }


def brain_graph_page(user_id: str, page: int = 1, limit: int = 200) -> dict:
    """Documents and their memories for a graph view, filtered to shared + the viewer's own container."""
    containers = [SHARED_TEAM_BRAIN_CONTAINER_TAG, private_container_tag_for(user_id)]
    limit = max(1, min(limit, 500))
    page = max(1, page)
    _, graph = connect()
    total = graph.query(
        f"MATCH (m:Memory) WHERE m.scope IN $scopes AND {LIVE} RETURN count(m)",
        {"scopes": containers, "now": now_ms()},
    ).result_set[0][0]
    rows = graph.query(
        f"""
        MATCH (m:Memory) WHERE m.scope IN $scopes AND {LIVE}
        RETURN {_MEMORY_FIELDS} ORDER BY m.created_at DESC SKIP $skip LIMIT $limit
        """,
        {"scopes": containers, "now": now_ms(), "skip": (page - 1) * limit, "limit": limit},
    ).result_set
    documents = []
    for row in rows:
        memory = _brain_memory(row)
        documents.append(
            {
                "id": memory.id,
                "metadata": memory.metadata,
                "memoryEntries": [{"id": memory.id, "memory": memory.memory, "spaceContainerTag": memory.container_tag}],
            }
        )
    return {
        "pagination": {"currentPage": page, "limit": limit, "totalItems": total, "totalPages": max(1, -(-total // limit))},
        "documents": documents,
    }


@dataclass
class MemoryExportSection:
    heading: str
    description: str
    memories: list[BrainMemory]


def _labels_for(memory: BrainMemory) -> list[str]:
    labels = (memory.metadata or {}).get(BRAIN_TAG_LABELS_METADATA_KEY)
    if not isinstance(labels, list):
        return []
    return list(dict.fromkeys(label.strip() for label in labels if isinstance(label, str) and label.strip()))


def _memory_line(memory: BrainMemory) -> str:
    text = re.sub(r"\s*\n\s*", " ", memory.memory).strip()
    text = re.sub(r"([\\`*_\[\]<>])", r"\\\1", text)
    meta = " · ".join([memory.updated_at[:10], *_labels_for(memory)])
    return f"- {text} _({meta})_"


def memories_to_markdown(org_name: str | None, exported_at: datetime, sections: list[MemoryExportSection]) -> str:
    total = sum(len(section.memories) for section in sections)
    title = f"# What Company Brain remembers about {org_name.strip()}" if org_name and org_name.strip() else "# What Company Brain remembers"
    stamp = exported_at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    lines = [title, "", f"Exported {stamp}. {total} {'memory' if total == 1 else 'memories'}, newest first."]
    for section in sections:
        lines += ["", f"## {section.heading}", "", section.description, ""]
        if not section.memories:
            lines.append("_Nothing here yet._")
            continue
        for memory in sorted(section.memories, key=lambda m: m.updated_at, reverse=True):
            lines.append(_memory_line(memory))
    return "\n".join(lines) + "\n"


def memory_export_filename(exported_at: datetime) -> str:
    return f"company-brain-memories-{exported_at.astimezone(UTC).strftime('%Y-%m-%d')}.md"


def export_brain_memories(user_id: str, org_name: str | None = None) -> tuple[str, str]:
    """Markdown export of the shared brain and the user's own memories. Returns (filename, markdown)."""
    exported_at = datetime.now(UTC)
    shared = list_brain_memories([SHARED_TEAM_BRAIN_CONTAINER_TAG], limit=EXPORT_LIMIT)
    personal = list_brain_memories([private_container_tag_for(user_id)], limit=EXPORT_LIMIT)
    markdown = memories_to_markdown(
        org_name,
        exported_at,
        [
            MemoryExportSection("Shared team brain", "What everyone in the workspace can draw on.", shared),
            MemoryExportSection("Only you", "Your private memories. Nobody else's export includes these.", personal),
        ],
    )
    return memory_export_filename(exported_at), markdown
