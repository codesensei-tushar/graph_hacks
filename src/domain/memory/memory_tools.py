"""Memory tools the agent calls, as plain functions.

Port of the memory tools in company-brain `brain/turn/context-tools.ts`
(recall_tagged_memories, list_memory_tags, outline_memory_tree, read_memory_node),
`brain/tools/forget-memories.ts` (forget_memories) and search_company_brain. Each
takes the turn's memory scope and reads only inside its read surface. Tool
descriptions are kept so the agent can register them as-is.
"""

import logging
from datetime import datetime

from src.domain.memory.containers import AGENT_SELF_CONTAINER_TAG
from src.domain.memory.documents import forget_matching_memories
from src.domain.memory.profile_recall import BrainProfileRecallInput, build_brain_profile_context
from src.domain.memory.read_scope import resolve_brain_read_container_tags
from src.domain.memory.search_brain import brain_card_title, count_brain_results, format_brain_results, search_brain
from src.domain.memory.tags import list_brain_memory_tags
from src.domain.memory.tree import fetch_subtree_brain_memories, outline_brain_tree
from src.domain.memory.writeback import SlackMemoryScope, slack_memory_container_tag

log = logging.getLogger(__name__)

READ_NODE_PAGE = 250
FORGET_DEFAULT_THRESHOLD = 0.5
FORGET_DEFAULT_MAX = 100
FORGET_SAMPLE_LIMIT = 8

TOOL_DESCRIPTIONS = {
    "search_company_brain": "Search the Company Brain's memories (everything this conversation may read) for evidence about a question.",
    "recall_tagged_memories": "Fetch compact, previously tagged durable memories about a person, project, customer, team, or recurring topic. Use when those latest known facts would help; use search_company_brain for a full evidence-backed investigation.",
    "list_memory_tags": "List existing Company Brain memory tags on demand before save_memory when reusing canonical person, project, customer, team, or topic tags matters. Do not call for ordinary retrieval questions.",
    "outline_memory_tree": "Show the Company Brain topic tree: the hierarchy of durable-memory subjects with rough sizes. Use it to see what is known and decide which node to read, then call read_memory_node.",
    "read_memory_node": "Read the durable memories under a topic-tree node — the node and all its descendants — newest-updated first, 250 per page. Pass `before` = nextCursor to page; pass `after` for a time window.",
    "forget_memories": "Forget memories in bulk by meaning. ALWAYS preview first with dry_run=True, then report the count and a few samples. To proceed, call again with dry_run=False AND the ids from the preview; that pauses for the requester's approval.",
}


def _read_tags(scope: SlackMemoryScope | None, container_tags: list[str] | None) -> list[str]:
    return resolve_brain_read_container_tags(scope, container_tags)


def search_company_brain(query: str, scope: SlackMemoryScope | None, container_tags: list[str] | None = None, focus_tags: list[str] | None = None) -> dict:
    found = search_brain(query, scope, focus_tags=focus_tags, container_tags_override=container_tags)
    formatted = format_brain_results(found["results"])
    count = count_brain_results(formatted)
    return {"title": brain_card_title(count), "count": count, "results": formatted}


def recall_tagged_memories(
    query: str,
    scope: SlackMemoryScope | None,
    asker_slack_user_id: str | None = None,
    include_asker: bool = False,
    mentioned_slack_user_ids: list[str] | None = None,
    container_tags: list[str] | None = None,
) -> dict:
    context = build_brain_profile_context(
        BrainProfileRecallInput(
            query=query,
            sender_slack_user_id=asker_slack_user_id if include_asker else None,
            mentioned_slack_user_ids=list(dict.fromkeys(mentioned_slack_user_ids or [])),
            scope=scope,
            container_tags=container_tags,
        )
    )
    return {"found": True, "context": context} if context else {"found": False, "context": "No matching tagged memories found."}


def list_memory_tags(scope: SlackMemoryScope | None, kinds: list[str] | None = None, limit: int = 80, container_tags: list[str] | None = None) -> dict:
    tags = list_brain_memory_tags(_read_tags(scope, container_tags), kinds=kinds, limit=limit)
    return {"tags": [tag.to_dict() for tag in tags]}


