"""Port of company-brain ``brain/slack/channel-observe.ts``.

Watches public channels the bot is in. A channel message arms an observe job
(3 minutes out); the job spools new messages since a persisted cursor, asks the
model to curate durable clusters, writes them to shared memory, advances the
cursor, and re-arms while more remains. Bots and the brain's own replies are
spooled as context only (``AGENT_OR_BOT``), never as a fact source.

Ownership: one live job per ``team:channel``; every step re-checks that it still
owns the row, so a newer arm or a reset wins. Failures retry with backoff up to
``MAX_RETRY_ATTEMPTS``, then the channel is marked exhausted and only a fresh arm
(a "probe") may try again.

company-brain's DO SQL tables are Redis hashes here (``brain:observe:*``) and its
alarms are ``src.core.scheduler`` jobs. With no model configured the distill step
raises, so a batch takes company-brain's failure path: retry with backoff, then
exhaust. The legacy single-key migration is dropped (there is no legacy state).
"""

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime

from src.core import tenancy
from src.core.tenancy import bind
from src.core import scheduler
from src.db.falkordb_client import kv
from src.domain.memory.tags import MAX_BRAIN_OBSERVE_DOCS, person_brain_tag_key
from src.domain.memory.tree import get_brain_memory_reset_epoch, is_brain_memory_reset_epoch_current
from src.domain.memory.writeback import MemoryDocInput
from src.domain.slack.channels._compat import as_dict, field
from src.domain.slack.turn.prompt_batch import build_slack_prompt_batch
from src.integrations.llm.model import ModelRequest, generate_object
from src.integrations.slack.events import mentioned_user_ids

log = logging.getLogger(__name__)

OBSERVE_DELAY_SECONDS = 3 * 60
CONTINUE_DELAY_SECONDS = 1
RETRY_BASE_DELAY_SECONDS = 60
RETRY_MAX_DELAY_SECONDS = 15 * 60
MAX_RETRY_ATTEMPTS = 5
MAX_NEW_MESSAGES = 60
MAX_CONVO_CHARS = 8000
REPAIR_DELAYS_MS = (1000, 5000, 15_000)
# Spool label for bot/app authors so the distiller keeps them as context only.
BOT_AUTHOR_LABEL = "AGENT_OR_BOT"
JOB_RUN_CHANNEL_OBSERVE = "runChannelObserve"

_ROWS = "brain:observe:rows"
_CURSOR = "brain:observe:cursor"
_SCAN = "brain:observe:scan"
_RETRY = "brain:observe:retry"
_PROBE = "brain:observe:probe"
_arm_lock = threading.Lock()
_arm_sequence = 0

# company-brain BRAIN_CAPTURE_POLICY (brain/memory/profile-config.ts), used when the memory layer doesn't export it.
_FALLBACK_CAPTURE_POLICY = "Capture durable facts about people, ownership, decisions, and commitments; skip chatter."


def _capture_policy() -> str:
    try:
        from src.domain.memory.profile_config import BRAIN_CAPTURE_POLICY

        return BRAIN_CAPTURE_POLICY
    except ImportError:
        return _FALLBACK_CAPTURE_POLICY


def distill_system() -> str:
    return f"""You watch a batch of new Slack channel messages and CURATE what's worth remembering for the team's shared brain. You do NOT write the final memories — you select and group the durable material; a downstream extractor turns each group into durable memories, dedupes, and supersedes stale facts.

{_capture_policy()}

Lines labeled {BOT_AUTHOR_LABEL} (bots, apps, and the brain's own prior replies) are context only: read them to make human messages make sense, but never extract a memory from them and never record the brain's own statements as facts. Capture a fact only when a HUMAN states it.

Group the durable content into the fewest TAG-COHERENT clusters that remain independently retrievable. Each cluster covers ONE coherent subject sharing one tag set (a topic-tree path and/or its primary people). Keep related decisions, rationale, owners, and implications together even when several people are involved. Split only genuinely independent subjects, not supporting facts that merely add another person or possible tag. Keep the original wording and any dates — do NOT pre-atomize into single facts, and do NOT dump raw chatter. Prioritize durable CHANGES (ownership, responsibility, roles, decisions) and keep their dates in the text so the extractor can resolve recency. Retain explicit human commitments, blockers, and meaningful status changes with the capture policy's decay rules; exclude raw activity, live counts, and app-derived status snapshots. Skip pure chatter; if nothing durable, return an empty list.

For each cluster: content = the curated relevant material (faithful, dates preserved); tags = the fewest coherent topic-tree paths ('/'-nested, reuse exact existing paths rather than creating near-duplicates) + person_<slack_user_id> only for primary people. Leave eventDate unset — dates stay in the content for the extractor to assign per fact."""


