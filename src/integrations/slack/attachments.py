"""Port of company-brain `brain/slack/attachments.ts`.

Message text normalization (legacy attachments folded in), the files a turn can
see, and downloads of model-readable files (images, PDFs). The AI-SDK content parts
become plain dicts for the model seam.
"""

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from src.integrations.slack.client import SlackClient, SlackThreadMessage

log = logging.getLogger(__name__)

MAX_THREAD_ATTACHMENTS = 5
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})


@dataclass(frozen=True)
class SlackFileRef:
    id: str
    name: str
    mimetype: str | None = None
    size: int | None = None
    url_private: str | None = None
    url_private_download: str | None = None


def _trimmed_string(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _valid_http_url(value) -> str | None:
    trimmed = _trimmed_string(value)
    if not trimmed:
        return None
    for char in trimmed:
        code = ord(char)
        if code <= 31 or code == 127 or char in "<>|":
            return None
    try:
        parsed = urlparse(trimmed)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return trimmed


def _exact_slack_http_link(value: str) -> bool:
    match = re.fullmatch(r"<([^|>]+)(?:\|[^>]*)?>", value)
    return bool(match and match.group(1) and _valid_http_url(match.group(1)))


def normalize_slack_message_content(message: SlackThreadMessage) -> str:
    segments: list[str] = []
    text = _trimmed_string(message.get("text"))
    if text:
        segments.append(text)
    attachments = message.get("attachments")
    if isinstance(attachments, list):
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            title = _trimmed_string(attachment.get("title"))
            title_link = _valid_http_url(attachment.get("title_link"))
            pretext = _trimmed_string(attachment.get("pretext"))
            body = _trimmed_string(attachment.get("text"))
            footer = _trimmed_string(attachment.get("footer"))
            if pretext:
                segments.append(pretext)
            if title:
                segments.append(title)
            # Exact whole-string Slack HTTP(S) titles already carry their URL;
            # only then ignore title_link (including when the two conflict).
            if title_link and not _exact_slack_http_link(title):
                segments.append(f"<{title_link}>")
            if body:
                segments.append(body)
            if footer:
                segments.append(footer)
    return "\n".join(segments)


def slack_files_from_message(message: SlackThreadMessage) -> list[SlackFileRef]:
    files = message.get("files")
    if not isinstance(files, list):
        return []
    out = []
    for file in files:
        if not isinstance(file, dict) or not file.get("id") or not file.get("name"):
            continue
        out.append(
            SlackFileRef(
                id=file["id"],
                name=file["name"],
                mimetype=file.get("mimetype"),
                size=file.get("size"),
                url_private=file.get("url_private"),
                url_private_download=file.get("url_private_download"),
            )
        )
    return out


def collect_thread_files(messages: list[SlackThreadMessage]) -> list[SlackFileRef]:
    seen: set[str] = set()
    out: list[SlackFileRef] = []
    for message in messages:
        for file in slack_files_from_message(message):
            if file.id in seen:
                continue
            seen.add(file.id)
            out.append(file)
    return out[-MAX_THREAD_ATTACHMENTS:]


def collect_turn_files(messages: list[SlackThreadMessage], trigger: SlackThreadMessage | None = None) -> list[SlackFileRef]:
    """Merge thread history with the triggering Slack event (top-level @mentions)."""
    if trigger is None:
        return collect_thread_files(messages)
    return collect_thread_files([*messages, trigger])


def is_model_readable_slack_file(file: SlackFileRef) -> bool:
    media_type = (file.mimetype or "").lower()
    if not media_type:
        return False
    return media_type in IMAGE_MEDIA_TYPES or media_type == "application/pdf"


def format_message_attachment_hint(files: list[SlackFileRef] | None) -> str:
    if not files:
        return ""
    labels = []
    for file in files:
        kind = file.mimetype or "unknown"
        readable = "" if is_model_readable_slack_file(file) else ", not sent to model"
        labels.append(f"{file.name} ({kind}{readable})")
    return f"[attachments: {'; '.join(labels)}]"


def _resolve_slack_file_download_url(client: SlackClient, file: SlackFileRef) -> str | None:
    direct = file.url_private_download or file.url_private
    if direct:
        return direct
    if not file.id:
        return None
    info = client.get_slack_file_info(file.id)
    if not info:
        return None
    return info.get("url_private_download") or info.get("url_private")


def download_slack_file(client: SlackClient, file: SlackFileRef) -> bytes | None:
    url = _resolve_slack_file_download_url(client, file)
    if not url:
        return None
    if isinstance(file.size, int) and file.size > MAX_ATTACHMENT_BYTES:
        log.warning("[slack] skip attachment %s: size %s exceeds limit", file.name, file.size)
        return None
    data = client.download_private_url(url, MAX_ATTACHMENT_BYTES)
    if data is None:
        return None
    if len(data) > MAX_ATTACHMENT_BYTES:
        log.warning("[slack] skip attachment %s: downloaded %s bytes exceeds limit", file.name, len(data))
        return None
    return data


def load_thread_attachment_parts(client: SlackClient, files: list[SlackFileRef]) -> list[dict]:
    """Image and PDF parts for the model: ``{"type": "image"|"file", "data", "media_type", "filename"?}``."""
    parts: list[dict] = []
    for file in files:
        if not is_model_readable_slack_file(file):
            continue
        data = download_slack_file(client, file)
        if not data:
            continue
        media_type = (file.mimetype or "").lower()
        if media_type in IMAGE_MEDIA_TYPES:
            parts.append({"type": "image", "data": data, "media_type": media_type})
        elif media_type == "application/pdf":
            parts.append({"type": "file", "data": data, "media_type": "application/pdf", "filename": file.name})
    return parts


def build_turn_user_content(text_prompt: str, attachment_parts: list[dict]) -> str | list[dict]:
    if not attachment_parts:
        return text_prompt
    text = (
        f"{text_prompt}\n\n<slack_attachments>\n{len(attachment_parts)} image/PDF file(s) from this Slack thread are "
        "attached below for direct inspection. Use their content when answering — especially screenshots shared "
        "earlier in the thread.\n</slack_attachments>"
    )
    return [{"type": "text", "text": text}, *attachment_parts]
