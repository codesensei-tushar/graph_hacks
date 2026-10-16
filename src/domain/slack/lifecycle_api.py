"""Slack Web API calls the install and member lifecycle needs.

Port of the matching functions in company-brain `brain/slack/client.ts`
(postSlackMessageIdempotent, inviteSlackUserToChannel, lookupSlackUserInfo,
listSlackUsersPage, getSlackTeamDirectory, getBotConversations, uninstallSlackApp,
exchangeSlackOAuth) and the raw `slackCall` helper in `bootstrap.ts`.

They return Slack's verdicts (error codes, Retry-After) instead of swallowing them,
because the lifecycle code branches on them. They take a slack_sdk ``WebClient``
(``SlackClient.web``) so they work whatever else the shared client grows.
"""

import logging
import time
import uuid
from dataclasses import dataclass, field

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from src.core.config import settings

log = logging.getLogger(__name__)

# Dead beyond recovery, so skip the auth.revoke retry. `invalid_auth` can mean IP restrictions.
SLACK_DEAD_TOKEN_ERRORS = frozenset({"token_revoked", "token_expired", "account_inactive"})
SLACK_RETRYABLE_ERRORS = frozenset({"ratelimited", "internal_error", "service_unavailable", "fatal_error", "request_timeout"})


def slack_call(web: WebClient, method: str, **body) -> dict:
    """POST one Web API method. Always returns a dict with ``ok``; never raises."""
    try:
        response = web.api_call(method, json=body)
        data = dict(response.data) if isinstance(response.data, dict) else {"ok": False}
        return data
    except SlackApiError as error:
        data = dict(error.response.data) if isinstance(error.response.data, dict) else {}
        data["ok"] = False
        data["http_status"] = error.response.status_code
        data.setdefault("error", f"http_{error.response.status_code}")
        retry_after = _retry_after(error.response.headers)
        if retry_after:
            data["retry_after_seconds"] = retry_after
        return data
    except Exception as error:  # network: Slack never answered
        return {"ok": False, "error": str(error) or type(error).__name__, "network_error": True}


def _no_verdict(data: dict) -> bool:
    """A non-2xx response or a network error: Slack never gave a verdict."""
    return bool(data.get("network_error")) or (data.get("http_status") or 200) >= 300


def _retry_after(headers) -> int | None:
    try:
        value = (headers or {}).get("Retry-After") or (headers or {}).get("retry-after")
        if isinstance(value, list):
            value = value[0]
        seconds = int(value) if value is not None else 0
        return seconds if seconds > 0 else None
    except (TypeError, ValueError):
        return None


@dataclass
class SlackPostResult:
    ok: bool
    ts: str | None = None
    deduped: bool = False
    error: str | None = None
    retry_after_seconds: int | None = None


def post_slack_message_idempotent(
    web: WebClient, channel: str, text: str, client_message_id: str, blocks: list | None = None
) -> SlackPostResult:
    """A stable client_msg_id cannot double-post; Slack answers a repeat with ok and the original ts."""
    sent_at = time.time()
    body: dict = {
        "channel": channel,
        "text": text,
        "client_msg_id": client_message_id,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if blocks:
        body["blocks"] = blocks
    data = slack_call(web, "chat.postMessage", **body)
    if data.get("ok") is True and data.get("ts"):
        deduped = sent_at - float(data["ts"]) > 60
        return SlackPostResult(ok=True, ts=data["ts"], deduped=deduped)
    return SlackPostResult(ok=False, error=data.get("error") or "unknown", retry_after_seconds=data.get("retry_after_seconds"))


@dataclass
class SlackInviteResult:
    ok: bool
    error: str | None = None
    retry_after_seconds: int | None = None


def invite_slack_user_to_channel(web: WebClient, channel: str, slack_user_id: str) -> SlackInviteResult:
    data = slack_call(web, "conversations.invite", channel=channel, users=slack_user_id)
    if data.get("ok") is True or data.get("error") in ("already_in_channel", "cant_invite_self"):
        return SlackInviteResult(ok=True)
    return SlackInviteResult(ok=False, error=data.get("error") or "unknown", retry_after_seconds=data.get("retry_after_seconds"))


@dataclass
class SlackMember:
    """Port of company-brain `SlackMember` / `SlackUserInfo` (users.list / users.info shape)."""

    id: str
    name: str | None = None
    handle: str | None = None
    display_name: str | None = None
    email: str | None = None
    is_bot: bool = False
    is_restricted: bool = False
    is_ultra_restricted: bool = False
    is_stranger: bool = False
    team_id: str | None = None
    deleted: bool = False
    tz_offset: int | None = None


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
        is_stranger=m.get("is_stranger") is True,
        team_id=m.get("team_id") or None,
        tz_offset=m.get("tz_offset"),
    )


