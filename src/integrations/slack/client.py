"""Port of company-brain `brain/slack/client.ts`.

Every Slack Web API call the brain makes, on slack_sdk. Each TS export
``fooBar(botToken, ...)`` is ``SlackClient.foo_bar(...)`` here. Calls never raise
for a Slack-side failure: like the original, they log and return a falsy value or
a ``{ok: false}``-style result so a turn can pick a fallback.

Left out on purpose: lease cards (leasing is not ported) and MCP catalog icons on
approval cards. The original's ``container`` approval card is rendered with
standard header/section/context/actions blocks.

The short names from the first cut (``post_message``, ``user_info``,
``conversation_info``, ...) stay as the stable surface the turn code and tests use;
they delegate to the ported functions.
"""

import json
import logging
import time
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from src.core.config import settings
from src.integrations.slack.format import (
    humanize_tool_action,
    markdown_reply_blocks,
    mrkdwn_reply_blocks,
    plain_text_fallback,
    split_slack_markdown,
    to_slack_mrkdwn,
)

log = logging.getLogger(__name__)

SLACK_FILE_UPLOAD_TIMEOUT_SECONDS = 30
SLACK_CONVERSATION_INFO_CACHE_TTL_SECONDS = 10 * 60

APPROVAL_APPROVE_ACTION_ID = "brain_approval_approve"
APPROVAL_DENY_ACTION_ID = "brain_approval_deny"

APPROVAL_STATUS_META = {
    "Approved": ("✅", "Approved"),
    "Denied": ("🚫", "Denied"),
    "Expired": ("⌛", "Expired"),
    "Cancelled": ("🛑", "Cancelled"),
}

# Dead beyond recovery, so skip the auth.revoke retry. `invalid_auth` can mean IP restrictions.
SLACK_DEAD_TOKEN_ERRORS = frozenset({"token_revoked", "token_expired", "account_inactive"})
SLACK_RETRYABLE_ERRORS = frozenset({"ratelimited", "internal_error", "service_unavailable", "fatal_error", "request_timeout"})

# A raw Slack message dict (conversations.history / replies item).
SlackThreadMessage = dict[str, Any]
# chat.appendStream chunk: markdown_text | blocks | task_update | plan_update.
StreamChunk = dict[str, Any]
SlackUninstallOutcome = Literal["revoked", "terminal", "transient"]


# --- result types ----------------------------------------------------------------------


@dataclass
class SlackConversationInfo:
    id: str | None = None
    name: str | None = None
    topic: str | None = None
    purpose: str | None = None
    is_private: bool | None = None
    is_member: bool | None = None
    is_archived: bool | None = None
    is_im: bool | None = None


@dataclass
class SlackConversationInfoLookup:
    ok: bool
    info: SlackConversationInfo | None = None
    # missing_channel_id | conversation_unavailable | slack_api_error
    reason: str | None = None
    error: str | None = None


@dataclass
class SlackUserInfo:
    """A Slack profile.

    ``name`` is the Slack login handle as users.info returns it; ``real_name`` is the
    person's display name (company-brain's ``SlackUserInfo.name``).
    """

    id: str
    name: str | None = None
    real_name: str | None = None
    display_name: str | None = None
    email: str | None = None
    is_bot: bool = False
    # Single- or multi-channel guest: only sees channels they're added to.
    is_restricted: bool = False
    # Single-channel guest, retained separately for explicit eligibility checks.
    is_ultra_restricted: bool = False
    handle: str | None = None
    timezone: str | None = None
    tz_offset: int | None = None
    # Home workspace id; differs from ours for Slack Connect users.
    team_id: str | None = None
    # Slack Connect / external user relative to the queried workspace.
    is_stranger: bool = False


@dataclass
class SlackUserInfoLookup:
    ok: bool
    user: SlackUserInfo | None = None
    # missing_user_id | user_not_found | slack_api_error | profile_unavailable
    reason: str | None = None
    error: str | None = None
    retry_after_seconds: int | None = None


@dataclass
class SlackBotIdentity:
    """This workspace's Company Brain Slack app identity."""

    slack_user_id: str
    product_name: str = "Company Brain"
    name: str | None = None
    display_name: str | None = None
    handle: str | None = None
    email: str | None = None


@dataclass
class SlackMember:
    id: str
    name: str
    handle: str | None = None
    display_name: str | None = None
    email: str | None = None
    is_bot: bool = False
    is_restricted: bool = False
    is_ultra_restricted: bool = False
    team_id: str | None = None
    is_stranger: bool = False


@dataclass
class SlackUserGroup:
    id: str
    handle: str
    name: str
    user_ids: list[str]


@dataclass
class SlackConversation:
    id: str
    name: str
    is_private: bool


@dataclass
class SlackPublicChannel:
    id: str
    name: str
    is_member: bool
    is_archived: bool
    is_general: bool
    # Slack Connect / externally shared channel.
    is_external: bool
    topic: str | None = None
    purpose: str | None = None
    member_count: int | None = None


@dataclass
class SlackCursorPage:
    ok: bool
    items: list = field(default_factory=list)
    next_cursor: str | None = None
    complete: bool = False
    error: str | None = None
    retry_after_seconds: int | None = None


@dataclass
class SlackThreadHistory:
    ok: bool
    messages: list[SlackThreadMessage]
    complete: bool
    next_cursor: str | None = None
    error: str | None = None


@dataclass
class SlackChannelHistoryResult:
    ok: bool
    messages: list[SlackThreadMessage]
    complete: bool
    error: str | None = None


@dataclass
class SlackPostResult:
    ok: bool
    ts: str | None = None
    deduped: bool = False
    error: str | None = None
    retry_after_seconds: int | None = None


@dataclass
class SlackInviteResult:
    ok: bool
    error: str | None = None
    retry_after_seconds: int | None = None


@dataclass
class SlackReactionAddResult:
    ok: bool
    # added | already_present
    outcome: str | None = None
    error: str | None = None
    retry_after_seconds: int | None = None


@dataclass
class SlackJoinResult:
    ok: bool
    already_member: bool = False
    error: str | None = None
    retry_after_seconds: int | None = None


@dataclass
class SlackFileUploadResult:
    ok: bool
    file_id: str | None = None
    # request_upload_url | upload_bytes | complete_upload
    stage: str | None = None
    error: str | None = None


@dataclass
class SlackReplyDelivery:
    ts: str | None
    complete: bool


@dataclass
class SlackMessageReaction:
    name: str
    count: int


@dataclass
class SlackOAuthResult:
    team_id: str
    bot_token: str
    team_name: str | None = None
    bot_user_id: str | None = None
    app_id: str | None = None
    scopes: str | None = None
    authed_user_id: str | None = None


