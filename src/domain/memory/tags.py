"""Port of company-brain `brain/memory/tags.ts`.

The DO SQL table ``brain_memory_tag`` (container_tag, key) becomes FalkorDB ``Tag``
nodes with ``id = "<container>::<key>"``. Tag keys double as topic-tree paths
("a/b/c"); every prefix of a path gets a Tag node too (``implicit: true`` until the
key itself is registered) joined by ``(child)-[:PARENT]->(parent)`` edges, so the
tree is walkable in the graph. Memories carry their keys in ``brain_tags`` and link
``(Memory)-[:HAS_TAG]->(Tag)``.
"""

import logging
import re
import time
from dataclasses import asdict, dataclass
from typing import Literal

from src.db.falkordb_client import connect
from src.domain.memory.containers import SHARED_TEAM_BRAIN_CONTAINER_TAG

log = logging.getLogger(__name__)

MAX_BRAIN_TAGS_PER_MEMORY = 4
MAX_BRAIN_MEMORY_DOCS_PER_TURN = 3
MAX_BRAIN_OBSERVE_DOCS = 6
MAX_BRAIN_TAGS_IN_PROMPT = 80
SHARED_BRAIN_TAG_SCOPE = SHARED_TEAM_BRAIN_CONTAINER_TAG

# Memory properties the brain's tags live under (company-brain metadata keys).
BRAIN_TAGS_METADATA_KEY = "brain_tags"
BRAIN_TAG_LABELS_METADATA_KEY = "brain_tag_labels"

NODE_PATH_DELIM = "/"

BRAIN_MEMORY_TAG_KINDS = ("person", "channel", "topic", "project", "customer", "team", "preference", "task", "other")
BrainMemoryTagKind = Literal["person", "channel", "topic", "project", "customer", "team", "preference", "task", "other"]

TAG_KEY_MAX = 128
TAG_LABEL_MAX = 80
TAG_DESCRIPTION_MAX = 180


@dataclass
class BrainMemoryTag:
    key: str
    label: str
    kind: str
    description: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def tag_node_id(container_tag: str, key: str) -> str:
    return f"{container_tag}::{key}"


def normalize_brain_tag_key(key: str) -> str:
    out = key.strip().lower()
    out = re.sub(r"[^a-z0-9/]+", "_", out)
    out = re.sub(r"_*/_*", "/", out)
    out = re.sub(r"/{2,}", "/", out)
    out = re.sub(r"^[/_]+|[/_]+$", "", out)
    return out[:TAG_KEY_MAX] or "other"


def node_path_segments(path: str) -> list[str]:
    return [segment for segment in path.split(NODE_PATH_DELIM) if segment]


def node_label(path: str) -> str:
    segments = node_path_segments(path)
    return (segments[-1] if segments else path).replace("_", " ")


def _normalize_tag_kind(kind: object) -> str:
    return kind if isinstance(kind, str) and kind in BRAIN_MEMORY_TAG_KINDS else "other"


def _get(tag: object, field: str):
    if isinstance(tag, dict):
        return tag.get(field)
    return getattr(tag, field, None)


def normalize_brain_memory_tags(tags: list | None, allowed_person_slack_user_ids: list[str] | None = None) -> list[BrainMemoryTag]:
    """A supplied allowlist (even empty) enforces person keys; [] drops every person_* tag."""
    seen: set[str] = set()
    out: list[BrainMemoryTag] = []
    enforce_person_keys = allowed_person_slack_user_ids is not None
    allowed_person_keys = {person_brain_tag_key(i) for i in (allowed_person_slack_user_ids or []) if i and i.strip()}
    for tag in tags or []:
        key = normalize_brain_tag_key(str(_get(tag, "key") or _get(tag, "label") or ""))
        if not key or key in seen:
            continue
        is_person_key = key.startswith("person_")
        is_channel_key = key.startswith("channel_")
        if enforce_person_keys and is_person_key and key not in allowed_person_keys:
            continue
        kind = "person" if is_person_key else "channel" if is_channel_key else _normalize_tag_kind(_get(tag, "kind"))
        seen.add(key)
        label = str(_get(tag, "label") or key.replace("_", " ")).strip()
        description = (_get(tag, "description") or "").strip()
        out.append(
            BrainMemoryTag(
                key=key,
                label=label[:TAG_LABEL_MAX] or key,
                kind=kind,
                description=description[:TAG_DESCRIPTION_MAX] if description else None,
            )
        )
        if len(out) >= MAX_BRAIN_TAGS_PER_MEMORY:
            break
    return out