@dataclass
class SlackUserInfoLookup:
    ok: bool
    user: SlackMember | None = None
    # missing_user_id | user_not_found | profile_unavailable | slack_api_error
    reason: str | None = None
    error: str | None = None
    retry_after_seconds: int | None = None


def lookup_slack_user_info(web: WebClient, user_id: str) -> SlackUserInfoLookup:
    if not (user_id or "").strip():
        return SlackUserInfoLookup(ok=False, reason="missing_user_id")
    data = slack_call(web, "users.info", user=user_id)
    if data.get("ok") is not True:
        error = data.get("error") or "unknown"
        if error in ("user_not_found", "users_not_found"):
            return SlackUserInfoLookup(ok=False, reason="user_not_found", error=error)
        return SlackUserInfoLookup(
            ok=False, reason="slack_api_error", error=error, retry_after_seconds=data.get("retry_after_seconds")
        )
    raw = data.get("user") or {}
    if raw.get("deleted"):
        return SlackUserInfoLookup(ok=False, reason="profile_unavailable", error="deleted")
    member = map_slack_member(raw)
    if member is None:
        return SlackUserInfoLookup(ok=False, reason="profile_unavailable")
    return SlackUserInfoLookup(ok=True, user=member)


@dataclass
class SlackUsersPage:
    ok: bool
    items: list[SlackMember] = field(default_factory=list)
    next_cursor: str | None = None
    complete: bool = False
    error: str | None = None
    retry_after_seconds: int | None = None


def list_slack_users_page(web: WebClient, cursor: str | None = None, limit: int = 200) -> SlackUsersPage:
    body: dict = {"limit": limit}
    if cursor:
        body["cursor"] = cursor
    data = slack_call(web, "users.list", **body)
    if data.get("ok") is not True:
        return SlackUsersPage(ok=False, error=data.get("error") or "unknown", retry_after_seconds=data.get("retry_after_seconds"))
    items = [m for m in (map_slack_member(raw) for raw in data.get("members") or []) if m is not None]
    next_cursor = ((data.get("response_metadata") or {}).get("next_cursor") or "").strip() or None
    return SlackUsersPage(ok=True, items=items, next_cursor=next_cursor, complete=next_cursor is None)


def get_slack_team_directory(web: WebClient, max_members: int = 5000) -> list[SlackMember]:
    out: list[SlackMember] = []
    cursor = None
    while len(out) < max_members:
        page = list_slack_users_page(web, cursor)
        if not page.ok:
            log.warning("[slack] users.list failed: %s", page.error)
            break
        out.extend(page.items[: max_members - len(out)])
        if page.complete:
            break
        cursor = page.next_cursor
    return out


@dataclass
class SlackConversation:
    id: str
    name: str
    is_private: bool = False


class SlackLookupIncomplete(RuntimeError):
    pass


def get_bot_conversations(web: WebClient, max_channels: int = 1000, require_complete: bool = False) -> list[SlackConversation]:
    out: list[SlackConversation] = []
    cursor = None
    while len(out) < max_channels:
        body: dict = {"types": "public_channel,private_channel", "exclude_archived": True, "limit": 200}
        if cursor:
            body["cursor"] = cursor
        data = slack_call(web, "users.conversations", **body)
        if data.get("ok") is not True:
            if require_complete:
                raise SlackLookupIncomplete(data.get("error") or "unknown")
            log.warning("[slack] users.conversations failed: %s", data.get("error"))
            break
        for channel in data.get("channels") or []:
            if channel.get("id") and channel.get("name"):
                out.append(SlackConversation(id=channel["id"], name=channel["name"], is_private=bool(channel.get("is_private"))))
            if len(out) >= max_channels:
                break
        cursor = ((data.get("response_metadata") or {}).get("next_cursor") or "").strip() or None
        if not cursor:
            break
    if require_complete and (cursor or len(out) >= max_channels):
        raise SlackLookupIncomplete("Slack channel lookup incomplete")
    return out


