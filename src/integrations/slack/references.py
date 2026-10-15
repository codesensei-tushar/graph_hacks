"""Port of company-brain `brain/slack/references.ts`.

Turns model-friendly channel references (``#eng``, ``C0123ABCD``) into Markdown links
to the Slack channel, leaving code, links, reference definitions and Slack control
tokens untouched.
"""

import logging
import re
from collections.abc import Callable
from typing import Protocol
from urllib.parse import quote

log = logging.getLogger(__name__)

_OPEN = r"(^|[\s(\[{'\"\u201c\u2018>])"
_CLOSE = r"(?=$|[\s)\]},.!?:;'\"\u201d\u2019])"
CHANNEL_REFERENCE_RE = re.compile(
    _OPEN + r"(?:#?([CG][A-Z0-9]{8,})|#([A-Za-z0-9][A-Za-z0-9_-]*))" + _CLOSE
)
CHANNEL_REFERENCE_CANDIDATE_RE = re.compile(
    _OPEN + r"(?:#?[CG][A-Z0-9]{8,}|#[A-Za-z0-9][A-Za-z0-9_-]*)" + _CLOSE
)
SLACK_ANGLE_TOKEN_RE = re.compile(
    r"^<(?:#[CG][A-Z0-9]+(?:\|[^<>\r\n]*)?|@[A-Z0-9]+(?:\|[^<>\r\n]*)?|![^<>\r\n]+|(?:https?://|mailto:)[^<>\r\n]+)>",
    re.IGNORECASE,
)


class _Channel(Protocol):
    id: str
    name: str


ChannelDirectory = list[_Channel]


def _at(text: str, index: int) -> str:
    """JS ``text[i]``: empty instead of IndexError past either end."""
    return text[index] if 0 <= index < len(text) else ""


def _backtick_code_end(text: str, start: int) -> int | None:
    delimiter_end = start
    while _at(text, delimiter_end) == "`":
        delimiter_end += 1
    delimiter_length = delimiter_end - start
    cursor = delimiter_end
    while cursor < len(text):
        nxt = text.find("`", cursor)
        if nxt == -1:
            return None
        run_end = nxt
        while _at(text, run_end) == "`":
            run_end += 1
        if run_end - nxt == delimiter_length:
            return run_end
        cursor = run_end
    return None


def _bracket_end(text: str, start: int) -> int | None:
    depth = 1
    cursor = start + 1
    while cursor < len(text) and depth > 0:
        if text[cursor] == "\\":
            cursor += 2
            continue
        if text[cursor] == "`":
            code_end = _backtick_code_end(text, cursor)
            if code_end is not None:
                cursor = code_end
                continue
        if text[cursor] == "[":
            depth += 1
        if text[cursor] == "]":
            depth -= 1
        cursor += 1
    return cursor - 1 if depth == 0 else None