_DISTILL_SCHEMA = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "maxItems": MAX_BRAIN_OBSERVE_DOCS,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "sources": {"type": "array", "items": {"type": "string"}},
                    "eventDate": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["title", "content", "sources", "tags"],
            },
        }
    },
    "required": ["memories"],
}


# --- durable retry (port of brain/turn/retry-state.ts) ---------------------------------


def can_schedule_durable_recovery(state: dict, probe_allowed: bool) -> bool:
    return not state["exhausted"] or probe_allowed


def advance_durable_retry(state: dict, max_attempts: int) -> tuple[dict, bool]:
    if state["exhausted"] or state["attempt"] >= max_attempts:
        return {"attempt": state["attempt"], "exhausted": True}, False
    return {"attempt": state["attempt"] + 1, "exhausted": False}, True


# --- state ------------------------------------------------------------------------------


def channel_key(payload: dict) -> str:
    return f"{payload['team_id']}:{payload['channel']}"


def _pending_key(key: str) -> str:
    return f"brain:observe:pending:{key}"


def _batch_key(key: str) -> str:
    return f"brain:observe:batch:{key}"


def _decode(value) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _next_arm_sequence() -> int:
    global _arm_sequence
    with _arm_lock:
        _arm_sequence = max(_arm_sequence + 1, int(time.time() * 1000) * 1000)
        return _arm_sequence


def _row(key: str) -> dict | None:
    raw = kv().hget(_ROWS, key)
    return json.loads(raw) if raw else None


def load_channel_retry_state(key: str) -> dict:
    raw = kv().hget(_RETRY, key)
    row = json.loads(raw) if raw else {}
    return {"attempt": row.get("attempt", 0), "exhausted": bool(row.get("exhausted"))}


def clear_channel_retry_state(key: str) -> None:
    kv().hdel(_RETRY, key)
    kv().hdel(_PROBE, key)


def _publish_channel_probe(key: str, armed_at: int) -> None:
    kv().hset(_PROBE, key, str(armed_at))


def _is_channel_probe_allowed(key: str, armed_at: int) -> bool:
    raw = kv().hget(_PROBE, key)
    return raw is not None and int(raw) == armed_at


def _claim_channel_probe(key: str, armed_at: int) -> bool:
    if not _is_channel_probe_allowed(key, armed_at):
        return False
    return bool(kv().hdel(_PROBE, key))


def _persist_channel_retry_state(key: str, attempt: int, exhausted: bool) -> None:
    kv().hset(_RETRY, key, json.dumps({"attempt": attempt, "exhausted": exhausted, "updated_at": int(time.time() * 1000)}))
    kv().hdel(_PROBE, key)


def _load_persisted_batch(key: str) -> dict | None:
    rows = {_decode(k): v for k, v in (kv().hgetall(_batch_key(key)) or {}).items()}
    if not rows:
        return None
    return json.loads(rows[min(rows)])


def stable_cluster_custom_id(team_id: str, key: str, from_ts: str, through_ts: str, index: int, reset_epoch: int) -> str:
    """reset_epoch scopes the id to a generation: an old-generation write can never
    dedupe onto (and then get the stale-cleanup delete of) a post-reset document."""
    digest = hashlib.sha256(f"{team_id}:{key}:{from_ts}:{through_ts}:{index}:{reset_epoch}".encode()).hexdigest()
    return f"company-brain-channel-observe:{digest}"


def get_observe_cursor(payload: dict) -> str:
    raw = kv().hget(_CURSOR, channel_key(payload))
    return _decode(raw) if raw else ""