def open_slack_conversation(web: WebClient, slack_user_id: str) -> str | None:
    data = slack_call(web, "conversations.open", users=slack_user_id)
    return ((data.get("channel") or {}).get("id")) if data.get("ok") is True else None


def post_slack_message(web: WebClient, channel: str, text: str, thread_ts: str | None = None, blocks: list | None = None) -> str | None:
    body: dict = {"channel": channel, "text": text, "unfurl_links": False, "unfurl_media": False}
    if thread_ts:
        body["thread_ts"] = thread_ts
    if blocks:
        body["blocks"] = blocks
    data = slack_call(web, "chat.postMessage", **body)
    if data.get("ok") is not True:
        log.warning("[slack] chat.postMessage failed: %s channel=%s", data.get("error"), channel)
        return None
    return data.get("ts")


def post_slack_ephemeral(web: WebClient, channel: str, user: str, text: str, thread_ts: str | None = None, blocks: list | None = None) -> bool:
    body: dict = {"channel": channel, "user": user, "text": text}
    if thread_ts:
        body["thread_ts"] = thread_ts
    if blocks:
        body["blocks"] = blocks
    return slack_call(web, "chat.postEphemeral", **body).get("ok") is True


def update_slack_message(web: WebClient, channel: str, ts: str, text: str, blocks: list | None = None) -> bool:
    body: dict = {"channel": channel, "ts": ts, "text": text}
    if blocks is not None:
        body["blocks"] = blocks
    return slack_call(web, "chat.update", **body).get("ok") is True


def lookup_slack_user_by_email(web: WebClient, email: str) -> str | None:
    data = slack_call(web, "users.lookupByEmail", email=email)
    return (data.get("user") or {}).get("id") if data.get("ok") is True else None


# --- install lifecycle --------------------------------------------------------------


@dataclass
class SlackOAuthResult:
    team_id: str
    bot_token: str
    team_name: str | None = None
    bot_user_id: str | None = None
    app_id: str | None = None
    scopes: str | None = None
    authed_user_id: str | None = None


def exchange_slack_oauth(code: str, redirect_uri: str, web: WebClient | None = None) -> SlackOAuthResult:
    if not (settings.slack_client_id and settings.slack_client_secret):
        raise RuntimeError("Slack is not configured: set SLACK_CLIENT_ID and SLACK_CLIENT_SECRET")
    data = slack_call(
        web or WebClient(base_url=settings.slack_api_base_url),
        "oauth.v2.access",
        client_id=settings.slack_client_id,
        client_secret=settings.slack_client_secret,
        code=code,
        redirect_uri=redirect_uri,
    )
    team = data.get("team") or {}
    if data.get("ok") is not True or not data.get("access_token") or not team.get("id"):
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


def uninstall_slack_app(web: WebClient) -> str:
    """``revoked`` | ``terminal`` | ``transient``.

    `transient` means Slack never gave a verdict, so the caller must keep the token
    and retry; `terminal` means the install is beyond our reach either way.
    """
    data = slack_call(web, "apps.uninstall", client_id=settings.slack_client_id, client_secret=settings.slack_client_secret)
    if data.get("ok") is True:
        return "revoked"
    error = data.get("error") or "unknown"
    log.warning("[slack] apps.uninstall failed: %s", error)
    if _no_verdict(data):
        return "transient"
    if error in SLACK_DEAD_TOKEN_ERRORS:
        return "terminal"
    if error in SLACK_RETRYABLE_ERRORS:
        return "transient"
    return _revoke_slack_token(web)


def _revoke_slack_token(web: WebClient) -> str:
    data = slack_call(web, "auth.revoke")
    if data.get("ok") is True:
        return "revoked"
    error = data.get("error") or "unknown"
    log.warning("[slack] auth.revoke failed: %s", error)
    if _no_verdict(data):
        return "transient"
    return "transient" if error in SLACK_RETRYABLE_ERRORS else "terminal"


def deterministic_uuid(seed: str) -> str:
    """Port of farewell.ts `deterministicUuid`: SHA-256 → RFC 4122 v4-shaped UUID."""
    import hashlib

    digest = bytearray(hashlib.sha256(seed.encode()).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))