def _normalize_reference_label(label: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\\([\[\]\\])", r"\1", label).strip()).lower()


def _reference_labels(text: str) -> set[str]:
    labels: set[str] = set()
    line_start = 0
    while line_start < len(text):
        line_end = text.find("\n", line_start)
        end = len(text) if line_end == -1 else line_end
        line = text[line_start:end]
        opening = re.match(r"^[ \t]{0,3}\[", line)
        if opening:
            bracket_start = line_start + len(opening.group(0)) - 1
            close = _bracket_end(text, bracket_start)
            if close is not None and close < end and _at(text, close + 1) == ":":
                labels.add(_normalize_reference_label(text[bracket_start + 1 : close]))
        if line_end == -1:
            break
        line_start = line_end + 1
    return labels


def _skip_link_whitespace(text: str, start: int) -> int:
    cursor = start
    newlines = 0
    while cursor < len(text) and text[cursor] in " \t\r\n":
        if text[cursor] == "\n":
            newlines += 1
            if newlines > 1:
                break
        cursor += 1
    return cursor


def _link_title_end(text: str, start: int) -> int | None:
    opener = _at(text, start)
    if opener not in ('"', "'", "("):
        return None
    closer = ")" if opener == "(" else opener
    cursor = start + 1
    while cursor < len(text):
        if text[cursor] == "\\":
            cursor += 2
            continue
        if text[cursor] == "\n" and _at(text, cursor + 1) == "\n":
            return None
        if text[cursor] == closer:
            return cursor + 1
        cursor += 1
    return None


def _inline_link_end(text: str, label_end: int) -> int | None:
    if _at(text, label_end + 1) != "(":
        return None
    cursor = _skip_link_whitespace(text, label_end + 2)
    if _at(text, cursor) == "<":
        cursor += 1
        while cursor < len(text):
            if text[cursor] == "\\":
                cursor += 2
                continue
            if text[cursor] in ("\n", "<"):
                return None
            if text[cursor] == ">":
                cursor += 1
                break
            cursor += 1
        if _at(text, cursor - 1) != ">":
            return None
    else:
        depth = 0
        while cursor < len(text):
            if text[cursor] == "\\":
                cursor += 2
                continue
            if text[cursor] in " \t\r\n":
                break
            if text[cursor] == "<":
                return None
            if text[cursor] == "(":
                depth += 1
            if text[cursor] == ")":
                if depth == 0:
                    return cursor + 1
                depth -= 1
            cursor += 1
        if depth != 0:
            return None

    title_start = _skip_link_whitespace(text, cursor)
    if _at(text, title_start) == ")":
        return title_start + 1
    if title_start == cursor:
        return None
    title_end = _link_title_end(text, title_start)
    if title_end is None:
        return None
    close = _skip_link_whitespace(text, title_end)
    return close + 1 if _at(text, close) == ")" else None


def _markdown_link_end(text: str, start: int, definitions: set[str]) -> int | None:
    label_end = _bracket_end(text, start)
    if label_end is None:
        return None
    inline_end = _inline_link_end(text, label_end)
    if inline_end is not None:
        return inline_end

    label = text[start + 1 : label_end]
    if _at(text, label_end + 1) == "[":
        reference_end = _bracket_end(text, label_end + 1)
        if reference_end is None:
            return None
        reference = text[label_end + 2 : reference_end]
        key = _normalize_reference_label(reference or label)
        return reference_end + 1 if key in definitions else None
    return label_end + 1 if _normalize_reference_label(label) in definitions else None


def _reference_definition_end(text: str, start: int, definitions: set[str]) -> int | None:
    line_start = text.rfind("\n", 0, start) + 1 if start > 0 else 0
    if not re.fullmatch(r"[ \t]{0,3}", text[line_start:start]):
        return None
    label_end = _bracket_end(text, start)
    if label_end is None or _at(text, label_end + 1) != ":":
        return None
    label = _normalize_reference_label(text[start + 1 : label_end])
    if label not in definitions:
        return None
    line_end = text.find("\n", label_end + 2)
    if line_end == -1:
        return len(text)

    destination_end = label_end + 2
    while _at(text, destination_end) in (" ", "\t") and _at(text, destination_end):
        destination_end += 1
    if _at(text, destination_end) == "<":
        close = text.find(">", destination_end + 1)
        if close == -1 or close > line_end:
            return line_end + 1
        destination_end = close + 1
    else:
        while destination_end < line_end and _at(text, destination_end) not in (" ", "\t", "\r"):
            destination_end += 1
    if text[destination_end:line_end].strip():
        return line_end + 1

    continuation_end = text.find("\n", line_end + 1)
    next_line_end = len(text) if continuation_end == -1 else continuation_end
    continuation = text[line_end + 1 : next_line_end]
    indentation = re.match(r"^[ \t]{1,3}", continuation)
    if not indentation:
        return line_end + 1
    title_start = line_end + 1 + len(indentation.group(0))
    title_end = _link_title_end(text, title_start)
    if title_end is None or title_end > next_line_end or text[title_end:next_line_end].strip():
        return line_end + 1
    return len(text) if continuation_end == -1 else continuation_end + 1


def _fenced_code_end(text: str, start: int) -> int | None:
    marker = _at(text, start)
    if marker not in ("`", "~"):
        return None
    line_start = text.rfind("\n", 0, start) + 1 if start > 0 else 0
    if not re.fullmatch(r"[ \t]{0,3}", text[line_start:start]):
        return None
    marker_end = start
    while _at(text, marker_end) == marker:
        marker_end += 1
    marker_length = marker_end - start
    if marker_length < 3:
        return None
    opening_line_end = text.find("\n", marker_end)
    opening_end = len(text) if opening_line_end == -1 else opening_line_end
    if marker == "`" and "`" in text[marker_end:opening_end]:
        return None

    next_line = len(text) if opening_line_end == -1 else opening_line_end + 1
    while next_line < len(text):
        line_end = text.find("\n", next_line)
        end = len(text) if line_end == -1 else line_end
        cursor = next_line
        while cursor < end and cursor - next_line < 3 and _at(text, cursor) in (" ", "\t"):
            cursor += 1
        close_end = cursor
        while _at(text, close_end) == marker:
            close_end += 1
        if close_end - cursor >= marker_length and re.fullmatch(r"[ \t\r]*", text[close_end:end]):
            return len(text) if line_end == -1 else line_end + 1
        if line_end == -1:
            break
        next_line = line_end + 1
    return len(text)


def _outside_protected_ranges(text: str, transform: Callable[[str], str]) -> str:
    definitions = _reference_labels(text)
    output = ""
    segment_start = 0
    cursor = 0

    while cursor < len(text):
        protected_end = _fenced_code_end(text, cursor)
        if protected_end is None and text[cursor] == "[":
            protected_end = _reference_definition_end(text, cursor, definitions)
            if protected_end is None:
                protected_end = _markdown_link_end(text, cursor, definitions)
        elif protected_end is None and text[cursor] == "<":
            token = SLACK_ANGLE_TOKEN_RE.match(text[cursor:])
            if token:
                protected_end = cursor + len(token.group(0))
        if protected_end is not None:
            output += transform(text[segment_start:cursor])
            output += text[cursor:protected_end]
            cursor = protected_end
            segment_start = protected_end
            continue

        if text[cursor] != "`":
            cursor += 1
            continue
        code_end = _backtick_code_end(text, cursor)
        output += transform(text[segment_start:cursor])
        if code_end is None:
            return output + text[cursor:]
        output += text[cursor:code_end]
        cursor = code_end
        segment_start = code_end
    return output + transform(text[segment_start:])


def format_slack_channel_references(text: str, channels: ChannelDirectory, team_id: str) -> str:
    """Convert model-friendly Slack channel references into standard Markdown links.

    Existing Slack control tokens, person names/mentions, URLs, and code stay
    untouched. Unknown channel names remain readable, while unknown raw IDs are
    made generic rather than being exposed to a person.
    """
    normalized_team_id = team_id.strip() if isinstance(team_id, str) else ""
    if not text.strip() or not normalized_team_id:
        return text
    by_name = {channel.name.lower(): channel for channel in channels}
    by_id = {channel.id.upper(): channel for channel in channels}
    encoded_team_id = quote(normalized_team_id, safe="")

    def replace(match: re.Match) -> str:
        full, prefix, raw_id, raw_name = match.group(0), match.group(1), match.group(2), match.group(3)
        if raw_id:
            channel = by_id.get(raw_id.upper())
            if channel is None and full[len(prefix) :].startswith("#"):
                channel = by_name.get(raw_id.lower())
        else:
            channel = by_name.get((raw_name or "").lower())
        if channel is None:
            return f"{prefix}that channel" if raw_id else full
        url = f"https://app.slack.com/client/{encoded_team_id}/{quote(channel.id, safe='')}"
        return f"{prefix}[#{channel.name}]({url})"

    return _outside_protected_ranges(text, lambda segment: CHANNEL_REFERENCE_RE.sub(replace, segment))


SlackReplyReferenceResolver = Callable[[str], str]


def create_slack_reply_reference_resolver(
    team_id: str,
    load_directory: Callable[[], ChannelDirectory],
) -> SlackReplyReferenceResolver:
    """Lazily loads the bot-visible channel directory once per delivery session.

    ``load_directory`` is the channel directory lookup (company-brain
    ``getChannelDirectory(env, teamId, botToken)``).
    """
    cache: dict[str, ChannelDirectory] = {}

    def resolve(reply: str) -> str:
        team = team_id.strip() if isinstance(team_id, str) else ""
        if not team or not reply.strip() or not CHANNEL_REFERENCE_CANDIDATE_RE.search(reply):
            return reply
        try:
            if "directory" not in cache:
                cache["directory"] = load_directory()
            return format_slack_channel_references(reply, cache["directory"], team)
        except Exception as error:
            log.warning("[company-brain] Slack channel reference lookup failed: %s", error)
            return format_slack_channel_references(reply, [], team)

    return resolve