@dataclass
class SlackApprovalCard:
    approval_id: str
    summary: str
    tool_name: str
    asker_user: str
    expires_at: float
    slug: str | None = None


# --- raw API --------------------------------------------------------------------------


@dataclass
class _Response:
    data: dict
    status: int
    headers: dict


def _retry_after_seconds(response: _Response) -> int | None:
    if response.status != 429:
        return None
    try:
        parsed = int(response.headers.get("retry-after") or response.headers.get("Retry-After") or "")
    except ValueError:
        return 1
    return parsed if parsed > 0 else 1


def _cursor(data: dict) -> str | None:
    return ((data.get("response_metadata") or {}).get("next_cursor") or "").strip() or None


def _str(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def build_slack_bot_identity(slack_user_id: str, profile: SlackUserInfo | None = None) -> SlackBotIdentity:
    profile = profile or SlackUserInfo(id=slack_user_id)
    return SlackBotIdentity(
        slack_user_id=slack_user_id,
        name=profile.real_name,
        display_name=profile.display_name,
        handle=profile.handle,
        email=profile.email,
    )


def map_slack_member(m: dict) -> SlackMember | None:
    if m.get("deleted") or not m.get("id"):
        return None
    profile = m.get("profile") or {}
    display_name = (profile.get("display_name") or "").strip()
    real_name = m.get("real_name") or profile.get("real_name") or ""
    name = real_name or display_name or m.get("name") or ""
    if not name and not m.get("name"):
        return None
    return SlackMember(
        id=m["id"],
        name=name or m.get("name") or m["id"],
        handle=(m.get("name") or "").strip() or None,
        display_name=display_name or None,
        email=profile.get("email"),
        is_bot=m.get("is_bot") is True,
        is_restricted=m.get("is_restricted") is True,
        is_ultra_restricted=m.get("is_ultra_restricted") is True,
        team_id=m.get("team_id") or None,
        is_stranger=m.get("is_stranger") is True,
    )


def _slack_user_info_from_member(user: dict) -> SlackUserInfo | None:
    mapped = map_slack_member(user)
    if mapped is None:
        return None
    return SlackUserInfo(
        id=mapped.id,
        name=mapped.handle,
        real_name=mapped.name,
        display_name=mapped.display_name,
        email=mapped.email,
        is_bot=mapped.is_bot,
        is_restricted=user.get("is_restricted") is True or user.get("is_ultra_restricted") is True,
        is_ultra_restricted=user.get("is_ultra_restricted") is True,
        handle=mapped.handle,
        timezone=_str(user.get("tz")),
        tz_offset=user.get("tz_offset") if isinstance(user.get("tz_offset"), int) else None,
        team_id=user.get("team_id") or None,
        is_stranger=user.get("is_stranger") is True,
    )


def _is_transient_slack_conversation_error(status: int, error: str | None) -> bool:
    import re

    if status in (408, 429) or status >= 500:
        return True
    return re.search(r"rate.?limit|temporar|timeout|internal_error|service_unavailable", error or "", re.I) is not None


class SlackClient:
    def __init__(self, token: str, web: WebClient | None = None):
        self.token = token
        self.web = web or WebClient(token=token, base_url=settings.slack_api_base_url)

    def _call(
        self,
        method: str,
        *,
        http_verb: str = "POST",
        json_body: dict | None = None,
        params: dict | None = None,
        form: dict | None = None,
    ) -> _Response:
        """One Web API call. Slack-side errors come back as ``{"ok": False}``, never raised."""
        try:
            response = self.web.api_call(method, http_verb=http_verb, json=json_body, params=params, data=form)
            return _Response(dict(response.data or {}), response.status_code, dict(response.headers or {}))
        except SlackApiError as error:
            response = error.response
            data = response.data if isinstance(response.data, dict) else {}
            return _Response(
                {"ok": False, **data, "error": data.get("error") or f"http_{response.status_code}"},
                response.status_code,
                dict(response.headers or {}),
            )
        except Exception as error:  # network
            return _Response({"ok": False, "error": str(error)}, 0, {})

    def _get_with_retry(self, method: str, params: dict, max_retries: int = 3) -> _Response:
        """Interactive reads sleep through a short 429 (company-brain ``slackFetchWithRetry``)."""
        attempt = 0
        while True:
            response = self._call(method, http_verb="GET", params=params)
            if response.status != 429 or attempt >= max_retries:
                return response
            try:
                seconds = int(response.headers.get("retry-after") or response.headers.get("Retry-After") or "1")
            except ValueError:
                seconds = 1
            time.sleep(min(max(seconds, 1), 5))
            attempt += 1

    # --- files ----------------------------------------------------------------------

    def upload_slack_file(
        self,
        *,
        channel: str,
        filename: str,
        content: bytes,
        thread_ts: str | None = None,
        alt_text: str | None = None,
        can_share: Callable[[], bool] | None = None,
    ) -> SlackFileUploadResult:
        """Upload a generated artifact directly to Slack and share it in the active thread."""

        def failure(stage: str, error: str) -> SlackFileUploadResult:
            return SlackFileUploadResult(ok=False, stage=stage, error=error)

        length = len(content)
        if can_share is not None and can_share() is False:
            return failure("request_upload_url", "stale_turn")
        if not channel.strip():
            return failure("request_upload_url", "missing_channel")
        if not filename.strip():
            return failure("request_upload_url", "missing_filename")
        if length <= 0:
            return failure("request_upload_url", "empty_file")

        # Slack's production API ignores JSON fields for this route; URL-encoded form
        # data is parsed consistently by both external-upload endpoints.
        form = {"filename": filename, "length": str(length)}
        if alt_text:
            form["alt_txt"] = alt_text[:1000]
        ticket = self._call("files.getUploadURLExternal", form=form).data
        if ticket.get("ok") is not True or not ticket.get("upload_url") or not ticket.get("file_id"):
            return failure("request_upload_url", ticket.get("error") or "invalid_response")

        try:
            request = urllib.request.Request(
                ticket["upload_url"],
                data=content,
                headers={"content-type": "application/octet-stream"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=SLACK_FILE_UPLOAD_TIMEOUT_SECONDS) as response:
                if not 200 <= response.status < 300:
                    return failure("upload_bytes", f"http_{response.status}")
        except OSError as error:
            return failure("upload_bytes", str(error))

        # Uploading bytes does not publish the file. Recheck the turn immediately
        # before Slack's completion call, which is the sharing step.
        if can_share is not None and can_share() is False:
            return failure("complete_upload", "stale_turn")
        form = {"files": json.dumps([{"id": ticket["file_id"], "title": filename}]), "channel_id": channel}
        if thread_ts:
            form["thread_ts"] = thread_ts
        complete = self._call("files.completeUploadExternal", form=form).data
        if complete.get("ok") is not True:
            return failure("complete_upload", complete.get("error") or "invalid_response")
        return SlackFileUploadResult(ok=True, file_id=ticket["file_id"])

    def get_slack_file_info(self, file_id: str) -> dict | None:
        data = self._call("files.info", http_verb="GET", params={"file": file_id}).data
        return data.get("file") if data.get("ok") else None

    def download_private_url(self, url: str, max_bytes: int) -> bytes | None:
        request = urllib.request.Request(url, headers={"authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(request, timeout=SLACK_FILE_UPLOAD_TIMEOUT_SECONDS) as response:
                if not 200 <= response.status < 300:
                    log.warning("[slack] attachment download failed: HTTP %s", response.status)
                    return None
                body = response.read(max_bytes + 1)
        except OSError as error:
            log.warning("[slack] attachment download error: %s", error)
            return None
        return body

    # --- posting --------------------------------------------------------------------

    def _post_slack_message_raw(self, channel: str, text: str, thread_ts: str | None = None, blocks: list | None = None) -> dict:
        if not channel:
            log.warning("[slack] chat.postMessage skipped: missing channel")
            return {"ok": False, "error": "missing_channel"}
        body: dict = {"channel": channel, "text": text, "unfurl_links": False, "unfurl_media": False}
        if blocks:
            body["blocks"] = blocks
        if thread_ts:
            body["thread_ts"] = thread_ts
        data = self._call("chat.postMessage", json_body=body).data
        if not data.get("ok"):
            log.warning("[slack] chat.postMessage failed: %s channel=%s", data.get("error") or "unknown", channel)
        return data

    def post_slack_message(self, channel: str, text: str, thread_ts: str | None = None, blocks: list | None = None) -> str | None:
        data = self._post_slack_message_raw(channel, to_slack_mrkdwn(text), thread_ts, blocks)
        return data.get("ts") if data.get("ok") else None

    def invite_slack_user_to_channel(self, channel: str, slack_user_id: str) -> SlackInviteResult:
        response = self._call("conversations.invite", json_body={"channel": channel, "users": slack_user_id})
        data = response.data
        if data.get("ok") is True or data.get("error") in ("already_in_channel", "cant_invite_self"):
            return SlackInviteResult(ok=True)
        return SlackInviteResult(
            ok=False,
            error=data.get("error") or f"http_{response.status}",
            retry_after_seconds=_retry_after_seconds(response),
        )

    def post_slack_message_idempotent(
        self, channel: str, text: str, client_message_id: str, blocks: list | None = None
    ) -> SlackPostResult:
        """A stable client_msg_id cannot double-post; Slack answers a repeat with ok and the original ts."""
        sent_at = time.time()
        body: dict = {
            "channel": channel,
            "text": to_slack_mrkdwn(text),
            "client_msg_id": client_message_id,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if blocks:
            body["blocks"] = blocks
        response = self._call("chat.postMessage", json_body=body)
        data = response.data
        if data.get("ok") is True and data.get("ts"):
            try:
                deduped = sent_at - float(data["ts"]) > 60
            except ValueError:
                deduped = False
            return SlackPostResult(ok=True, ts=data["ts"], deduped=deduped)
        return SlackPostResult(
            ok=False,
            error=data.get("error") or f"http_{response.status}",
            retry_after_seconds=_retry_after_seconds(response),
        )

    def post_slack_ephemeral(
        self, channel: str, user: str, text: str, thread_ts: str | None = None, blocks: list | None = None
    ) -> bool:
        if not channel or not user:
            log.warning("[slack] chat.postEphemeral skipped: missing channel or user")
            return False
        body: dict = {"channel": channel, "user": user, "text": to_slack_mrkdwn(text), "unfurl_links": False, "unfurl_media": False}
        if blocks:
            body["blocks"] = blocks
        if thread_ts:
            body["thread_ts"] = thread_ts
        data = self._call("chat.postEphemeral", json_body=body).data
        if not data.get("ok"):
            log.warning("[slack] chat.postEphemeral failed: %s channel=%s user=%s", data.get("error") or "unknown", channel, user)
            return False
        return True

    def post_slack_reply_delivery(
        self, channel: str, reply: str, thread_ts: str | None = None, extra_blocks: list | None = None
    ) -> SlackReplyDelivery:
        """Model reply: one sentence-aligned message per Slack markdown payload.

        mrkdwn and plain-text fallbacks apply independently to each message.
        ``complete`` is false unless every chunk landed, so callers that must know the
        whole reply was seen don't read a partial post as delivered.
        """
        if not reply.strip():
            return SlackReplyDelivery(ts=None, complete=False)
        latest_ts: str | None = None
        chunks = split_slack_markdown(reply)
        for index, chunk in enumerate(chunks):
            chunk_ts = self._post_slack_reply_chunk(
                channel, chunk, thread_ts, extra_blocks if index == len(chunks) - 1 else None
            )
            # A prior chunk is already visible, so reporting its timestamp prevents an
            # outer fallback from duplicating the answer if a later request fails.
            if not chunk_ts:
                return SlackReplyDelivery(ts=latest_ts, complete=False)
            latest_ts = chunk_ts
        # An empty chunk list posts nothing, so completeness tracks the last ts.
        return SlackReplyDelivery(ts=latest_ts, complete=bool(latest_ts))

    def post_slack_reply(
        self, channel: str, reply: str, thread_ts: str | None = None, extra_blocks: list | None = None
    ) -> str | None:
        return self.post_slack_reply_delivery(channel, reply, thread_ts, extra_blocks).ts

    def _post_slack_reply_chunk(self, channel: str, reply: str, thread_ts: str | None, extra_blocks: list | None) -> str | None:
        extra = extra_blocks or []
        markdown = self._post_slack_message_raw(channel, plain_text_fallback(reply), thread_ts, [*markdown_reply_blocks(reply), *extra])
        if markdown.get("ok"):
            return markdown.get("ts")
        log.warning("[slack] markdown block reply failed (%s); trying mrkdwn section", markdown.get("error") or "unknown")
        mrkdwn = self._post_slack_message_raw(channel, to_slack_mrkdwn(reply), thread_ts, [*mrkdwn_reply_blocks(reply), *extra])
        if mrkdwn.get("ok"):
            return mrkdwn.get("ts")
        log.warning("[slack] mrkdwn section reply failed (%s); trying plain text", mrkdwn.get("error") or "unknown")
        plain = self._post_slack_message_raw(channel, to_slack_mrkdwn(reply), thread_ts)
        return plain.get("ts") if plain.get("ok") else None

    # --- reading and editing messages -----------------------------------------------

    def get_slack_message_blocks(self, channel: str, thread_ts: str, message_ts: str) -> dict | None:
        data = self._call(
            "conversations.replies",
            http_verb="GET",
            params={"channel": channel, "ts": thread_ts, "oldest": message_ts, "inclusive": "true", "limit": "2"},
        ).data
        if not data.get("ok"):
            log.warning("[slack] conversations.replies failed: %s", data.get("error") or "unknown")
            return None
        message = next((m for m in data.get("messages") or [] if m.get("ts") == message_ts), None)
        if message is None:
            return None
        return {"text": message.get("text") or "", "blocks": message.get("blocks") or []}

    def update_slack_message(self, channel: str, ts: str, text: str, blocks: list | None = None) -> bool:
        body: dict = {
            "channel": channel,
            "ts": ts,
            # Blocks carry the complete formatted answer. Keep the top-level fallback
            # text bounded or chat.update rejects a valid long markdown block.
            "text": plain_text_fallback(text) if blocks else to_slack_mrkdwn(text),
        }
        if blocks is not None:
            body["blocks"] = blocks
        data = self._call("chat.update", json_body=body).data
        if not data.get("ok"):
            log.warning("[slack] chat.update failed: %s", data.get("error") or "unknown")
        return bool(data.get("ok"))

    def delete_slack_message(self, channel: str, ts: str) -> bool:
        data = self._call("chat.delete", json_body={"channel": channel, "ts": ts}).data
        if not data.get("ok"):
            log.warning("[slack] chat.delete failed: %s", data.get("error") or "unknown")
        return bool(data.get("ok"))

    def get_slack_message_permalink(self, channel: str, message_ts: str) -> str | None:
        data = self._call("chat.getPermalink", http_verb="GET", params={"channel": channel, "message_ts": message_ts}).data
        if not data.get("ok"):
            log.warning("[slack] chat.getPermalink failed: %s", data.get("error") or "unknown")
            return None
        return data.get("permalink")

    def get_slack_message_reactions(self, channel: str, message_ts: str) -> list[SlackMessageReaction]:
        """Reactions on one message: the only signal on whether a sent post landed, short of a reply."""
        data = self._call("reactions.get", http_verb="GET", params={"channel": channel, "timestamp": message_ts}).data
        if not data.get("ok"):
            log.warning("[slack] reactions.get failed: %s", data.get("error") or "unknown")
            return []
        reactions = (data.get("message") or {}).get("reactions") or []
        return [SlackMessageReaction(name=r["name"], count=r.get("count") or 0) for r in reactions if r.get("name")]

    # --- approvals ------------------------------------------------------------------

    def post_slack_approval_card(self, channel: str, thread_ts: str, card: SlackApprovalCard) -> str | None:
        return self.post_slack_message(
            channel,
            f"{card.slug or card.tool_name} needs approval from <@{card.asker_user}>.",
            thread_ts,
            approval_blocks(card),
        )

    # --- streams --------------------------------------------------------------------

    def start_slack_stream(
        self,
        channel: str,
        thread_ts: str | None,
        recipient_user_id: str | None = None,
        recipient_team_id: str | None = None,
        task_display_mode: Literal["timeline", "plan", "dense"] | None = None,
    ) -> str | None:
        if not channel or not thread_ts:
            return None
        body: dict = {"channel": channel, "thread_ts": thread_ts}
        if recipient_user_id:
            body["recipient_user_id"] = recipient_user_id
        if recipient_team_id:
            body["recipient_team_id"] = recipient_team_id
        if task_display_mode:
            body["task_display_mode"] = task_display_mode
        data = self._call("chat.startStream", json_body=body).data
        if not data.get("ok"):
            log.warning("[slack] chat.startStream failed: %s", data.get("error") or "unknown")
            return None
        return data.get("ts")

    def append_slack_stream_reply(self, channel: str, ts: str, reply: str) -> bool:
        """Append sentence-aligned markdown_text chunks, then a mrkdwn block fallback."""
        chunks = [{"type": "markdown_text", "text": text} for text in split_slack_markdown(reply)]
        if self.append_slack_stream_chunks(channel, ts, chunks):
            return True
        log.warning("[slack] markdown_text stream append failed; trying mrkdwn blocks chunk")
        return self.append_slack_stream_chunks(channel, ts, [{"type": "blocks", "blocks": mrkdwn_reply_blocks(reply)}])

    def append_slack_stream_chunks(self, channel: str, ts: str, chunks: list[StreamChunk]) -> bool:
        if not chunks:
            return True
        data = self._call("chat.appendStream", json_body={"channel": channel, "ts": ts, "chunks": chunks}).data
        if not data.get("ok"):
            log.warning("[slack] chat.appendStream failed: %s", data.get("error") or "unknown")
        return bool(data.get("ok"))

    def stop_slack_stream(
        self,
        channel: str,
        ts: str,
        chunks: list[StreamChunk] | None = None,
        markdown_text: str | None = None,
        blocks: list | None = None,
    ) -> bool:
        markdown_chunks = (
            [{"type": "markdown_text", "text": text} for text in split_slack_markdown(markdown_text)]
            if markdown_text and markdown_text.strip()
            else []
        )
        all_chunks = [*(chunks or []), *markdown_chunks]
        body: dict = {"channel": channel, "ts": ts}
        if all_chunks:
            body["chunks"] = all_chunks
        if blocks:
            body["blocks"] = blocks
        data = self._call("chat.stopStream", json_body=body).data
        if not data.get("ok"):
            log.warning("[slack] chat.stopStream failed: %s", data.get("error") or "unknown")
        return bool(data.get("ok"))

    # --- identity -------------------------------------------------------------------

    def get_slack_bot_identity(self) -> dict:
        data = self._call("auth.test").data
        if not data.get("ok"):
            return {}
        return {"user_id": data.get("user_id"), "bot_id": data.get("bot_id"), "team_id": data.get("team_id"), "user": data.get("user")}

    def get_slack_bot_user_id(self) -> str | None:
        return self.get_slack_bot_identity().get("user_id")

    def lookup_slack_user_info(self, user_id: str) -> SlackUserInfoLookup:
        if not user_id:
            return SlackUserInfoLookup(ok=False, reason="missing_user_id")
        response = self._call("users.info", http_verb="GET", params={"user": user_id})
        data = response.data
        if not data.get("ok"):
            if data.get("error") == "user_not_found":
                return SlackUserInfoLookup(ok=False, reason="user_not_found", error="user_not_found")
            return SlackUserInfoLookup(
                ok=False,
                reason="slack_api_error",
                error=data.get("error") or f"http_{response.status}",
                retry_after_seconds=_retry_after_seconds(response),
            )
        if not data.get("user"):
            return SlackUserInfoLookup(ok=False, reason="profile_unavailable")
        user = _slack_user_info_from_member(data["user"])
        if user is None:
            return SlackUserInfoLookup(ok=False, reason="profile_unavailable")
        return SlackUserInfoLookup(ok=True, user=user)

    def get_slack_user_info(self, user_id: str) -> SlackUserInfo:
        lookup = self.lookup_slack_user_info(user_id)
        if lookup.ok and lookup.user:
            return lookup.user
        if lookup.reason == "slack_api_error":
            log.warning("[slack] users.info error user=%s: %s", user_id or "?", lookup.error or "unknown")
        return SlackUserInfo(id=user_id)

    def lookup_slack_user_by_email(self, email: str) -> str | None:
        if not email:
            return None
        data = self._call("users.lookupByEmail", http_verb="GET", params={"email": email}).data
        user_id = (data.get("user") or {}).get("id")
        if not data.get("ok") or not user_id:
            log.warning("[slack] users.lookupByEmail failed: %s", data.get("error") or "unknown")
            return None
        return user_id

    def list_slack_users_page(self, cursor: str | None = None, limit: int = 200) -> SlackCursorPage:
        """One users.list page for resumable workflows."""
        params = {"limit": str(min(limit, 200))}
        if cursor:
            params["cursor"] = cursor
        response = self._call("users.list", http_verb="GET", params=params)
        data = response.data
        if data.get("ok") is not True:
            return SlackCursorPage(
                ok=False, error=data.get("error") or f"http_{response.status}", retry_after_seconds=_retry_after_seconds(response)
            )
        items = [mapped for mapped in (map_slack_member(m) for m in data.get("members") or []) if mapped]
        next_cursor = _cursor(data)
        return SlackCursorPage(ok=True, items=items, next_cursor=next_cursor, complete=not next_cursor)

    def get_slack_team_directory(self, max_members: int = 1000) -> list[SlackMember]:
        out: list[SlackMember] = []
        cursor = None
        while len(out) < max_members:
            params = {"limit": "200"}
            if cursor:
                params["cursor"] = cursor
            data = self._call("users.list", http_verb="GET", params=params).data
            if not data.get("ok") or not data.get("members"):
                break
            for member in data["members"]:
                mapped = map_slack_member(member)
                if mapped:
                    out.append(mapped)
                if len(out) >= max_members:
                    break
            cursor = _cursor(data)
            if not cursor:
                break
        return out

    def get_slack_user_groups(self) -> list[SlackUserGroup]:
        data = self._call("usergroups.list", http_verb="GET", params={"include_users": "true", "include_disabled": "false"}).data
        groups = data.get("usergroups") or []
        if not data.get("ok") or not groups:
            if data.get("error"):
                log.warning("[slack] usergroups.list: %s", data["error"])
            return []
        return [
            SlackUserGroup(
                id=g["id"],
                handle=g.get("handle") or g.get("name") or g["id"],
                name=g.get("name") or g.get("handle") or g["id"],
                user_ids=g["users"] if isinstance(g.get("users"), list) else [],
            )
            for g in groups
            if g.get("id")
        ]

    # --- conversations --------------------------------------------------------------

    def lookup_slack_conversation_info(self, channel: str) -> SlackConversationInfoLookup:
        if not channel:
            return SlackConversationInfoLookup(ok=False, reason="missing_channel_id")
        response = self._call("conversations.info", http_verb="GET", params={"channel": channel})
        data = response.data
        if not data.get("ok"):
            log.warning("[slack] conversations.info failed: %s channel=%s", data.get("error") or "unknown", channel)
            return SlackConversationInfoLookup(
                ok=False,
                reason="slack_api_error"
                if _is_transient_slack_conversation_error(response.status, data.get("error"))
                else "conversation_unavailable",
                error=data.get("error") or f"http_{response.status}",
            )
        info = data.get("channel")
        if not info:
            return SlackConversationInfoLookup(ok=False, reason="conversation_unavailable", error="channel_unavailable")
        return SlackConversationInfoLookup(
            ok=True,
            info=SlackConversationInfo(
                id=info.get("id"),
                name=info.get("name"),
                topic=_str((info.get("topic") or {}).get("value")),
                purpose=_str((info.get("purpose") or {}).get("value")),
                # Group DMs and legacy groups are private rooms too.
                is_private=bool(info.get("is_private") or info.get("is_group") or info.get("is_mpim")),
                is_member=info.get("is_member"),
                is_archived=info.get("is_archived"),
                is_im=info.get("is_im"),
            ),
        )

    def get_slack_conversation_info(self, channel: str) -> SlackConversationInfo | None:
        lookup = self.lookup_slack_conversation_info(channel)
        return lookup.info if lookup.ok else None

    def get_cached_slack_conversation_info(self, team_id: str, channel: str) -> SlackConversationInfo | None:
        """Short-lived channel identity cache for passive triage context."""
        from src.db.falkordb_client import kv

        key = f"brain:slack:conversation-info:{team_id}:{channel}"
        try:
            cached = kv().get(key)
            if cached:
                return SlackConversationInfo(**json.loads(cached))
        except Exception:
            pass
        info = self.get_slack_conversation_info(channel)
        if info is not None:
            try:
                kv().set(key, json.dumps(asdict(info)), ex=SLACK_CONVERSATION_INFO_CACHE_TTL_SECONDS)
            except Exception:
                pass
        return info

    def open_slack_conversation(self, user_id: str) -> str | None:
        if not user_id:
            return None
        data = self._call("conversations.open", json_body={"users": user_id}).data
        if not data.get("ok"):
            log.warning("[slack] conversations.open failed: %s user=%s", data.get("error") or "unknown", user_id)
            return None
        return (data.get("channel") or {}).get("id")

    def set_assistant_thread_status(
        self, channel_id: str, thread_ts: str, status: str, loading_messages: list[str] | None = None
    ) -> bool:
        if not channel_id or not thread_ts:
            return False
        body: dict = {"channel_id": channel_id, "thread_ts": thread_ts, "status": status}
        if loading_messages:
            body["loading_messages"] = loading_messages[:10]
        data = self._call("assistant.threads.setStatus", json_body=body).data
        if not data.get("ok"):
            log.warning("[slack] assistant.threads.setStatus failed: %s", data.get("error") or "unknown")
        return bool(data.get("ok"))

    def clear_assistant_thread_status(self, channel_id: str, thread_ts: str) -> bool:
        return self.set_assistant_thread_status(channel_id, thread_ts, "")

    # --- reactions ------------------------------------------------------------------

    def add_slack_reaction_detailed(
        self, channel: str, timestamp: str, name: str, quiet_errors: list[str] | None = None
    ) -> SlackReactionAddResult:
        if not channel or not timestamp:
            return SlackReactionAddResult(ok=False, error="missing_target")
        response = self._call("reactions.add", json_body={"channel": channel, "timestamp": timestamp, "name": name})
        data = response.data
        if data.get("ok"):
            return SlackReactionAddResult(ok=True, outcome="added")
        if data.get("error") == "already_reacted":
            return SlackReactionAddResult(ok=True, outcome="already_present")
        if not data.get("error") or data["error"] not in (quiet_errors or []):
            log.warning("[slack] reactions.add failed: %s", data.get("error") or "unknown")
        retry_after = None
        try:
            retry_after = int(response.headers.get("retry-after") or response.headers.get("Retry-After") or "")
        except ValueError:
            pass
        return SlackReactionAddResult(
            ok=False,
            error=data.get("error") or "unknown",
            retry_after_seconds=retry_after if retry_after and retry_after > 0 else None,
        )

    def add_slack_reaction(self, channel: str, timestamp: str, name: str, quiet_errors: list[str] | None = None) -> bool:
        return self.add_slack_reaction_detailed(channel, timestamp, name, quiet_errors).ok

    def remove_slack_reaction(self, channel: str, timestamp: str, name: str) -> None:
        if not channel or not timestamp:
            return
        data = self._call("reactions.remove", json_body={"channel": channel, "timestamp": timestamp, "name": name}).data
        if not data.get("ok") and data.get("error") != "no_reaction":
            log.warning("[slack] reactions.remove failed: %s", data.get("error") or "unknown")

    def swap_slack_reaction(self, channel: str, timestamp: str, old: str, new: str) -> None:
        self.remove_slack_reaction(channel, timestamp, old)
        self.add_slack_reaction(channel, timestamp, new)

    # --- history --------------------------------------------------------------------

    def get_slack_channel_history_page(
        self, channel: str, *, oldest: str, latest: str, cursor: str | None = None, limit: int = 100
    ) -> SlackCursorPage:
        """One resumable channel-history page.

        Unlike the interactive helpers, this does not sleep or retry a 429; callers
        persist their cursor and schedule the next attempt using Retry-After.
        """
        params = {"channel": channel, "oldest": oldest, "latest": latest, "inclusive": "true", "limit": str(min(limit, 200))}
        if cursor:
            params["cursor"] = cursor
        response = self._call("conversations.history", http_verb="GET", params=params)
        data = response.data
        if data.get("ok") is not True:
            return SlackCursorPage(
                ok=False, error=data.get("error") or f"http_{response.status}", retry_after_seconds=_retry_after_seconds(response)
            )
        next_cursor = _cursor(data)
        return SlackCursorPage(
            ok=True, items=data.get("messages") or [], next_cursor=next_cursor, complete=not next_cursor or data.get("has_more") is False
        )

    def get_slack_thread_history_page(self, channel: str, thread_ts: str, cursor: str | None = None, limit: int = 100) -> SlackCursorPage:
        """One cursor page of replies for the resumable public-channel backfill."""
        params = {"channel": channel, "ts": thread_ts, "limit": str(min(limit, 200))}
        if cursor:
            params["cursor"] = cursor
        response = self._call("conversations.replies", http_verb="GET", params=params)
        data = response.data
        if data.get("ok") is not True:
            return SlackCursorPage(
                ok=False, error=data.get("error") or f"http_{response.status}", retry_after_seconds=_retry_after_seconds(response)
            )
        next_cursor = _cursor(data)
        return SlackCursorPage(ok=True, items=data.get("messages") or [], next_cursor=next_cursor, complete=not next_cursor)

    def get_slack_thread_history(
        self,
        channel: str,
        thread_ts: str,
        *,
        page_limit: int = 200,
        max_messages: int | None = None,
        max_pages: int = 1,
        cursor: str | None = None,
        oldest: str | None = None,
    ) -> SlackThreadHistory:
        """Cursor-aware thread read.

        Callers decide how much history to fetch and how much of it to place in a
        prompt; fetching and prompt injection are deliberately separate budgets.
        """
        page_limit = min(max(page_limit, 1), 200)
        max_messages = max(max_messages if max_messages is not None else page_limit, 1)
        max_pages = max(max_pages, 1)
        collected: list[SlackThreadMessage] = []
        cursor = (cursor or "").strip() or None
        complete = False
        error: str | None = None
        page = 0
        while page < max_pages and len(collected) < max_messages:
            request_cursor = cursor
            params = {"channel": channel, "ts": thread_ts, "limit": str(min(page_limit, max_messages - len(collected)))}
            if oldest:
                params["oldest"] = oldest
                params["inclusive"] = "false"
            if request_cursor:
                params["cursor"] = request_cursor
            data = self._get_with_retry("conversations.replies", params).data
            if not data.get("ok"):
                log.warning("[slack] conversations.replies failed: %s", data.get("error") or "unknown")
                # Preserve the cursor that failed so an on-demand read can retry it.
                cursor = request_cursor
                error = data.get("error") or "unknown"
                break
            collected.extend(data.get("messages") or [])
            next_cursor = _cursor(data)
            if not next_cursor:
                cursor = None
                complete = True
                break
            cursor = next_cursor
            page += 1

        seen: set[str] = set()
        messages: list[SlackThreadMessage] = []
        for index, message in enumerate(collected):
            key = message.get("ts") or f"index:{index}"
            if key in seen:
                continue
            seen.add(key)
            messages.append(message)
        if error:
            return SlackThreadHistory(ok=False, messages=messages, complete=False, next_cursor=cursor, error=error)
        return SlackThreadHistory(ok=True, messages=messages, complete=complete, next_cursor=cursor)

    def get_slack_thread(self, channel: str, thread_ts: str, limit: int = 30) -> list[SlackThreadMessage]:
        history = self.get_slack_thread_history(
            channel, thread_ts, page_limit=min(limit, 200), max_messages=limit, max_pages=max(1, -(-limit // 200))
        )
        return history.messages

    def get_slack_channel_history_result(
        self,
        channel: str,
        *,
        oldest: str | None = None,
        latest: str | None = None,
        inclusive: bool | None = None,
        limit: int | None = None,
        max_messages: int | None = None,
        max_pages: int | None = None,
    ) -> SlackChannelHistoryResult:
        """Newest pages first from Slack, returned oldest-first."""
        page_limit = limit or 80
        cap = max_messages or page_limit
        pages = max_pages or (8 if max_messages else 1)
        collected: list[SlackThreadMessage] = []
        cursor: str | None = None
        complete = False
        for _page in range(pages):
            if len(collected) >= cap:
                break
            params = {"channel": channel, "limit": str(min(page_limit, cap - len(collected)))}
            if oldest:
                params["oldest"] = oldest
            if latest:
                params["latest"] = latest
            if oldest or latest:
                params["inclusive"] = "true" if inclusive else "false"
            if cursor:
                params["cursor"] = cursor
            data = self._get_with_retry("conversations.history", params).data
            if not data.get("ok"):
                log.warning("[slack] conversations.history failed: %s channel=%s", data.get("error") or "unknown", channel)
                return SlackChannelHistoryResult(
                    ok=False, messages=list(reversed(collected)), complete=False, error=data.get("error") or "unknown"
                )
            collected.extend(data.get("messages") or [])
            cursor = _cursor(data)
            if not cursor or not data.get("has_more"):
                complete = True
                break
        return SlackChannelHistoryResult(ok=True, messages=list(reversed(collected)), complete=complete)

    def get_slack_channel_history(self, channel: str, **opts) -> SlackChannelHistoryResult:
        return self.get_slack_channel_history_result(channel, **opts)

    # --- channels -------------------------------------------------------------------

    def list_slack_public_channels_page(self, cursor: str | None = None, limit: int = 200) -> SlackCursorPage:
        """One page of every public channel visible to the app."""
        params = {
            "types": "public_channel",
            "exclude_archived": "false",
            # Slack omits num_members unless asked; the rollout ranks on it.
            "include_num_members": "true",
            "limit": str(min(limit, 200)),
        }
        if cursor:
            params["cursor"] = cursor
        response = self._call("conversations.list", http_verb="GET", params=params)
        data = response.data
        if data.get("ok") is not True:
            return SlackCursorPage(
                ok=False, error=data.get("error") or f"http_{response.status}", retry_after_seconds=_retry_after_seconds(response)
            )
        items = [
            SlackPublicChannel(
                id=c["id"],
                name=c["name"],
                topic=_str((c.get("topic") or {}).get("value")),
                purpose=_str((c.get("purpose") or {}).get("value")),
                is_member=c.get("is_member") is True,
                is_archived=c.get("is_archived") is True,
                is_general=c.get("is_general") is True,
                is_external=c.get("is_ext_shared") is True
                or c.get("is_pending_ext_shared") is True
                or (c.get("is_shared") is True and c.get("is_org_shared") is not True),
                member_count=c["num_members"] if isinstance(c.get("num_members"), int) else None,
            )
            for c in data.get("channels") or []
            if c.get("id") and c.get("name")
        ]
        next_cursor = _cursor(data)
        return SlackCursorPage(ok=True, items=items, next_cursor=next_cursor, complete=not next_cursor)

    def join_slack_public_channel(self, channel: str) -> SlackJoinResult:
        """Join one public channel without hiding Slack's rate-limit signal."""
        response = self._call("conversations.join", json_body={"channel": channel})
        data = response.data
        if data.get("ok") is True:
            return SlackJoinResult(ok=True, already_member=False)
        if data.get("error") == "already_in_channel":
            return SlackJoinResult(ok=True, already_member=True)
        return SlackJoinResult(
            ok=False, error=data.get("error") or f"http_{response.status}", retry_after_seconds=_retry_after_seconds(response)
        )

    def get_bot_conversations(self, max_channels: int = 1000, require_complete: bool = False) -> list[SlackConversation]:
        out: list[SlackConversation] = []
        cursor = None
        while len(out) < max_channels:
            params = {"types": "public_channel,private_channel", "exclude_archived": "true", "limit": "200"}
            if cursor:
                params["cursor"] = cursor
            response = self._get_with_retry("users.conversations", params)
            data = response.data
            if not data.get("ok"):
                if require_complete:
                    raise RuntimeError(data.get("error") or f"http_{response.status}")
                log.warning("[slack] users.conversations failed: %s", data.get("error") or "unknown")
                break
            for ch in data.get("channels") or []:
                if ch.get("id") and ch.get("name"):
                    out.append(SlackConversation(id=ch["id"], name=ch["name"], is_private=bool(ch.get("is_private"))))
                if len(out) >= max_channels:
                    break
            cursor = _cursor(data)
            if not cursor:
                break
        if require_complete and (cursor or len(out) >= max_channels):
            raise RuntimeError("Slack channel lookup incomplete")
        return out

    def get_conversation_members(self, channel: str, max_members: int = 1000) -> list[str]:
        out: list[str] = []
        cursor = None
        while len(out) < max_members:
            params = {"channel": channel, "limit": "200"}
            if cursor:
                params["cursor"] = cursor
            data = self._get_with_retry("conversations.members", params).data
            if not data.get("ok"):
                log.warning("[slack] conversations.members failed: %s channel=%s", data.get("error") or "unknown", channel)
                break
            for member_id in data.get("members") or []:
                out.append(member_id)
                if len(out) >= max_members:
                    break
            cursor = _cursor(data)
            if not cursor:
                break
        return out

    # --- install lifecycle ----------------------------------------------------------

    def uninstall_slack_app(self, client_id: str, client_secret: str) -> SlackUninstallOutcome:
        """``transient`` means Slack never gave a verdict, so keep the token and retry."""
        response = self._call("apps.uninstall", form={"client_id": client_id, "client_secret": client_secret})
        if response.status == 0 or response.status >= 500:
            return "transient"
        if response.data.get("ok"):
            return "revoked"
        error = response.data.get("error") or "unknown"
        log.warning("[slack] apps.uninstall failed: %s", error)
        if error in SLACK_DEAD_TOKEN_ERRORS:
            return "terminal"
        if error in SLACK_RETRYABLE_ERRORS:
            return "transient"
        return self._revoke_slack_token()

    def _revoke_slack_token(self) -> SlackUninstallOutcome:
        response = self._call("auth.revoke")
        if response.status == 0 or response.status >= 500:
            return "transient"
        if response.data.get("ok"):
            return "revoked"
        error = response.data.get("error") or "unknown"
        log.warning("[slack] auth.revoke failed: %s", error)
        return "transient" if error in SLACK_RETRYABLE_ERRORS else "terminal"

    # --- short names (stable surface for the turn code and tests) -------------------

    def bot_user_id(self) -> str | None:
        return self.get_slack_bot_user_id()

    def user_info(self, user_id: str) -> SlackUserInfo | None:
        """None means the lookup failed. Callers must not guess."""
        lookup = self.lookup_slack_user_info(user_id)
        if not lookup.ok:
            log.warning("[slack] users.info failed user=%s: %s", user_id, lookup.error or lookup.reason)
        return lookup.user if lookup.ok else None

    def conversation_info(self, channel: str) -> SlackConversationInfo | None:
        """None means the lookup failed. Callers must fail closed."""
        return self.get_slack_conversation_info(channel)

    def conversation_members(self, channel: str) -> list[str]:
        return self.get_conversation_members(channel)

    def bot_private_channels(self) -> list[SlackConversationInfo]:
        """Private channels and group DMs the bot is in."""
        channels: list[SlackConversationInfo] = []
        cursor = None
        while True:
            params = {"types": "private_channel,mpim", "exclude_archived": "true", "limit": "200"}
            if cursor:
                params["cursor"] = cursor
            data = self._get_with_retry("users.conversations", params).data
            if not data.get("ok"):
                log.warning("[slack] users.conversations failed: %s", data.get("error") or "unknown")
                return channels
            for item in data.get("channels") or []:
                channels.append(SlackConversationInfo(id=item["id"], name=item.get("name"), is_private=True, is_member=True))
            cursor = _cursor(data)
            if not cursor:
                return channels

    def open_conversation(self, user_id: str) -> str | None:
        return self.open_slack_conversation(user_id)

    def post_message(self, channel: str, text: str, thread_ts: str | None = None, blocks: list | None = None) -> str | None:
        return self.post_slack_message(channel, text, thread_ts, blocks)

    def update_message(self, channel: str, ts: str, text: str, blocks: list | None = None) -> bool:
        return self.update_slack_message(channel, ts, text, blocks)

    def post_ephemeral(self, channel: str, user: str, text: str, thread_ts: str | None = None) -> bool:
        return self.post_slack_ephemeral(channel, user, text, thread_ts)

    def add_reaction(self, channel: str, ts: str, name: str) -> bool:
        return self.add_slack_reaction(channel, ts, name)

    def remove_reaction(self, channel: str, ts: str, name: str) -> None:
        self.remove_slack_reaction(channel, ts, name)

    def swap_reaction(self, channel: str, ts: str, old: str, new: str) -> None:
        self.swap_slack_reaction(channel, ts, old, new)

    def post_approval_card(self, channel: str, thread_ts: str, card: SlackApprovalCard) -> str | None:
        return self.post_slack_approval_card(channel, thread_ts, card)


# --- module-level helpers ----------------------------------------------------------------


def _clip(text: str, size: int) -> str:
    return text if len(text) <= size else text[: size - 1] + "…"


def _action_labels(action: str) -> tuple[str | None, str]:
    app, _, operation = action.partition(" · ")
    if not app or not operation:
        return None, action
    return app, operation


def approval_blocks(card: SlackApprovalCard, status: str | None = None) -> list[dict]:
    """Approve/Deny card. Buttons only while pending; a decided card records who asked."""
    expires = int(card.expires_at)
    action = humanize_tool_action(card.slug or card.tool_name)
    app, operation = _action_labels(action)
    emoji, label = APPROVAL_STATUS_META.get(status or "", ("", status or ""))
    title = f"{operation} {label.lower()}".strip() if status else f"{operation} needs approval"
    subtitle = " · ".join(part for part in (app, "Decision recorded" if status else "Review exact action") if part)
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": _clip(title, 150), "emoji": True}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": _clip(subtitle, 150)}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": _clip(f"*What will happen*\n{to_slack_mrkdwn(card.summary)}", 2900)}},
    ]
    if status:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{emoji} {label} · Requested by <@{card.asker_user}>"}]}
        )
        return blocks
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"<@{card.asker_user}> requested this · Expires <!date^{expires}^{{time}}|in 15 minutes> · 🔒 Only what's listed above will run",
                }
            ],
        }
    )
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": APPROVAL_APPROVE_ACTION_ID,
                    "value": card.approval_id,
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve action", "emoji": True},
                },
                {
                    "type": "button",
                    "action_id": APPROVAL_DENY_ACTION_ID,
                    "value": card.approval_id,
                    "text": {"type": "plain_text", "text": "Deny", "emoji": True},
                },
            ],
        }
    )
    return blocks


