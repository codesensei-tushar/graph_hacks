"""Port of company-brain `brain/memory/tree.ts`.

The topic tree is the tag key paths. DO SQL ``brain_memory_node`` rows (document,
container, node path) become ``(Memory)-[:FILED_UNDER]->(Tag)`` edges, so one memory
can sit under several topic nodes. The reset epoch (``brain_memory_state``) lives in
Redis; every mapping write checks it so a write from before a reset can't land after it.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

from src.db.falkordb_client import connect, kv
from src.domain.memory.documents import LIVE, get_brain_document, memories_for_documents, now_ms, update_memory_metadata
from src.domain.memory.tags import (
    BRAIN_TAG_LABELS_METADATA_KEY,
    BRAIN_TAGS_METADATA_KEY,
    NODE_PATH_DELIM,
    BrainMemoryTag,
    list_brain_memory_tags,
    node_label,
    node_path_segments,
    normalize_brain_tag_key,
    tag_node_id,
)

log = logging.getLogger(__name__)

OUTLINE_TAG_LIMIT = 500
TOPIC_TREE_TAG_KINDS = {"topic", "project", "customer", "team"}
_RESET_EPOCH_KEY = "brain:memory:reset_epoch"


def is_topic_tree_tag(tag: BrainMemoryTag) -> bool:
    return tag.kind in TOPIC_TREE_TAG_KINDS


@dataclass
class BrainTreeNode:
    path: str
    label: str
    depth: int
    subtag_count: int
    children: list["BrainTreeNode"] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "label": self.label,
            "depth": self.depth,
            "subtagCount": self.subtag_count,
            "children": [child.to_dict() for child in self.children],
        }


@dataclass(frozen=True)
class BrainNodeMapping:
    document_id: str
    container_tag: str
    node_path: str


@dataclass
class HydratedBrainNodeMemory:
    document_id: str
    memory_id: str
    memory: str
    updated_at: datetime


# --- reset epoch ----------------------------------------------------------------------


def get_brain_memory_reset_epoch() -> int:
    raw = kv().get(_RESET_EPOCH_KEY)
    return int(raw) if raw is not None else 0


def is_brain_memory_reset_epoch_current(epoch: int) -> bool:
    return get_brain_memory_reset_epoch() == epoch


def advance_brain_memory_reset_epoch() -> int:
    return int(kv().incr(_RESET_EPOCH_KEY))


# --- node mappings ---------------------------------------------------------------------


def upsert_brain_memory_node(mapping: BrainNodeMapping, expected_reset_epoch: int) -> bool:
    document_id = mapping.document_id.strip()
    container_tag = mapping.container_tag.strip()
    node_path = normalize_brain_tag_key(mapping.node_path)
    if not document_id or not container_tag or node_path == "other":
        return False
    if not is_brain_memory_reset_epoch_current(expected_reset_epoch):
        return False
    _, graph = connect()
    now = now_ms()
    rows = graph.query(
        """
        MATCH (m:Memory {id: $doc})
        MERGE (t:Tag {id: $tag})
        ON CREATE SET t.container = $scope, t.key = $path, t.label = $label, t.kind = 'topic',
                      t.implicit = true, t.use_count = 0, t.created_at = $now, t.updated_at = $now, t.last_used_at = $now
        MERGE (m)-[r:FILED_UNDER]->(t)
        ON CREATE SET r.created_at = $now
        SET r.updated_at = $now
        RETURN m.id
        """,
        {"doc": document_id, "tag": tag_node_id(container_tag, node_path), "scope": container_tag, "path": node_path,
         "label": node_label(node_path), "now": now},
    ).result_set
    return bool(rows)


def list_brain_memory_node_mappings(container_tags: list[str], node_path: str, descendants: bool) -> list[BrainNodeMapping]:
    path = normalize_brain_tag_key(node_path)
    if path == "other":
        return []
    containers = list(dict.fromkeys(tag.strip() for tag in container_tags if tag and tag.strip()))
    if not containers:
        return []
    _, graph = connect()
    rows = graph.query(
        """
        MATCH (m:Memory)-[:FILED_UNDER]->(t:Tag)
        WHERE t.container IN $scopes AND (t.key = $path OR ($descendants AND t.key STARTS WITH $prefix))
        RETURN m.id, t.container, t.key ORDER BY m.id
        """,
        {"scopes": containers, "path": path, "prefix": f"{path}{NODE_PATH_DELIM}", "descendants": descendants},
    ).result_set
    return [BrainNodeMapping(document_id=d, container_tag=c, node_path=p) for d, c, p in rows]


def _delete_brain_memory_node_mappings(mappings: list[BrainNodeMapping]) -> None:
    _, graph = connect()
    for mapping in mappings:
        graph.query(
            "MATCH (m:Memory {id: $doc})-[r:FILED_UNDER]->(t:Tag {container: $scope}) DELETE r",
            {"doc": mapping.document_id, "scope": mapping.container_tag},
        )


def reconcile_brain_memory_node_mappings(mappings: list[BrainNodeMapping]) -> list[BrainNodeMapping]:
    """Drop mappings whose memory is gone or forgotten (supermemory: failed documents)."""
    if not mappings:
        return []
    _, graph = connect()
    rows = graph.query(
        f"MATCH (m:Memory) WHERE m.id IN $ids AND {LIVE} RETURN m.id",
        {"ids": list({m.document_id for m in mappings}), "now": now_ms()},
    ).result_set
    valid = {row[0] for row in rows}
    _delete_brain_memory_node_mappings([m for m in mappings if m.document_id not in valid])
    return [m for m in mappings if m.document_id in valid]


def repoint_brain_memory_nodes(
    container_tag: str, document_ids: list[str], expected_path: str, new_path: str, expected_reset_epoch: int
) -> list[str]:
    scope = container_tag.strip()
    parent_path = normalize_brain_tag_key(expected_path)
    node_path = normalize_brain_tag_key(new_path)
    ids = list(dict.fromkeys(i.strip() for i in document_ids if i and i.strip()))
    if not scope or not ids or parent_path == "other" or node_path == "other":
        return []
    if not is_brain_memory_reset_epoch_current(expected_reset_epoch):
        return []
    _, graph = connect()
    moved = []
    now = now_ms()
    for document_id in ids:
        rows = graph.query(
            """
            MATCH (m:Memory {id: $doc})-[old:FILED_UNDER]->(:Tag {id: $parent})
            DELETE old
            WITH m
            MERGE (t:Tag {id: $child})
            ON CREATE SET t.container = $scope, t.key = $path, t.label = $label, t.kind = 'topic',
                          t.implicit = true, t.use_count = 0, t.created_at = $now, t.updated_at = $now, t.last_used_at = $now
            MERGE (m)-[r:FILED_UNDER]->(t)
            ON CREATE SET r.created_at = $now
            SET r.updated_at = $now
            RETURN m.id
            """,
            {"doc": document_id, "parent": tag_node_id(scope, parent_path), "child": tag_node_id(scope, node_path),
             "scope": scope, "path": node_path, "label": node_label(node_path), "now": now},
        ).result_set
        if rows:
            moved.append(rows[0][0])
    return moved


def repoint_brain_memory_metadata(document_ids: list[str], old_node_path: str, new_path: str, new_label: str) -> None:
    """Add the child key to memories under the parent (never remove the parent: other sources may justify it)."""
    ids = list(dict.fromkeys(i.strip() for i in document_ids if i and i.strip()))
    old_key = normalize_brain_tag_key(old_node_path)
    new_key = normalize_brain_tag_key(new_path)
    if not ids or old_key == new_key:
        return
    for row in memories_for_documents(ids):
        if row.container_tag is None:
            continue
        metadata = dict(row.metadata or {})
        keys = metadata.get(BRAIN_TAGS_METADATA_KEY)
        if not isinstance(keys, list) or old_key not in keys or new_key in keys:
            continue
        metadata[BRAIN_TAGS_METADATA_KEY] = [*keys, new_key]
        labels = metadata.get(BRAIN_TAG_LABELS_METADATA_KEY)
        if isinstance(labels, list):
            metadata[BRAIN_TAG_LABELS_METADATA_KEY] = [*labels, new_label]
        update_memory_metadata(row.id, row.container_tag, row.memory, metadata)


def clear_brain_memory_registry() -> None:
    _, graph = connect()
    graph.query("MATCH (t:Tag) DETACH DELETE t")


def pick_node_paths(tags: list[BrainMemoryTag]) -> list[str]:
    """Every topic-tree tag is its own node, so a memory is reachable under each branch."""
    return list(dict.fromkeys(tag.key for tag in tags if is_topic_tree_tag(tag)))


# --- outline ---------------------------------------------------------------------------


def outline_brain_tree(current_container_tags: list[str] | None = None) -> list[BrainTreeNode]:
    tags = [t for t in list_brain_memory_tags(current_container_tags, limit=OUTLINE_TAG_LIMIT) if is_topic_tree_tag(t)]
    roots: dict[str, dict] = {}

    def ensure(level: dict, path: str, depth: int) -> dict:
        node = level.get(path)
        if node is None:
            node = {"path": path, "label": node_label(path), "depth": depth, "count": 0, "children": {}}
            level[path] = node
        return node

    for tag in tags:
        level = roots
        prefix = ""
        for i, segment in enumerate(node_path_segments(tag.key)):
            prefix = f"{prefix}{NODE_PATH_DELIM}{segment}" if prefix else segment
            node = ensure(level, prefix, i)
            node["count"] += 1
            if prefix == tag.key:
                node["label"] = tag.label
            level = node["children"]

    def to_out(node: dict) -> BrainTreeNode:
        children = sorted(node["children"].values(), key=lambda c: -c["count"])
        return BrainTreeNode(node["path"], node["label"], node["depth"], node["count"], [to_out(c) for c in children])

    return [to_out(node) for node in sorted(roots.values(), key=lambda n: -n["count"])]


def render_brain_tree_outline(nodes: list[BrainTreeNode], max_roots: int = 20, max_children: int = 6) -> str:
    lines = []
    for root in nodes[:max_roots]:
        kids = [child.label for child in root.children[:max_children]]
        more = f", +{len(root.children) - max_children}" if len(root.children) > max_children else ""
        suffix = f" › {', '.join(kids)}{more}" if kids else ""
        lines.append(f"- {root.label}{suffix}")
    if len(nodes) > max_roots:
        lines.append(f"- (+{len(nodes) - max_roots} more topics)")
    return "\n".join(lines)


# --- hydration -------------------------------------------------------------------------


def hydrate_live_brain_node_mappings(
    mappings: list[BrainNodeMapping],
    limit_per_chunk: int,
    before: datetime | None = None,
    after: datetime | None = None,
) -> list[HydratedBrainNodeMemory]:
    by_container: dict[str, list[str]] = {}
    for mapping in mappings:
        by_container.setdefault(mapping.container_tag, []).append(mapping.document_id)
    rows: list[HydratedBrainNodeMemory] = []
    for container_tag, document_ids in by_container.items():
        page = []
        for memory in memories_for_documents(document_ids):
            if memory.container_tag != container_tag:
                continue
            if before and memory.updated_at >= before:
                continue
            if after and memory.updated_at < after:
                continue
            page.append(HydratedBrainNodeMemory(memory.document_id, memory.id, memory.memory, memory.updated_at))
        page.sort(key=lambda r: r.updated_at, reverse=True)
        rows.extend(page[: max(1, limit_per_chunk)])
    return sorted(rows, key=lambda r: r.updated_at, reverse=True)


def hydrate_live_brain_node_documents(mappings: list[BrainNodeMapping]) -> list[HydratedBrainNodeMemory]:
    rows = []
    for document_id in dict.fromkeys(m.document_id for m in mappings):
        doc = get_brain_document(document_id)
        text = (doc.content or "").strip() if doc else ""
        if not doc or not text:
            continue
        rows.append(HydratedBrainNodeMemory(doc.id, doc.id, text, datetime.fromisoformat(doc.created_at.replace("Z", "+00:00"))))
    return sorted(rows, key=lambda r: r.updated_at, reverse=True)


def fetch_subtree_brain_memories(
    container_tags: list[str],
    node_path: str,
    limit: int,
    before: datetime | None = None,
    after: datetime | None = None,
) -> list[dict]:
    """The node and all descendants, newest first: [{memory, updated_at}]."""
    mappings = reconcile_brain_memory_node_mappings(list_brain_memory_node_mappings(container_tags, node_path, descendants=True))
    if not mappings:
        return []
    rows = hydrate_live_brain_node_mappings(mappings, limit, before, after)
    seen: set[str] = set()
    out = []
    for row in rows:
        memory = row.memory.strip()
        if not memory or row.memory_id in seen:
            continue
        seen.add(row.memory_id)
        out.append({"memory": memory, "updated_at": row.updated_at})
        if len(out) >= limit:
            break
    return out
