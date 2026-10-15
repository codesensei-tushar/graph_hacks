"""Slack Web API calls the brain uses. Subset of company-brain ``brain/slack/client.ts`` on slack_sdk."""

import json
import logging
import time
import urllib.request
from dataclasses import dataclass

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

log = logging.getLogger(__name__)

APPROVAL_APPROVE_ACTION_ID = "brain_approval_approve"
APPROVAL_DENY_ACTION_ID = "brain_approval_deny"

APPROVAL_STATUS_META = {
    "Approved": ("✅", "Approved"),
    "Denied": ("🚫", "Denied"),
    "Expired": ("⌛", "Expired"),
    "Cancelled": ("🛑", "Cancelled"),
}


@dataclass
class SlackConversationInfo:
    id: str | None = None
    name: str | None = None
    is_private: bool | None = None
    is_member: bool | None = None
    is_archived: bool | None = None
    is_im: bool | None = None


@dataclass
class SlackUserInfo:
    id: str
    name: str | None = None
    real_name: str | None = None
    display_name: str | None = None
    email: str | None = None
    is_bot: bool = False
    is_restricted: bool = False


@dataclass
class SlackApprovalCard:
    approval_id: str
    summary: str
    tool_name: str
    asker_user: str
    expires_at: float


def _clip(text: str, size: int) -> str:
    return text if len(text) <= size else text[: size - 1] + "…"


class SlackClient:
    def __init__(self, token: str):
        self.web = WebClient(token=token)

    # --- identity -----------------------------------------------------------------

    def bot_user_id(self) -> str | None:
        try:
            return self.web.auth_test().get("user_id")
        except SlackApiError as error:
            log.warning("[slack] auth.test failed: %s", error.response.get("error"))
            return None

    def user_info(self, user_id: str) -> SlackUserInfo | None:
        try:
            user = self.web.users_info(user=user_id)["user"]
        except SlackApiError as error:
            log.warning("[slack] users.info failed user=%s: %s", user_id, error.response.get("error"))
            return None
        profile = user.get("profile") or {}
        return SlackUserInfo(
            id=user["id"],
            name=user.get("name"),
            real_name=user.get("real_name") or profile.get("real_name"),
            display_name=profile.get("display_name") or None,
            email=profile.get("email"),
            is_bot=bool(user.get("is_bot")),
            is_restricted=bool(user.get("is_restricted") or user.get("is_ultra_restricted")),
        )

    # --- conversations ------------------------------------------------------------

    def conversation_info(self, channel: str) -> SlackConversationInfo | None:
        """None means the lookup failed. Callers must fail closed."""
        try:
            info = self.web.conversations_info(channel=channel)["channel"]
        except SlackApiError as error:
            log.warning("[slack] conversations.info failed channel=%s: %s", channel, error.response.get("error"))
            return None
        return SlackConversationInfo(
            id=info.get("id"),
            name=info.get("name"),
            is_private=bool(info.get("is_private") or info.get("is_group") or info.get("is_mpim")),
            is_member=info.get("is_member"),
            is_archived=info.get("is_archived"),
            is_im=info.get("is_im"),
        )

    def conversation_members(self, channel: str) -> list[str]:
        members: list[str] = []
        cursor = None
        try:
            while True:
                page = self.web.conversations_members(channel=channel, cursor=cursor, limit=200)
                members.extend(page.get("members") or [])
                cursor = (page.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    return members
        except SlackApiError as error:
            log.warning("[slack] conversations.members failed channel=%s: %s", channel, error.response.get("error"))
            return []

    def bot_private_channels(self) -> list[SlackConversationInfo]:
        channels: list[SlackConversationInfo] = []
        cursor = None
        try:
            while True:
                page = self.web.users_conversations(types="private_channel,mpim", cursor=cursor, limit=200, exclude_archived=True)
                for item in page.get("channels") or []:
                    channels.append(SlackConversationInfo(id=item["id"], name=item.get("name"), is_private=True, is_member=True))
                cursor = (page.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    return channels
        except SlackApiError as error:
            log.warning("[slack] users.conversations failed: %s", error.response.get("error"))
            return channels

    def open_conversation(self, user_id: str) -> str | None:
        try:
            return self.web.conversations_open(users=user_id)["channel"]["id"]
        except SlackApiError as error:
            log.warning("[slack] conversations.open failed user=%s: %s", user_id, error.response.get("error"))
            return None

    # --- messages -----------------------------------------------------------------

    def post_message(self, channel: str, text: str, thread_ts: str | None = None, blocks: list | None = None) -> str | None:
        try:
            response = self.web.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts, blocks=blocks, unfurl_links=False)
            return response.get("ts")
        except SlackApiError as error:
            log.warning("[slack] chat.postMessage failed channel=%s: %s", channel, error.response.get("error"))
            return None

    def update_message(self, channel: str, ts: str, text: str, blocks: list | None = None) -> bool:
        try:
            self.web.chat_update(channel=channel, ts=ts, text=text, blocks=blocks)
            return True
        except SlackApiError as error:
            log.warning("[slack] chat.update failed channel=%s: %s", channel, error.response.get("error"))
            return False

    def post_ephemeral(self, channel: str, user: str, text: str, thread_ts: str | None = None) -> None:
        try:
            self.web.chat_postEphemeral(channel=channel, user=user, text=text, thread_ts=thread_ts)
        except SlackApiError as error:
            log.warning("[slack] chat.postEphemeral failed channel=%s: %s", channel, error.response.get("error"))

    def add_reaction(self, channel: str, ts: str, name: str) -> bool:
        try:
            self.web.reactions_add(channel=channel, timestamp=ts, name=name)
            return True
        except SlackApiError as error:
            if error.response.get("error") == "already_reacted":
                return True
            log.warning("[slack] reactions.add failed: %s", error.response.get("error"))
            return False

    def remove_reaction(self, channel: str, ts: str, name: str) -> None:
        try:
            self.web.reactions_remove(channel=channel, timestamp=ts, name=name)
        except SlackApiError:
            pass

    def swap_reaction(self, channel: str, ts: str, old: str, new: str) -> None:
        self.remove_reaction(channel, ts, old)
        self.add_reaction(channel, ts, new)

    # --- approvals ----------------------------------------------------------------

    def post_approval_card(self, channel: str, thread_ts: str, card: SlackApprovalCard) -> str | None:
        return self.post_message(
            channel,
            f"{card.tool_name} needs approval from <@{card.asker_user}>.",
            thread_ts,
            approval_blocks(card),
        )


def approval_blocks(card: SlackApprovalCard, status: str | None = None) -> list[dict]:
    expires = int(card.expires_at)
    emoji, label = APPROVAL_STATUS_META.get(status or "", ("", status or ""))
    title = f"{card.tool_name} {label.lower()}".strip() if status else f"{card.tool_name} needs approval"
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": _clip(title, 150), "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _clip(f"*What will happen*\n{card.summary}", 2900)}},
    ]
    if status:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"{emoji} {label} · Requested by <@{card.asker_user}>"}],
            }
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


def update_interaction_response(
    response_url: str,
    text: str,
    blocks: list | None = None,
    replace_original: bool = False,
    response_type: str | None = None,
) -> bool:
    if not response_url:
        return False
    body: dict = {"text": text, "replace_original": replace_original}
    if blocks is not None:
        body["blocks"] = blocks
    if response_type:
        body["response_type"] = response_type
    request = urllib.request.Request(
        response_url,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return 200 <= response.status < 300
    except OSError as error:
        log.warning("[slack] response_url update error: %s", error)
        return False


def now_seconds() -> float:
    return time.time()
