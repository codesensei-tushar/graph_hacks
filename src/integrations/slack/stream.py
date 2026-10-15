"""Port of company-brain `brain/slack/stream.ts`.

A turn's live presence in Slack. DMs and the assistant pane use Slack's native
streams (a plan card with task updates, then the answer appended). Channel threads
get a temporary public progress card instead; the final answer is posted as a fresh
reply and the card is deleted (or collapsed and linked when delete fails).

The original serializes writes through promise chains; here a lock per stream does
the same, since a turn may report progress from more than one thread.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from src.integrations.slack.client import SlackClient, StreamChunk
from src.integrations.slack.format import markdown_reply_blocks, mrkdwn_reply_blocks, split_slack_markdown

log = logging.getLogger(__name__)

CONTEXT_FOOTER_BLOCK_ID = "brain_context_footer"
MAX_VISIBLE_TASKS = 8
OVERFLOW_TASK_TITLE = "Continuing research"
# Runaway backstop only; narration is a deliberate post_update tool call.
MAX_NARRATIONS = 10

TaskStatus = Literal["in_progress", "complete", "error"]


@dataclass(frozen=True)
class TurnCardSource:
    url: str
    text: str


@dataclass
class TurnCardExtra:
    detail: str | None = None
    output: str | None = None
    sources: list[TurnCardSource] | None = None


@dataclass
class _TaskSlot:
    slack_id: str
    title: str
    status: TaskStatus
    detail: str | None = None
    output: str | None = None
    sources: list[TurnCardSource] | None = None
    active_tool_call_ids: set[str] = field(default_factory=set)
    had_error: bool = False


@dataclass
class _ReplyDelivery:
    ok: bool
    message_ts: str | None = None


@dataclass
class FinalizeResult:
    streamed: bool
    message_ts: str | None = None


def rich_text_entity(text: str) -> dict:
    """Slack ``task_card`` details/output field: a single rich_text entity."""
    return {
        "type": "rich_text",
        "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": text[:300]}]}],
    }


def _format_elapsed(seconds_elapsed: float) -> str:
    s = round(seconds_elapsed)
    if s < 60:
        return f"{s}s"
    m, rem = divmod(s, 60)
    return f"{m}m {rem}s" if rem else f"{m}m"


def task_card_sources(sources: list[TurnCardSource] | None) -> list[dict] | None:
    if not sources:
        return None
    return [{"type": "url", "url": s.url, "text": s.text[:150]} for s in sources[:3]]


def _public_progress_blocks(slots: list[_TaskSlot], title: str, block_id: str, final: bool = False) -> list[dict]:
    tasks = []
    for slot in slots:
        task: dict = {
            "type": "task_card",
            "task_id": slot.slack_id,
            "title": slot.title[:150],
            "status": slot.status if final or slot.status == "error" else "in_progress",
        }
        if slot.detail:
            task["details"] = rich_text_entity(slot.detail)
        if slot.output:
            task["output"] = rich_text_entity(slot.output)
        sources = task_card_sources(slot.sources)
        if sources:
            task["sources"] = sources
        tasks.append(task)
    return [{"type": "plan", "block_id": block_id, "title": title[:150], "tasks": tasks}]


class TurnProgress:
    """What a running turn reports: tool cards and deliberate narration lines."""

    def __init__(self, session: "SlackStreamSession"):
        self._session = session

    def card(self, tool_call_id: str, title: str, status: TaskStatus, extra: TurnCardExtra | None = None) -> None:
        self._session._card(tool_call_id, title, status, extra)

    def narrate(self, text: str) -> bool:
        """True only when the whole update reached the thread."""
        return self._session._narrate(text)


class SlackStreamSession:
    def __init__(
        self,
        client: SlackClient,
        *,
        channel: str,
        thread_ts: str,
        team_id: str,
        org_id: str = "",
        recipient_user_id: str | None = None,
        public_progress: bool = False,
        existing_public_progress_ts: str | None = None,
        clear_assistant_status_on_progress: bool = False,
        context_footer: Callable[[], str] | None = None,
        prepare_reply: Callable[[str], str] | None = None,
        on_delivery_checkpoint: Callable[[dict], None] | None = None,
    ):
        self.client = client
        self.channel = channel
        self.thread_ts = thread_ts
        self.team_id = team_id
        self.org_id = org_id
        self.recipient_user_id = recipient_user_id
        self.public_progress = public_progress
        self.clear_assistant_status_on_progress = clear_assistant_status_on_progress
        self.context_footer = context_footer
        self.prepare_reply = prepare_reply
        self.on_delivery_checkpoint = on_delivery_checkpoint

        self._task_slots: dict[str, _TaskSlot] = {}
        self._tool_call_slots: dict[str, _TaskSlot] = {}
        self._next_task_index = 1
        self._stream_header_shown = False
        self._stream_ts: str | None = None
        self._public_progress_ts = existing_public_progress_ts
        # Stable for this card's lifetime so each chat.update mutates the same plan
        # block in place rather than re-rendering a brand-new block every tick.
        self._plan_block_id = f"plan_{thread_ts}_{int(time.time() * 1000)}"
        self._started_at = time.time()
        self._public_lock = threading.RLock()
        self._narration_lock = threading.RLock()
        self._stream_dead = False
        self._assistant_status_cleared = False
        # Set once finalized or discarded: a turn must not close its stream twice,
        # and a late steering discard must not clobber a delivered answer.
        self._closed = False
        self._narration_count = 0
        self._last_narration = ""
        self._last_narration_ts: str | None = None
        self.progress = TurnProgress(self)

    # --- helpers --------------------------------------------------------------------

    def _footer_blocks(self) -> list[dict]:
        if not self.context_footer:
            return []
        return [
            {
                "type": "context",
                "block_id": CONTEXT_FOOTER_BLOCK_ID,
                "elements": [{"type": "mrkdwn", "text": self.context_footer()}],
            }
        ]

    def _checkpoint(self, **update) -> None:
        if not self.on_delivery_checkpoint:
            return
        try:
            self.on_delivery_checkpoint(update)
        except Exception as error:
            log.warning("[company-brain] delivery checkpoint failed: %s", error)

    def _prepare(self, reply: str) -> str:
        if not self.prepare_reply or not reply.strip():
            return reply
        try:
            return self.prepare_reply(reply)
        except Exception as error:
            log.warning("[company-brain] Slack reply preparation failed: %s", error)
            return reply

    def _task_slot_for(self, title: str) -> _TaskSlot:
        visible = title if title in self._task_slots or len(self._task_slots) < MAX_VISIBLE_TASKS else OVERFLOW_TASK_TITLE
        existing = self._task_slots.get(visible)
        if existing:
            return existing
        slot = _TaskSlot(slack_id=f"task-{self._next_task_index}", title=visible, status="in_progress")
        self._next_task_index += 1
        self._task_slots[visible] = slot
        return slot

    def _reset_tasks(self) -> None:
        self._task_slots.clear()
        self._tool_call_slots.clear()
        self._next_task_index = 1
        self._stream_header_shown = False

    def _ensure_stream(self) -> str | None:
        if self.public_progress or self._stream_dead:
            return None
        if self._stream_ts:
            return self._stream_ts
        self._stream_ts = self.client.start_slack_stream(
            self.channel, self.thread_ts, self.recipient_user_id, self.team_id, "plan"
        )
        if not self._stream_ts:
            self._stream_dead = True
            log.warning("[company-brain] stream unavailable for org=%s; answering without a card", self.org_id)
        return self._stream_ts

    def _clear_assistant_status(self) -> None:
        if not self.clear_assistant_status_on_progress or self._assistant_status_cleared:
            return
        self._assistant_status_cleared = self.client.clear_assistant_thread_status(self.channel, self.thread_ts)

    def _render_public_progress(self) -> None:
        slots = list(self._task_slots.values())
        text = "\n".join(slot.title for slot in slots) or "Working on it"
        # Latest phase, not last in-progress: between tools no slot is in_progress.
        header = slots[-1].title if slots else "Working on it"
        blocks = [*_public_progress_blocks(slots, header, self._plan_block_id), *self._footer_blocks()]
        if self._public_progress_ts:
            if not self.client.update_slack_message(self.channel, self._public_progress_ts, text, blocks):
                self._stream_dead = True
            return
        self._public_progress_ts = self.client.post_slack_message(self.channel, text, self.thread_ts, blocks)
        if self._public_progress_ts:
            self._checkpoint(progress_message_ts=self._public_progress_ts)
        else:
            self._stream_dead = True

    def _sync_public_progress(self) -> None:
        with self._public_lock:
            try:
                self._render_public_progress()
            except Exception:
                log.exception("[company-brain] public progress update failed")

    def _update_public_card_with_reply(self, ts: str, text: str, plan_blocks: list, reply: str | None = None) -> _ReplyDelivery:
        footer = self._footer_blocks()
        if not (reply or "").strip():
            ok = self.client.update_slack_message(self.channel, ts, text, [*plan_blocks, *footer])
            return _ReplyDelivery(ok, ts if ok else None)
        reply_chunks = split_slack_markdown(reply)
        if len(reply_chunks) > 1:
            # Slack caps all markdown blocks in one payload at 12k characters. Close
            # the progress card, then send sentence-aligned continuation messages.
            if not self.client.update_slack_message(self.channel, ts, text, [*plan_blocks, *footer]):
                return _ReplyDelivery(False)
            message_ts = self.client.post_slack_reply(self.channel, reply, self.thread_ts, footer)
            return _ReplyDelivery(bool(message_ts), message_ts)
        first = reply_chunks[0] if reply_chunks else reply
        if self.client.update_slack_message(self.channel, ts, text, [*plan_blocks, *markdown_reply_blocks(first), *footer]):
            return _ReplyDelivery(True, ts)
        if self.client.update_slack_message(self.channel, ts, text, [*plan_blocks, *mrkdwn_reply_blocks(reply), *footer]):
            return _ReplyDelivery(True, ts)
        plain_ok = self.client.update_slack_message(self.channel, ts, reply)
        return _ReplyDelivery(plain_ok, ts if plain_ok else None)

    def _finalize_public_card(
        self,
        ts: str,
        slots: list[_TaskSlot],
        title: str,
        terminal_status: TaskStatus,
        reply: str | None = None,
        rename_in_progress: str | None = None,
    ) -> _ReplyDelivery:
        for slot in slots:
            if slot.status == "in_progress":
                if rename_in_progress:
                    slot.title = rename_in_progress
                slot.status = terminal_status
        blocks = _public_progress_blocks(slots, title, self._plan_block_id, final=True)
        return self._update_public_card_with_reply(ts, (reply or "").strip() or title, blocks, reply)

    # --- progress -------------------------------------------------------------------

    def _narrate(self, text: str) -> bool:
        if self._closed:
            return False
        line = text.strip()
        if not line or line == self._last_narration or self._narration_count >= MAX_NARRATIONS:
            return False
        self._narration_count += 1
        self._last_narration = line
        with self._narration_lock:
            if self._closed:
                return False
            outgoing = self._prepare(line)
            if self._closed:
                return False
            try:
                delivery = self.client.post_slack_reply_delivery(self.channel, outgoing, self.thread_ts)
            except Exception:
                return False
            # Only a fully posted update counts as surfaced: on a later-chunk failure
            # the tail was never seen, so it stays eligible for the final reply.
            if delivery.complete:
                self._last_narration_ts = delivery.ts
            return delivery.complete

    def _card(self, tool_call_id: str, title: str, status: TaskStatus, extra: TurnCardExtra | None) -> None:
        if self._closed or self._stream_dead:
            return
        existing = self._tool_call_slots.get(tool_call_id)
        if status != "in_progress" and not existing:
            return
        slot = existing or self._task_slot_for(title)
        if extra and extra.detail:
            slot.detail = extra.detail
        if extra and extra.output:
            slot.output = extra.output
        if extra and extra.sources:
            slot.sources = extra.sources
        if status == "in_progress":
            self._tool_call_slots[tool_call_id] = slot
            slot.active_tool_call_ids.add(tool_call_id)
            if slot.status != "in_progress":
                return
            if len(slot.active_tool_call_ids) > 1 or existing:
                return
        else:
            self._tool_call_slots.pop(tool_call_id, None)
            slot.active_tool_call_ids.discard(tool_call_id)
            if status == "error":
                slot.had_error = True
            if slot.active_tool_call_ids:
                return
            next_status: TaskStatus = "error" if slot.had_error else "complete"
            if slot.status == next_status:
                return
            slot.status = next_status

        if self.public_progress:
            self._sync_public_progress()
            if self._public_progress_ts and not self._stream_dead:
                self._clear_assistant_status()
            return

        ts = self._ensure_stream()
        if not ts or self._stream_dead:
            return
        chunks: list[StreamChunk] = []
        if not self._stream_header_shown:
            chunks.append({"type": "plan_update", "title": "Working on it"})
            self._stream_header_shown = True
        update: dict = {"type": "task_update", "id": slot.slack_id, "title": slot.title, "status": slot.status}
        if slot.detail:
            update["details"] = slot.detail
        if slot.output:
            update["output"] = slot.output
        if slot.sources:
            update["sources"] = [{"type": "url", "url": s.url, "text": s.text} for s in slot.sources[:3]]
        chunks.append(update)
        ok = self.client.append_slack_stream_chunks(self.channel, ts, chunks)
        if not ok:
            self._stream_dead = True
        else:
            self._clear_assistant_status()

    # --- session --------------------------------------------------------------------

    def rewrite_last_narration(self, text: str) -> bool:
        with self._narration_lock:
            ts = self._last_narration_ts
        if not ts or not text.strip():
            return False
        try:
            return self.client.update_slack_message(self.channel, ts, text)
        except Exception:
            return False

    def finalize(self, reply: str, failed: bool, paused: bool = False, settled: bool = False) -> FinalizeResult:
        if self._closed:
            return FinalizeResult(streamed=False)
        self._closed = True
        outgoing = self._prepare(reply)
        streamed_reply = False

        elapsed = _format_elapsed(time.time() - self._started_at)
        if failed:
            closing_title = f"Ran into a problem ({elapsed})"
        elif paused:
            closing_title = f"Taking longer than expected — reply to pick this back up ({elapsed})"
        elif outgoing.strip():
            closing_title = f"Answer ready ({elapsed})"
        elif settled:
            closing_title = f"Done ({elapsed})"
        else:
            closing_title = f"Paused ({elapsed})"

        # Public progress cards are temporary. A fresh reply owns the final message
        # ts so Slack keeps the answer in chronological order.
        answer_ts: str | None = None
        try:
            with self._public_lock, self._narration_lock:
                self._clear_assistant_status()
                if self._public_progress_ts:
                    progress_ts = self._public_progress_ts
                    terminal_status: TaskStatus = "error" if failed else "complete"
                    slots = list(self._task_slots.values())
                    rename = "Interrupted" if paused else None

                    def collapse() -> _ReplyDelivery:
                        return self._finalize_public_card(progress_ts, slots, closing_title, terminal_status, None, rename)

                    if outgoing.strip():
                        fresh_ts = self.client.post_slack_reply(self.channel, outgoing, self.thread_ts, self._footer_blocks())
                        if fresh_ts:
                            streamed_reply = True
                            answer_ts = fresh_ts
                            self._checkpoint(reply_message_ts=fresh_ts)
                            deleted = self.client.delete_slack_message(self.channel, progress_ts)
                            if deleted:
                                self._checkpoint(progress_message_ts=None)
                            else:
                                permalink = self.client.get_slack_message_permalink(self.channel, fresh_ts)
                                linked = self._finalize_public_card(
                                    progress_ts,
                                    slots,
                                    closing_title,
                                    terminal_status,
                                    f"[View final answer]({permalink})"
                                    if permalink
                                    else "Final answer posted as the newest reply below.",
                                    rename,
                                )
                                if not linked.ok:
                                    collapse()
                        else:
                            delivery = self._finalize_public_card(
                                progress_ts, slots, closing_title, terminal_status, outgoing, rename
                            )
                            streamed_reply = delivery.ok
                            if delivery.ok:
                                answer_ts = delivery.message_ts or progress_ts
                                self._checkpoint(reply_message_ts=answer_ts)
                            else:
                                self._stream_dead = True
                                collapse()
                    else:
                        delivery = collapse()
                        if delivery.ok:
                            answer_ts = delivery.message_ts or progress_ts
                    self._reset_tasks()

                if outgoing.strip():
                    self._ensure_stream()
                if self._stream_ts:
                    stream_ts = self._stream_ts
                    if not self._stream_dead:
                        final_status: TaskStatus = "error" if failed else "complete"
                        closing: list[StreamChunk] = []
                        for slot in self._task_slots.values():
                            if slot.status != "in_progress":
                                continue
                            slot.status = final_status
                            closing.append(
                                {
                                    "type": "task_update",
                                    "id": slot.slack_id,
                                    "title": "Interrupted" if paused else slot.title,
                                    "status": final_status,
                                }
                            )
                        closing.append({"type": "plan_update", "title": closing_title})
                        self.client.append_slack_stream_chunks(self.channel, stream_ts, closing)
                        if outgoing.strip():
                            ok = self.client.append_slack_stream_reply(self.channel, stream_ts, outgoing)
                            streamed_reply = ok
                            if not ok:
                                self._stream_dead = True
                    answer_ts = stream_ts
                    if streamed_reply:
                        self._checkpoint(reply_message_ts=stream_ts)
                    self._reset_tasks()
                    self.client.stop_slack_stream(self.channel, stream_ts)
        except Exception:
            log.exception("[company-brain] stream finalize failed")
        return FinalizeResult(streamed=streamed_reply, message_ts=answer_ts)

    def discard(self, reply: str | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        outgoing = self._prepare(reply) if reply else None
        with self._public_lock, self._narration_lock:
            self._clear_assistant_status()
            ts = self._stream_ts
            public_ts = self._public_progress_ts
            slots = list(self._task_slots.values())
            # Captured before finalize flips in-progress slots to error.
            interrupted_chunks: list[StreamChunk] = [
                {
                    "type": "task_update",
                    "id": slot.slack_id,
                    "title": "Interrupted" if slot.status == "in_progress" else slot.title,
                    "status": "complete",
                }
                for slot in slots
            ]
            interrupted_chunks.append({"type": "plan_update", "title": "Interrupted"})
            self._stream_dead = True
            self._stream_ts = None
            self._public_progress_ts = None
            self._checkpoint(progress_message_ts=None)
            if public_ts:
                delivery = self._finalize_public_card(public_ts, slots, "Interrupted", "error", outgoing)
                self._reset_tasks()
                if delivery.ok:
                    return
            else:
                self._reset_tasks()
            if ts:
                has_reply = bool(outgoing and outgoing.strip())
                if self.client.stop_slack_stream(
                    self.channel, ts, chunks=interrupted_chunks, markdown_text=outgoing if has_reply else None
                ):
                    return
                if self.client.update_slack_message(self.channel, ts, outgoing if has_reply else "Interrupted", []):
                    return
            if outgoing and outgoing.strip():
                self.client.post_slack_reply(self.channel, outgoing, self.thread_ts)

    def post_fallback(self, reply: str) -> str | None:
        with self._narration_lock:
            outgoing = self._prepare(reply)
            ts = self.client.post_slack_reply(self.channel, outgoing, self.thread_ts)
        if ts:
            self._checkpoint(reply_message_ts=ts)
        else:
            log.warning(
                "[company-brain] reply not delivered org=%s channel=%s thread=%s", self.org_id, self.channel, self.thread_ts
            )
        return ts


def create_slack_stream_session(client: SlackClient, **kwargs) -> SlackStreamSession:
    return SlackStreamSession(client, **kwargs)
