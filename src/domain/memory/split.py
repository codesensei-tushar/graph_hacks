"""Port of company-brain `brain/memory/split.ts`.

When a topic node grows past 125 memories, a model partitions it into 2-4 child
subtopics and the memories are repointed under them. With no model the node stays
whole (the same outcome company-brain gets when the model call fails).
"""

import logging
import threading

from pydantic import BaseModel, Field

from src.domain.memory.tags import BrainMemoryTag, node_label, normalize_brain_tag_key, register_brain_memory_tags
from src.domain.memory.tree import (
    hydrate_live_brain_node_documents,
    is_brain_memory_reset_epoch_current,
    list_brain_memory_node_mappings,
    reconcile_brain_memory_node_mappings,
    repoint_brain_memory_metadata,
    repoint_brain_memory_nodes,
)
from src.integrations.llm.model import ModelRequest, ModelUnavailable, generate_object

log = logging.getLogger(__name__)

SPLIT_THRESHOLD = 125
SPLIT_SAMPLE_CAP = 160

# Serialize splits per node: bursty writes each fire a split check.
_split_in_flight: set[str] = set()
_lock = threading.Lock()

SPLIT_PROMPT = (
    'The memory topic "{node_path}" has grown too large to read at once. Split its memories into 2-4 coherent child '
    "subtopics. Give each child a short snake_case segment name (one path segment, no slashes), a one-line description, "
    "and the indexes of the memories that belong to it. Leave genuinely broad or parent-level memories unassigned (omit "
    "their index). Every child must be a real sub-theme, not a catch-all.\n\nMemories:\n{memories}"
)


class _Child(BaseModel):
    segment: str = Field(description="One snake_case path segment (no slashes).")
    description: str = Field(description="One line: what belongs here.")
    memory_indexes: list[int] = Field(description="Indexes (from the list) of memories in this child.")


class PartitionSchema(BaseModel):
    children: list[_Child] = Field(min_length=2, max_length=4)


def maybe_split_node(container_tag: str, node_path: str, expected_reset_epoch: int) -> None:
    if not is_brain_memory_reset_epoch_current(expected_reset_epoch):
        return
    lock_key = f"{container_tag}:{normalize_brain_tag_key(node_path)}"
    with _lock:
        if lock_key in _split_in_flight:
            return
        _split_in_flight.add(lock_key)
    try:
        mappings = reconcile_brain_memory_node_mappings(list_brain_memory_node_mappings([container_tag], node_path, descendants=False))
        if len(mappings) < SPLIT_THRESHOLD:
            return
        live = hydrate_live_brain_node_documents(mappings)
        if len(live) < SPLIT_THRESHOLD:
            return
        members = live[:SPLIT_SAMPLE_CAP]
        listing = "\n".join(f"[{i}] {' '.join(m.memory.split())}" for i, m in enumerate(members))
        try:
            raw = generate_object(
                ModelRequest(
                    profile="fast",
                    system="",
                    prompt=SPLIT_PROMPT.format(node_path=node_path, memories=listing),
                    schema=PartitionSchema.model_json_schema(),
                )
            )
            children = PartitionSchema.model_validate(raw).children
        except ModelUnavailable:
            log.info("[company-brain] split skipped node=%s: no model; node stays whole", node_path)
            return
        if not is_brain_memory_reset_epoch_current(expected_reset_epoch):
            return

        updates = []
        for child in children:
            segment = normalize_brain_tag_key(child.segment.replace("/", "_"))
            if segment == "other":
                continue
            new_path = normalize_brain_tag_key(f"{node_path}/{segment}")
            if new_path == normalize_brain_tag_key(node_path):
                continue
            document_ids = [members[i].document_id for i in child.memory_indexes if 0 <= i < len(members)]
            if document_ids:
                tag = BrainMemoryTag(key=new_path, label=node_label(new_path), kind="topic", description=child.description[:180])
                updates.append({"document_ids": document_ids, "new_path": new_path, "tag": tag})
        # Later child wins when indexes overlap.
        winner = {}
        for index, update in enumerate(updates):
            for document_id in update["document_ids"]:
                winner[document_id] = index
        successful = []
        for index, update in enumerate(updates):
            ids = list(dict.fromkeys(d for d in update["document_ids"] if winner.get(d) == index))
            if not ids:
                continue
            try:
                repoint_brain_memory_metadata(ids, node_path, update["new_path"], update["tag"].label)
            except Exception:
                log.exception("[brain-split-metadata] repoint failed")
                continue
            moved = repoint_brain_memory_nodes(container_tag, ids, node_path, update["new_path"], expected_reset_epoch)
            if moved:
                successful.append({**update, "document_ids": moved})
        if not is_brain_memory_reset_epoch_current(expected_reset_epoch):
            return
        child_tags = [u["tag"] for u in successful]
        if child_tags:
            register_brain_memory_tags(container_tag, child_tags)
        log.info(
            "[company-brain] split node=%s members=%d children=%d moved=%d",
            node_path, len(members), len(child_tags), sum(len(u["document_ids"]) for u in successful),
        )
    except Exception:
        log.exception("[brain-split] failed node=%s", node_path)
    finally:
        with _lock:
            _split_in_flight.discard(lock_key)
