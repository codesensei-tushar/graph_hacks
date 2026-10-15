"""Port of company-brain `brain/memory/profile-recall.ts`.

Two prompt blocks built from tagged memories inside the turn's read surface:

- ``build_ambient_brain_profile_context``: injected every turn. Topic map, static
  shared knowledge, the current channel's bucketed disposition, and the asker's and
  mentioned people's bucketed profiles.
- ``build_brain_profile_context``: the ``recall_tagged_memories`` tool. Memories under
  the tags relevant to a query, packed to a token budget, flagged when truncated.
"""

import logging
from dataclasses import dataclass, field

from src.domain.memory.documents import BrainMemory, list_brain_memories
from src.domain.memory.read_scope import resolve_brain_read_container_tags
from src.domain.memory.tags import (
    channel_brain_tag_key,
    fetch_tagged_brain_memories,
    list_brain_memory_tags,
    person_brain_tag_key,
    relevant_brain_tag_keys,
)
from src.domain.memory.tree import outline_brain_tree, render_brain_tree_outline
from src.domain.memory.writeback import SlackMemoryScope

log = logging.getLogger(__name__)

FLOOR_TOKEN_BUDGET = 20_000
FLOOR_TAG_KEYS = 10
FLOOR_CANDIDATE_LIMIT = 500
AMBIENT_STATIC_CANDIDATE_LIMIT = 500
AMBIENT_ASKER_CANDIDATE_LIMIT = 250
AMBIENT_MENTIONED_CANDIDATE_LIMIT = 25
AMBIENT_PERSON_COUNT = 8
AMBIENT_CHANNEL_CANDIDATE_LIMIT = 250


@dataclass
class BrainProfileRecallInput:
    query: str | None = None
    sender_slack_user_id: str | None = None
    mentioned_slack_user_ids: list[str] = field(default_factory=list)
    scope: SlackMemoryScope | None = None
    # Explicit read surface; replaces whatever scope would imply.
    container_tags: list[str] | None = None