def update_observe_cursor(key: str, last_ts: str) -> None:
    current = kv().hget(_CURSOR, key)
    if current is None or last_ts > _decode(current):
        kv().hset(_CURSOR, key, last_ts)


def _is_owned(key: str, schedule_id: str) -> bool:
    row = _row(key)
    return bool(row and row["schedule_id"] == schedule_id)


def _schedule_exists(job_id: str) -> bool:
    return scheduler.exists(job_id)


def _schedule_channel_observe(payload: dict, delay_seconds: float) -> str:
    job_id = uuid.uuid4().hex
    scheduler.schedule(delay_seconds, JOB_RUN_CHANNEL_OBSERVE, {**payload, "schedule_id": job_id}, job_id=job_id)
    return job_id


def _swap_owner(key: str, owned_schedule_id: str, new_schedule_id: str) -> bool:
    row = _row(key)
    if not row or row["schedule_id"] != owned_schedule_id:
        return False
    row["schedule_id"] = new_schedule_id
    kv().hset(_ROWS, key, json.dumps(row))
    return True


def _rearm_owned_channel_observe(payload: dict, owned_schedule_id: str, delay_seconds: float, retry_attempt: int) -> None:
    key = channel_key(payload)
    try:
        scheduled = _schedule_channel_observe({**_public(payload), "retry_attempt": retry_attempt}, delay_seconds)
    except Exception:
        _queue_channel_observe_repair(key)
        log.exception("[company-brain] channel-observe schedule failed")
        return
    try:
        if _swap_owner(key, owned_schedule_id, scheduled):
            return
    except Exception:
        scheduler.cancel(scheduled)
        _queue_channel_observe_repair(key)
        log.exception("[company-brain] channel-observe ownership handoff failed")
        return
    scheduler.cancel(scheduled)


def _retry_delay_seconds(attempt: int) -> int:
    return min(RETRY_BASE_DELAY_SECONDS * 2 ** max(0, attempt - 1), RETRY_MAX_DELAY_SECONDS)


def _retry_owned_channel_observe(payload: dict, key: str, schedule_id: str, error: BaseException | None = None) -> None:
    if not _is_owned(key, schedule_id):
        return
    state, should_schedule = advance_durable_retry(load_channel_retry_state(key), MAX_RETRY_ATTEMPTS)
    _persist_channel_retry_state(key, state["attempt"], state["exhausted"])
    if not should_schedule:
        _finish_owned_channel_observe(key, schedule_id)
        log.error(
            "[company-brain] channel-observe retries exhausted channel=%s %s",
            payload["channel"],
            type(error).__name__ if error else "unknown",
        )
        return
    _rearm_owned_channel_observe(payload, schedule_id, _retry_delay_seconds(state["attempt"]), state["attempt"])


def _finish_owned_channel_observe(key: str, schedule_id: str) -> None:
    if _is_owned(key, schedule_id):
        kv().hdel(_ROWS, key)


def _clear_terminal_channel_observe(key: str, schedule_id: str) -> None:
    _finish_owned_channel_observe(key, schedule_id)
    kv().hdel(_SCAN, key)
    kv().delete(_pending_key(key), _batch_key(key))
    clear_channel_retry_state(key)


def _reconcile_channel_observe_row(key: str) -> bool:
    scheduled = None
    try:
        row = _row(key)
        if not row:
            return True
        retry = load_channel_retry_state(key)
        if not can_schedule_durable_recovery(retry, _is_channel_probe_allowed(key, row["armed_at"])):
            _finish_owned_channel_observe(key, row["schedule_id"])
            scheduler.cancel(row["schedule_id"])
            return True
        if _schedule_exists(row["schedule_id"]):
            return True
        delay = (
            _retry_delay_seconds(retry["attempt"])
            if retry["attempt"] > 0 and not retry["exhausted"]
            else CONTINUE_DELAY_SECONDS
        )
        scheduled = _schedule_channel_observe(
            {
                "team_id": row["team_id"],
                "channel": row["channel"],
                "retry_attempt": retry["attempt"],
                "reset_epoch": row["reset_epoch"],
            },
            delay,
        )
        if not _swap_owner(key, row["schedule_id"], scheduled):
            scheduler.cancel(scheduled)
        return True
    except Exception:
        if scheduled:
            scheduler.cancel(scheduled)
        log.exception("[company-brain] channel-observe repair failed")
        return False


