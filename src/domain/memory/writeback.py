"""Port of company-brain `brain/memory/writeback.ts`.

Which container a Slack memory is written to, plus the memory document schema the
model proposes, custom ids, and the write request built from both. Also hosts
``slackMemoryScopeForTurn`` from `brain/slack/turn.ts`. Containers are FalkorDB
Scope nodes here instead of supermemory container tags.

| Scope            | Container               |
|------------------|-------------------------|
| public channel   | org_shared              |
| DM (personal)    | user_<slack user id>    |
| private / mpim   | slack_channel_<id>      |

A message writes to exactly one container, the narrowest room it was said in.
A DM without a known user writes nothing.
"""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from src.domain.memory.containers import (
    AGENT_SELF_CONTAINER_TAG,
    CONNECTOR_SCOPE_PREFIX,
    ORG_INTERNAL_CONTAINER_TAG,
    SHARED_TEAM_BRAIN_CONTAINER_TAG,
    private_container_tag_for,
    private_slack_channel_container_tag,
)
from src.domain.memory.tags import (
    BRAIN_MEMORY_TAG_KINDS,
    BRAIN_TAG_LABELS_METADATA_KEY,
    BRAIN_TAGS_METADATA_KEY,
    MAX_BRAIN_TAGS_PER_MEMORY,
    BrainMemoryTag,
    channel_brain_tag_key,
    normalize_brain_memory_tags,
)
from src.integrations.slack.client import SlackConversationInfo
from src.integrations.slack.events import is_private_slack_channel

__all__ = [
    "AGENT_SELF_CONTAINER_TAG",
    "ORG_INTERNAL_CONTAINER_TAG",
    "SHARED_TEAM_BRAIN_CONTAINER_TAG",
    "private_container_tag_for",
    "private_slack_channel_container_tag",
]

ScopeKind = Literal["shared", "dm", "private_channel"]


class BrainMemoryTagInput(BaseModel):
    key: str = Field(
        description=(
            "Stable tag key that doubles as a topic-tree path: use '/' to nest from general to specific "
            "(e.g. product/billing/invoices), and '_' within a single segment. Use person_<slack_user_id> for "
            "teammates (flat, no '/'); use nested topic/project/customer/team paths for durable non-person subjects. "
            "If list_memory_tags/outline_memory_tree contains the concept, use its exact canonical key; create a new "
            "key only when no existing tag covers the subject."
        )
    )
    label: str = Field(description="Human-readable tag label.")
    kind: Literal[BRAIN_MEMORY_TAG_KINDS] = Field(description="Tag category.")  # type: ignore[valid-type]
    description: str | None = Field(default=None, description="Short boundary for when this tag should be reused.")


class MemoryDoc(BaseModel):
    """One memory the model proposes to save. Field descriptions are the model-facing contract."""

    title: str = Field(description="A short title (a few words) for this memory.")
    content: str = Field(
        description=(
            "ONE detailed-but-compact, self-contained memory about one coherent durable subject. Keep related "
            "decisions, rationale, owners, and implications together. It must be useful if read alone, without "
            "sibling memories or thread context. Split only independently retrievable subjects, not supporting "
            "details or people within the same subject. Write people and things by their human-readable name; never "
            "put a raw Slack user id (e.g. U0AB123), an @-mention token, or a tag key into the memory text. Do not "
            "save volatile live-app results, bare entity/name/domain stubs, tag labels, or identifying details by "
            "themselves; fold supporting details into the substantive memory. A stable inference from live results "
            "is allowed only when it is likely to remain useful after the results change."
        )
    )
    sources: list[str] = Field(default_factory=list, description="Authoritative source links gathered this turn (may be empty).")
    event_date: str | None = Field(
        default=None,
        description=(
            "If this memory is about a dated event, decision, deadline, incident, or status change, its date as "
            "YYYY-MM-DD. Omit for timeless facts. Powers recency/staleness."
        ),
    )
    tags: list[BrainMemoryTagInput] = Field(
        min_length=1,
        max_length=MAX_BRAIN_TAGS_PER_MEMORY,
        description=(
            "The fewest stable tags that retrieve this memory well, usually one to three. Use exact existing keys "
            "from list_memory_tags/outline_memory_tree whenever they cover the concept; do not create synonyms, "
            "spelling variants, or redundant parent/child tags. Include person_<slack_user_id> only for a primary "
            "person and use a nested topic/project/customer/team path for the durable subject."
        ),
    )


