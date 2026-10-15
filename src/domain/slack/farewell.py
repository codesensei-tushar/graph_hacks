"""Port of company-brain `brain/slack/farewell.ts`.

One idempotent goodbye per install in #company-brain. A retryable failure returns
``hold`` so nothing is torn down and the retry can still speak; a missing or dead
channel is ``skipped``, never a blocker.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from src.domain.slack.lifecycle_api import SlackConversation, SlackPostResult, deterministic_uuid

log = logging.getLogger(__name__)

HOME_CHANNEL_NAME = "company-brain"

SLACK_FAREWELL_TERMINAL = frozenset(
    {
        "account_inactive",
        "cannot_reply_to_broadcast",
        "channel_not_found",
        "invalid_auth",
        "is_archived",
        "is_inactive",
        "msg_too_long",
        "not_in_channel",
        "restricted_action",
        "token_expired",
        "token_revoked",
    }
)

SLACK_FAREWELL_RETRYABLE = frozenset(
    {"fatal_error", "http_429", "internal_error", "ratelimited", "request_timeout", "service_unavailable"}
)


@dataclass
class SlackFarewellDeps:
    post_message_idempotent: Callable[..., SlackPostResult]  # (web, channel, text, client_message_id)
    list_bot_conversations: Callable[..., list[SlackConversation]]  # (web)


def should_hold_for_farewell(result: SlackPostResult) -> bool:
    error = result.error or ""
    if result.retry_after_seconds:
        return True
    if error in SLACK_FAREWELL_RETRYABLE:
        return True
    if re.fullmatch(r"http_5\d\d", error):
        return True
    if error in SLACK_FAREWELL_TERMINAL:
        return False
    if error.startswith("http_"):
        return False
    return True


def slack_farewell_client_msg_id(kind: str, team_id: str, channel_id: str) -> str:
    return deterministic_uuid(f"cb-{kind}:{team_id}:{channel_id}")


def post_slack_farewell(
    *,
    web,
    team_id: str,
    kind: str,
    text: str,
    known_home_channel_id: str | None,
    deps: SlackFarewellDeps,
) -> str:
    """``posted`` | ``already_posted`` | ``skipped`` | ``hold``.

    Falls back to the bot's joined channels so a retry after a state reset still finds home.
    """
    channel_id = known_home_channel_id
    if not channel_id:
        try:
            conversations = deps.list_bot_conversations(web)
            channel_id = next((c.id for c in conversations if c.name == HOME_CHANNEL_NAME), None)
        except Exception as error:
            log.warning("[slack] farewell home lookup failed team=%s: %s", team_id, error)
            return "skipped" if str(error) in SLACK_FAREWELL_TERMINAL else "hold"
    if not channel_id:
        return "skipped"

    posted = deps.post_message_idempotent(web, channel_id, text, slack_farewell_client_msg_id(kind, team_id, channel_id))
    if posted.ok:
        return "already_posted" if posted.deduped else "posted"
    if should_hold_for_farewell(posted):
        log.warning("[slack] farewell retryable team=%s channel=%s error=%s", team_id, channel_id, posted.error)
        return "hold"
    log.warning("[slack] farewell skipped team=%s channel=%s error=%s", team_id, channel_id, posted.error)
    return "skipped"