def outline_memory_tree(scope: SlackMemoryScope | None, container_tags: list[str] | None = None) -> dict:
    return {"tree": [node.to_dict() for node in outline_brain_tree(_read_tags(scope, container_tags))]}


def read_memory_node(
    path: str,
    scope: SlackMemoryScope | None,
    before: str | None = None,
    after: str | None = None,
    container_tags: list[str] | None = None,
) -> dict:
    try:
        before_dt = datetime.fromisoformat(before.replace("Z", "+00:00")) if before else None
        after_dt = datetime.fromisoformat(after.replace("Z", "+00:00")) if after else None
    except ValueError:
        return {"path": path, "memories": [], "note": "before/after must be ISO timestamps (e.g. the nextCursor from a prior call)."}
    fetched = fetch_subtree_brain_memories(_read_tags(scope, container_tags), path, READ_NODE_PAGE + 1, before_dt, after_dt)
    page = fetched[:READ_NODE_PAGE]
    memories = [row["memory"] for row in page]
    if not memories:
        note = "No memories in this range under the node." if before or after else "No memories under this node."
        return {"path": path, "memories": [], "note": note}
    if len(fetched) > READ_NODE_PAGE:
        return {"path": path, "memories": memories, "nextCursor": page[-1]["updated_at"].isoformat().replace("+00:00", "Z")}
    return {"path": path, "memories": memories}


def forget_memories(
    query: str,
    scope: SlackMemoryScope | None,
    dry_run: bool = True,
    threshold: float | None = None,
    max_forget: int | None = None,
    reason: str | None = None,
    ids: list[str] | None = None,
) -> dict:
    """Runs over this conversation's write container plus agent-self. Never another team or channel.

    The apply call (dry_run=False) must ride the turn's approval card.
    """
    # Fail closed: never default a bulk forget to the shared brain when the scope is unknown.
    if scope is None:
        return {"error": "I can't tell which memory space this conversation belongs to, so I won't run a bulk forget without a resolved scope. Nothing was changed."}
    container_tag = slack_memory_container_tag(scope)
    if not container_tag:
        return {"error": "I can't resolve a memory space for this conversation, so I won't run a forget without a safe scope. Nothing was changed."}
    if not dry_run and not ids:
        return {"error": "To actually forget, first preview with dry_run=True, then call again with dry_run=False AND the ids it returned."}
    cap = max_forget or FORGET_DEFAULT_MAX
    results = []
    for tag in (container_tag, AGENT_SELF_CONTAINER_TAG):
        try:
            results.append(
                forget_matching_memories(
                    tag,
                    query=query,
                    dry_run=dry_run,
                    threshold=threshold if threshold is not None else FORGET_DEFAULT_THRESHOLD,
                    max_forget=cap,
                    reason=reason,
                    ids=None if dry_run else ids,
                )
            )
        except Exception:
            log.exception("[company-brain] forget_memories container=%s failed", tag)
    if not results:
        return {"error": "The forget request failed before anything was changed. No memories were removed."}
    partial_note = " One memory space couldn't be checked, so nothing there was touched." if len(results) < 2 else ""
    candidates = sorted((c for r in results for c in r.candidates or []), key=lambda c: -c.score)[:cap]
    forgotten = [c for r in results for c in r.forgotten or []]
    count = len(candidates) if dry_run else len(forgotten)
    if count == 0:
        return {
            "dryRun": dry_run,
            "count": 0,
            "message": f'Nothing matched "{query}" in this conversation\'s memories or in what you\'ve learned about yourself, so there\'s nothing to forget.{partial_note}',
        }
    summary = " ".join(r.summary for r in results if r.count > 0) + partial_note
    if dry_run:
        return {
            "dryRun": True,
            "count": count,
            "sampleMemories": [c.memory for c in candidates[:FORGET_SAMPLE_LIMIT]],
            "ids": [c.id for c in candidates],
            "summary": summary,
        }
    return {
        "dryRun": False,
        "forgottenCount": count,
        "sampleForgotten": [c.memory for c in forgotten[:FORGET_SAMPLE_LIMIT]],
        "forgetBatchId": ",".join(r.forget_batch_id for r in results if r.forget_batch_id) or None,
        "summary": summary,
    }