def person_brain_tag_key(slack_user_id: str) -> str:
    return normalize_brain_tag_key(f"person_{slack_user_id}")


def channel_brain_tag_key(channel_id: str) -> str:
    """A channel's memory tag, id-scoped like person_."""
    return normalize_brain_tag_key(f"channel_{channel_id}")


_KIND_PREFIXES = [f"{kind}_" for kind in BRAIN_MEMORY_TAG_KINDS]

GENERIC_TOKENS = {
    "data", "team", "plan", "work", "scale", "scaling", "core", "main", "prod", "test", "infra", "api", "app",
    "user", "users", "stuff", "misc", "general", "update", "updates", "status", "issue", "issues", "note", "notes", "thing",
}


def _tag_core(key: str) -> str:
    normalized = normalize_brain_tag_key(key)
    for prefix in _KIND_PREFIXES:
        if normalized.startswith(prefix):
            return normalized[len(prefix):]
    return normalized


def _core_tokens(core: str) -> list[str]:
    return [token for token in core.split("_") if len(token) >= 3]


def _token_akin(a: str, b: str) -> bool:
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 4 and long.startswith(short)


def _canonical_match(proposed, existing: list[BrainMemoryTag]) -> BrainMemoryTag | None:
    key = normalize_brain_tag_key(str(_get(proposed, "key") or _get(proposed, "label") or ""))
    if not key:
        return None
    core = _tag_core(key)
    kind = _get(proposed, "kind")
    # Same-kind only: project_acme must not canonicalize onto customer_acme.
    for tag in existing:
        if tag.kind == kind and _tag_core(tag.key) == core:
            return tag
    if key.startswith("person_") or key.startswith("channel_"):
        return None
    tokens = _core_tokens(core)
    for tag in existing:
        if tag.kind != kind or tag.key.startswith("person_") or tag.key.startswith("channel_"):
            continue
        existing_tokens = _core_tokens(_tag_core(tag.key))
        if any(token not in GENERIC_TOKENS and any(_token_akin(token, other) for other in existing_tokens) for token in tokens):
            return tag
    return None


def canonicalize_proposed_tags(existing: list[BrainMemoryTag], proposed: list | None) -> list:
    if not proposed or not existing:
        return list(proposed or [])
    out = []
    for tag in proposed:
        canon = _canonical_match(tag, existing)
        if canon:
            out.append(
                {
                    "key": canon.key,
                    "label": canon.label,
                    "kind": canon.kind,
                    "description": _get(tag, "description") or canon.description,
                }
            )
        else:
            out.append(tag)
    return out


def _normalize_brain_tag_scope(container_tag: str | None) -> str | None:
    trimmed = (container_tag or "").strip()
    return trimmed or None


def _ensure_path(graph, scope: str, key: str, now: int) -> None:
    """MERGE implicit Tag nodes for every prefix of ``key`` and PARENT edges between them."""
    segments = node_path_segments(key)
    prefix = ""
    parent = None
    for segment in segments:
        prefix = f"{prefix}{NODE_PATH_DELIM}{segment}" if prefix else segment
        graph.query(
            """
            MERGE (t:Tag {id: $id})
            ON CREATE SET t.container = $scope, t.key = $key, t.label = $label, t.kind = 'topic',
                          t.implicit = true, t.created_at = $now, t.updated_at = $now, t.last_used_at = $now, t.use_count = 0
            """,
            {"id": tag_node_id(scope, prefix), "scope": scope, "key": prefix, "label": node_label(prefix), "now": now},
        )
        if parent is not None:
            graph.query(
                "MATCH (c:Tag {id: $child}), (p:Tag {id: $parent}) MERGE (c)-[:PARENT]->(p)",
                {"child": tag_node_id(scope, prefix), "parent": tag_node_id(scope, parent)},
            )
        parent = prefix