@dataclass
class MemoryDocInput:
    """A MemoryDoc as the backend receives it: tags may be partial or missing."""

    title: str
    content: str
    sources: list[str] = field(default_factory=list)
    event_date: str | None = None
    tags: list = field(default_factory=list)
    internal_custom_id_override: str | None = None
    # Profile buckets the memory belongs to (supermemory classified these; here the writer sets them).
    buckets: list[str] = field(default_factory=list)

    @classmethod
    def from_doc(cls, doc: MemoryDoc | dict) -> "MemoryDocInput":
        if isinstance(doc, MemoryDoc):
            doc = doc.model_dump()
        return cls(
            title=doc.get("title") or "",
            content=doc.get("content") or "",
            sources=list(doc.get("sources") or []),
            event_date=doc.get("event_date") or doc.get("eventDate"),
            tags=list(doc.get("tags") or []),
            internal_custom_id_override=doc.get("internal_custom_id_override"),
            buckets=list(doc.get("buckets") or []),
        )


MemoryWriteback = MemoryDocInput | list[MemoryDocInput] | None


def memory_docs_from_writeback(memory) -> list[MemoryDocInput]:
    if not memory:
        return []
    docs = memory if isinstance(memory, list) else [memory]
    out = []
    for doc in docs:
        if doc is None:
            continue
        doc = doc if isinstance(doc, MemoryDocInput) else MemoryDocInput.from_doc(doc)
        if doc.content and doc.content.strip():
            out.append(doc)
    return out


