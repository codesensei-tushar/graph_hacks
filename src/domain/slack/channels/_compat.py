"""Small adapters shared by the channel modules.

The Slack client returns results either as dicts or dataclasses with snake_case
fields; company-brain's TS uses camelCase. ``field`` reads any of those shapes so
the ported logic can stay line-for-line.
"""

import dataclasses
import re
from typing import Any


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def field(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    for key in (name, _camel(name)):
        if isinstance(obj, dict):
            if key in obj and obj[key] is not None:
                return obj[key]
        elif hasattr(obj, key) and getattr(obj, key) is not None:
            return getattr(obj, key)
    return default


def as_dict(obj: Any) -> dict:
    """A Slack message as a plain dict with Slack's own (snake_case) keys."""
    if isinstance(obj, dict):
        return obj
    if dataclasses.is_dataclass(obj):
        return {key: value for key, value in dataclasses.asdict(obj).items() if value is not None}
    return {key: value for key, value in vars(obj).items() if value is not None}


def _trimmed(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _valid_http_url(value: Any) -> str:
    text = _trimmed(value)
    return text if re.match(r"^https?://", text, re.IGNORECASE) else ""


def _exact_slack_http_link(value: str) -> bool:
    return re.fullmatch(r"<https?://[^>]+>", value or "") is not None


def normalize_slack_message_content(message: Any) -> str:
    """Port of company-brain ``normalizeSlackMessageContent`` (``brain/slack/attachments.ts``).

    Prefers the integrations module when it exists so there is one implementation.
    """
    try:
        from src.integrations.slack.attachments import normalize_slack_message_content as shared

        return shared(message)
    except ImportError:
        pass
    msg = as_dict(message)
    segments: list[str] = []
    text = _trimmed(msg.get("text"))
    if text:
        segments.append(text)
    for attachment in msg.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        title = _trimmed(attachment.get("title"))
        title_link = _valid_http_url(attachment.get("title_link"))
        pretext = _trimmed(attachment.get("pretext"))
        body = _trimmed(attachment.get("text"))
        footer = _trimmed(attachment.get("footer"))
        if pretext:
            segments.append(pretext)
        if title:
            segments.append(title)
        # Exact whole-string Slack HTTP(S) titles already carry their URL.
        if title_link and not _exact_slack_http_link(title):
            segments.append(f"<{title_link}>")
        if body:
            segments.append(body)
        if footer:
            segments.append(footer)
    return "\n".join(segments)


def escape_slack_text(value: str) -> str:
    """Port of company-brain ``escapeSlackText`` (``brain/slack/workspace.ts``)."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_user_display_name(user: Any) -> str | None:
    """Port of company-brain ``slackUserDisplayName``: display name, then name, then handle."""
    # The client maps company-brain's `name` to `real_name`; `name` there is the login handle.
    for key in ("display_name", "real_name", "name", "handle"):
        value = _trimmed(field(user, key))
        if value:
            return value
    return None