def resolved_approval_blocks(card: SlackApprovalCard, status: str) -> list[dict]:
    return approval_blocks(card, status)


def _post_response_url(response_url: str, body: dict) -> bool:
    request = urllib.request.Request(
        response_url,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if not 200 <= response.status < 300:
                log.warning("[slack] response_url call failed HTTP %s", response.status)
                return False
            return True
    except OSError as error:
        log.warning("[slack] response_url error: %s", error)
        return False


def update_interaction_response(
    response_url: str,
    text: str,
    blocks: list | None = None,
    replace_original: bool = False,
    response_type: str | None = None,
) -> bool:
    if not response_url:
        return False
    body: dict = {"text": to_slack_mrkdwn(text), "replace_original": replace_original}
    if blocks is not None:
        body["blocks"] = blocks
    if response_type:
        body["response_type"] = response_type
    return _post_response_url(response_url, body)


def delete_interaction_response(response_url: str) -> bool:
    if not response_url:
        return False
    return _post_response_url(response_url, {"delete_original": True})


def exchange_slack_oauth(client_id: str, client_secret: str, code: str, redirect_uri: str) -> SlackOAuthResult:
    """oauth.v2.access. Raises when Slack refuses, like the original."""
    try:
        data = WebClient(base_url=settings.slack_api_base_url).oauth_v2_access(client_id=client_id, client_secret=client_secret, code=code, redirect_uri=redirect_uri).data
    except SlackApiError as error:
        raise RuntimeError(f"slack oauth.v2.access failed: {error.response.get('error') or 'unknown'}") from error
    team = data.get("team") or {}
    if not data.get("ok") or not data.get("access_token") or not team.get("id"):
        raise RuntimeError(f"slack oauth.v2.access failed: {data.get('error') or 'unknown'}")
    return SlackOAuthResult(
        team_id=team["id"],
        team_name=team.get("name"),
        bot_user_id=data.get("bot_user_id"),
        bot_token=data["access_token"],
        app_id=data.get("app_id"),
        scopes=data.get("scope"),
        authed_user_id=(data.get("authed_user") or {}).get("id"),
    )


def now_seconds() -> float:
    return time.time()