def _queue_channel_observe_repair(key: str) -> None:
    def repair() -> None:
        for delay_ms in REPAIR_DELAYS_MS:
            time.sleep(delay_ms / 1000)
            if _reconcile_channel_observe_row(key):
                return

    threading.Thread(target=bind(repair), name="observe-repair", daemon=True).start()


def reconcile_channel_observe_schedules() -> None:
    """Startup safety net: every owned row has a live job (or is retired)."""
    from src.domain import orgs

    for raw_key in kv().hkeys(_ROWS):
        key = _decode(raw_key)
        # The rows of every org share one hash: each is reconciled in its own team's org.
        with tenancy.org_context(orgs.org_ref_for_team(key.split(":", 1)[0])):
            if not _reconcile_channel_observe_row(key):
                _queue_channel_observe_repair(key)


def _public(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if k != "schedule_id"}


def _attempt_channel_observe_arm(payload: dict, key: str, armed_at: int) -> bool:
    existing = _row(key)
    if existing and existing["armed_at"] >= armed_at:
        return True
    retry = load_channel_retry_state(key)
    scheduled = None
    try:
        scheduled = _schedule_channel_observe({**payload, "retry_attempt": retry["attempt"]}, OBSERVE_DELAY_SECONDS)
        if not is_brain_memory_reset_epoch_current(payload.get("reset_epoch") or 0):
            scheduler.cancel(scheduled)
            return True
        current = _row(key)
        if current and current["armed_at"] > armed_at:
            scheduler.cancel(scheduled)
            return True
        kv().hset(
            _ROWS,
            key,
            json.dumps(
                {
                    "schedule_id": scheduled,
                    "team_id": payload["team_id"],
                    "channel": payload["channel"],
                    "armed_at": armed_at,
                    "reset_epoch": payload.get("reset_epoch") or 0,
                }
            ),
        )
        if load_channel_retry_state(key)["exhausted"]:
            _publish_channel_probe(key, armed_at)
        if current and current["schedule_id"] != scheduled:
            scheduler.cancel(current["schedule_id"])
        return True
    except Exception:
        if scheduled:
            _finish_owned_channel_observe(key, scheduled)
            scheduler.cancel(scheduled)
        log.exception("[company-brain] channel-observe initial arm failed")
        return False


def arm_channel_observe(payload: dict) -> None:
    """``payload``: team_id, channel. Called for public-channel messages the bot can see."""
    epoch_payload = {**payload, "reset_epoch": get_brain_memory_reset_epoch()}
    key = channel_key(epoch_payload)
    armed_at = _next_arm_sequence()
    if _attempt_channel_observe_arm(epoch_payload, key, armed_at):
        return

    def retry_arm() -> None:
        for delay_ms in REPAIR_DELAYS_MS:
            time.sleep(delay_ms / 1000)
            if _attempt_channel_observe_arm(epoch_payload, key, armed_at):
                return

    threading.Thread(target=bind(retry_arm), name="observe-arm-repair", daemon=True).start()


# --- spool + distill ---------------------------------------------------------------------


def _is_bot_authored_message(message: dict, bot_user_id: str | None) -> bool:
    """Bot/app output (incl. the brain's own replies) isn't evidence and would loop back into memory."""
    return bool(
        message.get("bot_id")
        or message.get("app_id")
        or message.get("subtype") == "bot_message"
        or (bot_user_id and message.get("user") == bot_user_id)
    )