def utc_date_string(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m-%d")


def normalize_memory_key(key: str) -> str:
    return re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", key.strip().lower()))


def slack_memory_custom_id(utc_date: str, memory_key: str, raw_key: str) -> str:
    hash_key = ":".join(part for part in (memory_key, raw_key.strip()) if part) or "memory"
    key_hash = hashlib.sha256(hash_key.encode()).hexdigest()
    return f"company-brain-slack:{utc_date}:{key_hash}"


@dataclass(frozen=True)
class SlackMemoryScope:
    kind: ScopeKind
    channel_id: str | None = None
    channel_type: str | None = None
    # Person the personal write tag belongs to.
    user_id: str | None = None
    # Slack user id, used to resolve the asker's private-channel read access.
    slack_user_id: str | None = None
    # Whether everyone who reads the answer is a full member of the workspace (no guest, no Slack
    # Connect). False drops org_internal and connector scopes from the reads (read_scope.py).
    internal_audience: bool = True


def slack_memory_container_tag(scope: SlackMemoryScope | None) -> str | None:
    if scope and scope.kind == "dm":
        return private_container_tag_for(scope.user_id) if scope.user_id else None
    if scope and scope.kind == "private_channel" and scope.channel_id:
        return private_slack_channel_container_tag(scope.channel_id)
    return SHARED_TEAM_BRAIN_CONTAINER_TAG


@dataclass
class SlackMemoryWriteRequest:
    content: str
    custom_id: str
    container_tag: str
    metadata: dict
    tags: list[BrainMemoryTag]
    # The memory text itself (content without the DOCUMENT_DATE anchor).
    memory: str = ""
    buckets: list[str] = field(default_factory=list)


def build_slack_memory_write_request(
    doc: MemoryDocInput,
    scope: SlackMemoryScope | None = None,
    now: datetime | None = None,
    allowed_person_slack_user_ids: list[str] | None = None,
    expected_reset_epoch: int | None = None,
) -> SlackMemoryWriteRequest | None:
    now = now or datetime.now(UTC)
    base = (doc.content or "").strip()
    if not base:
        return None
    event_date = (doc.event_date or "").strip()
    with_event = f"{base} (as of {event_date})" if event_date and event_date not in base else base
    # Anchor the current date. Channel-observe already prefixes its own DOCUMENT_DATE.
    content = with_event if with_event.startswith("DOCUMENT_DATE:") else f"DOCUMENT_DATE: {utc_date_string(now)}\n{with_event}"
    memory = re.sub(r"^DOCUMENT_DATE:[^\n]*\n", "", content).strip()

    tags = normalize_brain_memory_tags(doc.tags, allowed_person_slack_user_ids)
    if scope and scope.channel_id:
        # Ambient recall filters on the exact channel_<id> key; rewrite any channel_* tag to it.
        canonical = channel_brain_tag_key(scope.channel_id)
        seen: set[str] = set()
        rewritten: list[BrainMemoryTag] = []
        for tag in tags:
            nxt = (
                BrainMemoryTag(key=canonical, label=tag.label, kind="channel", description=tag.description)
                if tag.key.startswith("channel_") and tag.key != canonical
                else tag
            )
            if nxt.key in seen:
                continue
            seen.add(nxt.key)
            rewritten.append(nxt)
        tags = rewritten
    tag_keys = [tag.key for tag in tags]
    key = doc.title or content[:64]
    memory_key = normalize_memory_key(key)
    ingestion_date = utc_date_string(now)
    internal_custom_id = (doc.internal_custom_id_override or "").strip()
    # Scope agent-backed ids to the reset generation so an old-generation write can't
    # dedupe onto (and then stale-delete) a post-reset document.
    generation = "" if expected_reset_epoch is None else f":epoch{expected_reset_epoch}"
    custom_id = (
        internal_custom_id
        if internal_custom_id.startswith("company-brain-channel-observe:") or internal_custom_id.startswith("company-brain-research:")
        else slack_memory_custom_id(ingestion_date, memory_key, f"{key}:{','.join(tag_keys)}:{content}{generation}")
    )
    container_tag = slack_memory_container_tag(scope)
    if not container_tag:
        return None
    metadata: dict = {
        "source": "company-brain",
        "source_type": "company-brain-slack",
        "event_from": "slack",
        "ingestion_date": ingestion_date,
        "memory_scope": scope.kind if scope else "shared",
        "memory_key": memory_key,
        "title": doc.title,
    }
    if scope and scope.channel_id:
        metadata["slack_channel_id"] = scope.channel_id
    if scope and scope.channel_type:
        metadata["slack_channel_type"] = scope.channel_type
    if doc.sources:
        metadata["sources"] = list(doc.sources)
    if event_date:
        metadata["event_date"] = event_date
    if tag_keys:
        metadata[BRAIN_TAGS_METADATA_KEY] = tag_keys
        metadata[BRAIN_TAG_LABELS_METADATA_KEY] = [tag.label for tag in tags]
    return SlackMemoryWriteRequest(
        content=content,
        custom_id=custom_id,
        container_tag=container_tag,
        metadata=metadata,
        tags=tags,
        memory=memory,
        buckets=list(doc.buckets),
    )


def slack_memory_scope_for_turn(
    *,
    is_dm: bool,
    channel: str,
    channel_type: str | None = None,
    user_id: str | None = None,
    slack_user_id: str | None = None,
    conversation_info: SlackConversationInfo | None = None,
) -> SlackMemoryScope:
    if is_dm:
        return SlackMemoryScope(
            kind="dm",
            channel_id=channel,
            channel_type=channel_type,
            user_id=user_id,
            slack_user_id=slack_user_id,
        )

    # Privacy signals are monotonic: a live private event wins over cached public
    # conversation info, and cached private info stays the safer scope when the
    # event is ambiguous. An event without channel_type (app_mention) says nothing about
    # a C-channel, so conversations.info decides: a channel it reports public is public.
    if channel_type is None and conversation_info is not None and isinstance(conversation_info.is_private, bool) and not (channel or "").startswith("G"):
        is_private = conversation_info.is_private
    else:
        is_private = is_private_slack_channel(channel, channel_type) or (
            conversation_info is not None and conversation_info.is_private is True
        )
    if is_private:
        return SlackMemoryScope(kind="private_channel", channel_id=channel, channel_type=channel_type, user_id=user_id)
    return SlackMemoryScope(kind="shared", channel_id=channel, channel_type=channel_type)


def slack_memory_writer_user_id(scope: SlackMemoryScope | None, fallback_user_id: str | None) -> str | None:
    """Port of ``slackMemoryWriterUserId`` (turn.ts)."""
    if scope and scope.kind in ("dm", "private_channel"):
        return scope.user_id
    return fallback_user_id


def scope_label(tag: str, channel_names: dict[str, str] | None = None) -> str:
    """How a container is cited back to the asker."""
    if tag == SHARED_TEAM_BRAIN_CONTAINER_TAG:
        return "public channels"
    if tag == AGENT_SELF_CONTAINER_TAG:
        return "what I've learned about myself"
    if tag == ORG_INTERNAL_CONTAINER_TAG:
        return "connected tools"
    if tag.startswith("slack_channel_"):
        channel_id = tag.removeprefix("slack_channel_")
        name = (channel_names or {}).get(channel_id)
        return f"#{name}" if name else f"<#{channel_id}>"
    if tag.startswith("user_"):
        return "your DMs"
    if tag.startswith("github_repo_"):
        name = (channel_names or {}).get(tag)
        return f"the {name} repo" if name else "a private GitHub repo"
    if tag.startswith(CONNECTOR_SCOPE_PREFIX):
        name = (channel_names or {}).get(tag)
        return f"the restricted {name}" if name else "a restricted connected project"
    return tag