def _estimate_tokens(text: str) -> int:
    return -(-len(text) // 4)


def _normalize_whitespace(value: str) -> str:
    return " ".join(value.split())


def _key(memory: str) -> str:
    return _normalize_whitespace(memory).lower()


def _compact(memory: str) -> str:
    compact = _normalize_whitespace(memory)
    return compact if len(compact) <= 300 else f"{compact[:297]}..."


def _only_bucketed(rows: list[BrainMemory]) -> list[BrainMemory]:
    return [row for row in rows if row.buckets]


def _dedupe_people(sender: str | None, mentioned: list[str]) -> list[tuple[str, str]]:
    people: list[tuple[str, str]] = []
    seen: set[str] = set()
    for slack_user_id, role in [(sender, "asker"), *((m, "mentioned person") for m in mentioned)]:
        sid = (slack_user_id or "").strip()
        if sid and sid not in seen:
            seen.add(sid)
            people.append((sid, role))
    return people


def _balance_bucket_rows(rows: list[BrainMemory]) -> list[tuple[str, str]]:
    selected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        key = _key(row.memory)
        if key in seen:
            continue
        seen.add(key)
        buckets = list(dict.fromkeys(b.strip() for b in row.buckets if b.strip())) or ["other"]
        selected.extend((bucket, row.memory) for bucket in buckets)
    return selected


def _group(rows: list[tuple[str, str]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for bucket, memory in rows:
        grouped.setdefault(bucket, []).append(memory)
    return grouped


def build_ambient_brain_profile_context(data: BrainProfileRecallInput) -> str | None:
    try:
        containers = resolve_brain_read_container_tags(data.scope, data.container_tags)
        if not containers:
            return None
        people = _dedupe_people(data.sender_slack_user_id, data.mentioned_slack_user_ids)[:AMBIENT_PERSON_COUNT]
        channel_tag = channel_brain_tag_key(data.scope.channel_id) if data.scope and data.scope.channel_id else None

        tagged_rows = sorted(
            list_brain_memories(containers, limit=AMBIENT_STATIC_CANDIDATE_LIMIT, with_buckets=True),
            key=lambda row: -row.source_count,
        )
        channel_rows = (
            _only_bucketed(list_brain_memories(containers, limit=AMBIENT_CHANNEL_CANDIDATE_LIMIT, tag_keys=[channel_tag], with_buckets=True))
            if channel_tag
            else []
        )
        person_rows = [
            _only_bucketed(
                list_brain_memories(
                    containers,
                    limit=AMBIENT_ASKER_CANDIDATE_LIMIT if role == "asker" else AMBIENT_MENTIONED_CANDIDATE_LIMIT,
                    tag_keys=[person_brain_tag_key(slack_user_id)],
                    with_buckets=True,
                )
            )
            for slack_user_id, role in people
        ]
        # Shared knowledge carries a brain tag but isn't about one person.
        static_rows = [row for row in tagged_rows if row.tags and not any(tag.startswith("person_") for tag in row.tags)]
        outline = render_brain_tree_outline(outline_brain_tree(containers))
        return _render_ambient_profile(static_rows, list(zip(people, person_rows, strict=True)), channel_rows, outline)
    except Exception:
        log.exception("[brain-ambient-profile] failed")
        return None


def _render_ambient_profile(static_rows, people, channel_rows, outline: str | None) -> str | None:
    seen_elsewhere: set[str] = set()
    rendered_people = []
    for (slack_user_id, role), rows in people:
        balanced = _balance_bucket_rows(rows)
        if not balanced:
            continue
        seen_elsewhere.update(_key(memory) for _bucket, memory in balanced)
        rendered_people.append((slack_user_id, role, balanced))
    rendered_channel = _balance_bucket_rows(channel_rows)
    seen_elsewhere.update(_key(memory) for _bucket, memory in rendered_channel)
    selected_static = []
    seen: set[str] = set()
    for row in static_rows:
        key = _key(row.memory)
        if not key or key in seen or key in seen_elsewhere:
            continue
        seen.add(key)
        selected_static.append(row.memory)

    sections: list[str] = []
    if outline:
        sections.append(
            "Topic map of durable shared knowledge (use recall_tagged_memories, or outline_memory_tree then read_memory_node, to pull a whole subject):\n"
            + outline
        )
    if selected_static:
        sections.append("\n".join(["Static profile:", *(f"- {_compact(m)}" for m in selected_static)]))
    if rendered_channel:
        lines = ["How this channel wants you to show up (disposition and recurring patterns for the current channel):"]
        for bucket, memories in _group(rendered_channel).items():
            lines += [f"{bucket}:", *(f"- {_compact(m)}" for m in memories)]
        sections.append("\n".join(lines))
    for slack_user_id, role, rows in rendered_people:
        lines = [f"Profile buckets for {role} (Slack {slack_user_id}):"]
        for bucket, memories in _group(rows).items():
            lines += [f"{bucket}:", *(f"- {_compact(m)}" for m in memories)]
        sections.append("\n".join(lines))
    if not sections:
        return None
    return "\n".join(["<ambient_brain_profile>", *sections, "</ambient_brain_profile>"])


def build_brain_profile_context(data: BrainProfileRecallInput) -> str | None:
    try:
        containers = resolve_brain_read_container_tags(data.scope, data.container_tags)
        tags = list_brain_memory_tags(containers)
        person_ids = [i for i in [data.sender_slack_user_id, *data.mentioned_slack_user_ids] if i and i.strip()]
        tag_keys = relevant_brain_tag_keys(tags, data.query, person_ids, limit=FLOOR_TAG_KEYS)
        fetched = fetch_tagged_brain_memories(containers, tag_keys, FLOOR_CANDIDATE_LIMIT)
        deduped = _dedupe_memories(fetched)
        memories = _pack_to_budget(deduped, FLOOR_TOKEN_BUDGET)
        # Not the whole set: packed under budget, hit the fetch cap, or hit the tag-key cap.
        truncated = len(memories) < len(deduped) or len(fetched) >= FLOOR_CANDIDATE_LIMIT or len(tag_keys) >= FLOOR_TAG_KEYS
        outline = render_brain_tree_outline(outline_brain_tree(containers))
        if not memories and not outline and not truncated:
            return None
        parts = ["<brain_memory_context>"]
        if outline:
            parts += ["Topic map (use outline_memory_tree / read_memory_node to go deeper):", outline, ""]
        if memories:
            parts.append("\n".join(["Relevant memories:", *(f"- {m}" for m in memories)]))
        if truncated:
            parts.append(
                f"(Showing {len(memories)} of {len(deduped)}+ matching memories — this is not the complete set; narrow the tag or use search_company_brain for the rest.)"
                if memories
                else "(Hit the retrieval limit before any memories fit here — narrow the tag or use search_company_brain to find matches.)"
            )
        parts.append("</brain_memory_context>")
        return "\n".join(parts)
    except Exception:
        log.exception("[brain-profile-recall] failed")
        return None


def _pack_to_budget(memories: list[str], budget: int) -> list[str]:
    out: list[str] = []
    used = 0
    for memory in memories:
        cost = _estimate_tokens(memory) + 2
        if used + cost > budget:
            break
        out.append(memory)
        used += cost
    return out


def _dedupe_memories(memories: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for memory in memories:
        key = _key(memory)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(memory)
    return out