def _spool_channel_page(client, payload: dict, key: str, cursor: str, schedule_id: str, bot_user_id: str | None) -> str:
    scan = kv().hget(_SCAN, key)
    history = client.get_slack_channel_history_result(
        payload["channel"],
        oldest=cursor or None,
        latest=_decode(scan) if scan else None,
        inclusive=False,
        max_messages=MAX_NEW_MESSAGES,
        max_pages=1,
        limit=MAX_NEW_MESSAGES,
    )
    if not field(history, "ok", False):
        return "retry"
    if not _is_owned(key, schedule_id):
        return "stale"

    fresh = sorted(
        (as_dict(m) for m in field(history, "messages", []) or [] if as_dict(m).get("ts") and as_dict(m)["ts"] > cursor),
        key=lambda m: m["ts"],
    )
    # Spool bots too (a human reply is meaningless without them), but label them
    # so the distiller uses them as context, never as a fact source.
    for message in fresh:
        author = BOT_AUTHOR_LABEL if _is_bot_authored_message(message, bot_user_id) else (message.get("user") or "?")
        kv().hsetnx(_pending_key(key), message["ts"], json.dumps({"user_id": author, "text": message.get("text") or ""}))

    if field(history, "complete", False):
        kv().hdel(_SCAN, key)
        return "complete"
    if not fresh:
        return "retry"
    kv().hset(_SCAN, key, fresh[0]["ts"])
    return "continue"


def _pending_rows(key: str) -> list[dict]:
    rows = []
    for ts, raw in (kv().hgetall(_pending_key(key)) or {}).items():
        row = json.loads(raw)
        rows.append({"ts": _decode(ts), "user": row["user_id"], "text": row["text"]})
    rows.sort(key=lambda r: r["ts"])
    return rows


def _trusted_person_ids_for_batch(key: str, from_ts: str, through_ts: str, bot_user_id: str | None) -> list[str]:
    """Person tags may only reference people actually in the batch (authors + their
    @-mentions), so the model can't invent an unrelated or nonexistent person."""
    ids: list[str] = []
    for row in _pending_rows(key):
        if not (from_ts <= row["ts"] <= through_ts):
            continue
        # Bot lines are context only; they never make a person "trusted".
        if row["user"] == BOT_AUTHOR_LABEL:
            continue
        if row["user"] and row["user"] != "?" and row["user"] != bot_user_id and row["user"] not in ids:
            ids.append(row["user"])
        for mentioned in mentioned_user_ids(row["text"], bot_user_id):
            if mentioned not in ids:
                ids.append(mentioned)
    return ids


def _profiles(team_id: str, client, user_ids: list[str]) -> dict:
    try:
        from src.domain.slack.profile_cache import get_cached_slack_user_profiles

        return get_cached_slack_user_profiles(team_id=team_id, client=client, user_ids=user_ids)
    except ImportError:
        out = {}
        for user_id in user_ids:
            lookup = client.lookup_slack_user_info(user_id)
            if field(lookup, "ok", False):
                out[user_id] = field(lookup, "user")
        return out