def register_brain_memory_tags(container_tag: str, tags: list[BrainMemoryTag]) -> None:
    scope = _normalize_brain_tag_scope(container_tag)
    if not scope or not tags:
        return
    _, graph = connect()
    now = int(time.time() * 1000)
    for tag in tags:
        if NODE_PATH_DELIM in tag.key:
            _ensure_path(graph, scope, tag.key, now)
        graph.query(
            """
            MERGE (t:Tag {id: $id})
            ON CREATE SET t.created_at = $now, t.use_count = 0
            SET t.container = $scope, t.key = $key, t.label = $label, t.kind = $kind,
                t.description = coalesce($description, t.description), t.implicit = false,
                t.updated_at = $now, t.last_used_at = $now, t.use_count = coalesce(t.use_count, 0) + 1
            """,
            {
                "id": tag_node_id(scope, tag.key),
                "scope": scope,
                "key": tag.key,
                "label": tag.label,
                "kind": tag.kind,
                "description": tag.description,
                "now": now,
            },
        )


def list_brain_memory_tags(
    current_container_tags: list[str] | None = None,
    kinds: list[str] | None = None,
    limit: int | None = None,
) -> list[BrainMemoryTag]:
    """Tags across org_shared + the given read set, strongest first, one per key."""
    limit = limit or MAX_BRAIN_TAGS_IN_PROMPT
    wanted_kinds = [kind for kind in dict.fromkeys(kinds or []) if kind in BRAIN_MEMORY_TAG_KINDS]
    scopes = [
        scope
        for scope in dict.fromkeys(_normalize_brain_tag_scope(tag) for tag in [SHARED_BRAIN_TAG_SCOPE, *(current_container_tags or [])])
        if scope
    ]
    try:
        _, graph = connect()
        rows = graph.query(
            """
            MATCH (t:Tag)
            WHERE t.container IN $scopes AND coalesce(t.implicit, false) = false
              AND (size($kinds) = 0 OR t.kind IN $kinds)
            RETURN t.key, t.label, t.kind, t.description, t.use_count, t.updated_at
            ORDER BY t.use_count DESC, t.updated_at DESC
            """,
            {"scopes": scopes, "kinds": wanted_kinds},
        ).result_set
    except Exception:
        log.warning("[brain-memory-tags] list failed", exc_info=True)
        return []
    # The same key can live in several containers; keep the strongest.
    seen: set[str] = set()
    out: list[BrainMemoryTag] = []
    for key, label, kind, description, _count, _updated in rows:
        key = normalize_brain_tag_key(key)
        if key in seen:
            continue
        seen.add(key)
        out.append(BrainMemoryTag(key=key, label=label, kind=_normalize_tag_kind(kind), description=description))
        if len(out) >= limit:
            break
    return out


def fetch_tagged_brain_memories(container_tags: list[str], tag_keys: list[str], limit: int) -> list[str]:
    keys = [key for key in dict.fromkeys(normalize_brain_tag_key(k) for k in tag_keys) if key]
    if not keys:
        return []
    containers = [tag for tag in dict.fromkeys(container_tags) if tag]
    if not containers:
        return []
    from src.domain.memory.documents import list_brain_memories

    rows = list_brain_memories(container_tags=containers, tag_keys=keys, limit=limit)
    return [row.memory.strip() for row in rows if row.memory.strip()]


def _query_tokens(text: str) -> list[str]:
    return list(dict.fromkeys(token for token in re.split(r"[^a-z0-9]+", text.lower()) if len(token) >= 3))


def relevant_brain_tag_keys(
    tags: list[BrainMemoryTag],
    query: str | None = None,
    slack_user_ids: list[str] | None = None,
    limit: int = 12,
) -> list[str]:
    keys: list[str] = []
    for slack_user_id in slack_user_ids or []:
        if slack_user_id and slack_user_id.strip():
            key = person_brain_tag_key(slack_user_id)
            if key not in keys:
                keys.append(key)
    tokens = _query_tokens(query or "")
    for tag in tags:
        if tag.key in keys:
            continue
        haystack = " ".join([tag.key, tag.label, tag.description or ""]).lower()
        if any(token in haystack for token in tokens):
            keys.append(tag.key)
        if len(keys) >= limit:
            break
    return keys[:limit]