def humanize_batch_names(team_id: str, client, bot_user_id: str | None, messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Memories must read with human names, not raw Slack ids. Resolve author and
    @-mention ids to display names for the evidence only; person tags stay id-based."""
    ids: list[str] = []
    for message in messages:
        if message["user"] and message["user"] != BOT_AUTHOR_LABEL and message["user"] not in ids:
            ids.append(message["user"])
        for mentioned in mentioned_user_ids(message["text"], bot_user_id):
            if mentioned not in ids:
                ids.append(mentioned)
    if not ids:
        return messages, []
    try:
        profiles = _profiles(team_id, client, ids)
    except Exception:
        return messages, []

    def base_name(user_id: str) -> str:
        info = profiles.get(user_id)
        return field(info, "real_name") or field(info, "display_name") or field(info, "handle") or field(info, "name") or user_id

    # Disambiguate people who share a display name so a memory is never tagged to
    # the wrong person: qualify a collided name with the handle, else the id.
    counts: dict[str, int] = {}
    for user_id in ids:
        counts[base_name(user_id)] = counts.get(base_name(user_id), 0) + 1

    def label_for(user_id: str) -> str:
        base = base_name(user_id)
        if counts.get(base, 0) <= 1:
            return base
        handle = field(profiles.get(user_id), "handle") or field(profiles.get(user_id), "name")
        return f"{base} (@{handle})" if handle else f"{base} ({user_id})"

    roster = [{"name": label_for(i), "tag": person_brain_tag_key(i)} for i in ids if i != bot_user_id]
    named = [
        {
            "ts": m["ts"],
            "user": BOT_AUTHOR_LABEL if m["user"] == BOT_AUTHOR_LABEL else label_for(m["user"]),
            "text": re.sub(r"<@([A-Z0-9]+)(?:\|[^>]+)?>", lambda match: f"@{label_for(match.group(1))}", m["text"]),
        }
        for m in messages
    ]
    return named, roster


def _write_clusters(team_id: str, author_id: str, clusters: list[MemoryDocInput], reset_epoch: int, allowed_person_ids: list[str]) -> tuple[int, int]:
    """``writeMemories`` into shared memory. Returns (written, total)."""
    try:
        from src.domain.memory import write_memories

        out = write_memories(
            clusters,
            scope=None,
            author_id=author_id,
            expected_reset_epoch=reset_epoch,
            allowed_person_slack_user_ids=allowed_person_ids,
        )
        return field(out, "written", 0), field(out, "total", len(clusters))
    except ImportError:
        from src.domain.memory.containers import SHARED_TEAM_BRAIN_CONTAINER_TAG
        from src.domain.memory.documents import upsert_brain_document

        written = 0
        for cluster in clusters:
            result = upsert_brain_document(
                content=cluster.content,
                container_tag=SHARED_TEAM_BRAIN_CONTAINER_TAG,
                custom_id=cluster.internal_custom_id_override,
                title=cluster.title,
                author_id=author_id or None,
            )
            written += 1 if result.status == "done" else 0
        return written, len(clusters)


def _process_pending_batch(client, payload: dict, key: str, schedule_id: str, bot_user_id: str | None, installer_id: str) -> str:
    if not _is_owned(key, schedule_id):
        return "stale"
    cursor = get_observe_cursor(payload)
    persisted = _load_persisted_batch(key)
    if persisted and persisted["through_ts"] <= cursor:
        kv().hdel(_batch_key(key), persisted["through_ts"])
        persisted = _load_persisted_batch(key)
    for ts in list(kv().hkeys(_pending_key(key))):
        if _decode(ts) <= cursor:
            kv().hdel(_pending_key(key), ts)

    if not persisted:
        pending = [r for r in _pending_rows(key) if r["ts"] > cursor][:MAX_NEW_MESSAGES]
        if not pending:
            return "complete"
        named, roster = humanize_batch_names(payload["team_id"], client, bot_user_id, pending)
        prompt_batch = build_slack_prompt_batch(named, MAX_CONVO_CHARS)
        batch_messages = field(prompt_batch, "messages", [])
        from_ts = batch_messages[0]["ts"] if batch_messages else None
        through_ts = max([cursor] + [m.get("ts") or "" for m in batch_messages])
        if not from_ts or through_ts <= cursor:
            return "retry"
        if not _is_owned(key, schedule_id):
            return "stale"

        roster_text = (
            "People in these messages — when a memory is about one of them, tag it with the exact key shown and write their name (never the id) in the content:\n"
            + "\n".join(f"- {r['name']} → {r['tag']}" for r in roster)
            + "\n\n"
            if roster
            else ""
        )
        result = generate_object(
            ModelRequest(
                profile="fast",
                system=distill_system(),
                prompt=f"{roster_text}New messages in this channel:\n{field(prompt_batch, 'prompt', '')}\n\nDurable memories (or an empty list):",
                schema=_DISTILL_SCHEMA,
            )
        )
        if not _is_owned(key, schedule_id):
            return "stale"
        doc_date = datetime.fromtimestamp(int(through_ts.split(".")[0]), tz=UTC).strftime("%Y-%m-%d")
        clusters = []
        for index, memory in enumerate(result.get("memories") or []):
            doc = MemoryDocInput.from_doc(memory)
            doc.content = f"DOCUMENT_DATE: {doc_date}\n{doc.content}"
            doc.internal_custom_id_override = stable_cluster_custom_id(
                payload["team_id"], key, from_ts, through_ts, index, payload.get("reset_epoch") or 0
            )
            clusters.append(asdict(doc))
        kv().hsetnx(
            _batch_key(key),
            through_ts,
            json.dumps(
                {
                    "from_ts": from_ts,
                    "through_ts": through_ts,
                    "message_count": len(batch_messages),
                    "clusters": clusters,
                    "created_at": int(time.time() * 1000),
                }
            ),
        )
        if not _is_owned(key, schedule_id):
            return "stale"
        persisted = _load_persisted_batch(key)
        if not persisted:
            return "retry"

    if persisted["clusters"]:
        clusters = [MemoryDocInput(**c) for c in persisted["clusters"]]
        written, total = _write_clusters(
            payload["team_id"],
            installer_id,
            clusters,
            payload.get("reset_epoch") or 0,
            _trusted_person_ids_for_batch(key, persisted["from_ts"], persisted["through_ts"], bot_user_id),
        )
        if written != total:
            return "retry"
        log.info(
            "[company-brain] channel-observe team=%s channel=%s messages=%d clusters=%d written=%d/%d",
            payload["team_id"], payload["channel"], persisted["message_count"], len(clusters), written, total,
        )
    if not _is_owned(key, schedule_id):
        return "stale"
    update_observe_cursor(key, persisted["through_ts"])
    for ts in list(kv().hkeys(_pending_key(key))):
        if _decode(ts) <= persisted["through_ts"]:
            kv().hdel(_pending_key(key), ts)
    kv().hdel(_batch_key(key), persisted["through_ts"])
    return "continue"


def run_channel_observe(payload: dict) -> None:
    """Scheduler job. ``payload``: team_id, channel, schedule_id, reset_epoch, retry_attempt."""
    from src.core.config import settings
    from src.domain.slack.workspace import get_workspace_by_team_id

    key = channel_key(payload)
    schedule_id = payload["schedule_id"]
    if not is_brain_memory_reset_epoch_current(payload.get("reset_epoch") or 0):
        _finish_owned_channel_observe(key, schedule_id)
        return
    if not _is_owned(key, schedule_id):
        return
    retry = load_channel_retry_state(key)
    ownership = _row(key)
    if retry["exhausted"] and (not ownership or not _claim_channel_probe(key, ownership["armed_at"])):
        _finish_owned_channel_observe(key, schedule_id)
        return

    try:
        ws = get_workspace_by_team_id(payload["team_id"])
        if not _is_owned(key, schedule_id):
            return
        installer_id = getattr(ws, "installed_by_slack_user_id", None) or settings.slack_owner_id
        # Never write with a workspace this server doesn't own.
        if ws is None or not installer_id:
            _clear_terminal_channel_observe(key, schedule_id)
            return
        client = ws.client
        info = client.get_slack_conversation_info(payload["channel"])
        if not _is_owned(key, schedule_id):
            return
        if info is None:
            raise RuntimeError("channel_info_unavailable")
        if field(info, "is_private") is not False:
            _clear_terminal_channel_observe(key, schedule_id)
            return

        cursor = get_observe_cursor(payload)
        scan_active = kv().hexists(_SCAN, key)
        pending_count = sum(1 for ts in kv().hkeys(_pending_key(key)) if _decode(ts) > cursor)
        if scan_active or not pending_count:
            scan_result = _spool_channel_page(client, payload, key, cursor, schedule_id, ws.bot_user_id)
            if scan_result == "stale":
                return
            if scan_result != "complete":
                if scan_result == "retry":
                    _retry_owned_channel_observe(payload, key, schedule_id)
                else:
                    clear_channel_retry_state(key)
                    _rearm_owned_channel_observe(payload, schedule_id, CONTINUE_DELAY_SECONDS, 0)
                return
            clear_channel_retry_state(key)

        result = _process_pending_batch(client, payload, key, schedule_id, ws.bot_user_id, installer_id)
        if result == "stale":
            return
        if result == "complete":
            clear_channel_retry_state(key)
            _finish_owned_channel_observe(key, schedule_id)
            return
        if result == "retry":
            _retry_owned_channel_observe(payload, key, schedule_id)
            return
        clear_channel_retry_state(key)
        _rearm_owned_channel_observe(payload, schedule_id, CONTINUE_DELAY_SECONDS, 0)
    except Exception as error:
        log.warning("[company-brain] channel-observe step failed channel=%s: %s", payload["channel"], type(error).__name__)
        try:
            _retry_owned_channel_observe(payload, key, schedule_id, error)
        except Exception:
            log.exception("[company-brain] channel-observe retry arm failed")


scheduler.register(JOB_RUN_CHANNEL_OBSERVE, run_channel_observe)
