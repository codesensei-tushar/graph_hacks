"""Port of company-brain `brain/slack/format.ts`.

Markdown to Slack: sentence-aligned splitting for markdown blocks and stream
chunks, the legacy mrkdwn subset for section blocks and fallback text, and the
plain-text notification fallback.
"""

import re

# Max chars for markdown block / markdown_text stream chunk (per payload).
SLACK_MARKDOWN_LIMIT = 12_000

# Max chars for mrkdwn inside a section block.
SLACK_MRKDWN_SECTION_LIMIT = 3_000

_FENCE_RE = re.compile(r"(```[\s\S]*?```)")
SLACK_CONTINUATION_SENTENCE = "_Continued in the next section._"


def _normalize_segment(text: str, collapse_spaces: bool, dash: str) -> str:
    out = re.sub(r"(\d)\s*[—–]\s*(\d)", r"\1-\2", text)
    out = re.sub(r"\s*—\s*", lambda _m: dash, out)
    out = re.sub(r"\s*–\s*", lambda _m: dash, out)
    if collapse_spaces:
        out = re.sub(r" {2,}", " ", out)
    return out


def normalize_text_preserving_code_fences(text: str, dash: str = " - ") -> str:
    """Normalize em/en dashes outside fenced code.

    ``dash`` is the replacement: Slack rendering uses " - ", prose uses ", ".
    Numeric ranges keep a hyphen.
    """
    parts = _FENCE_RE.split(text)
    return "".join(_normalize_segment(part, i % 2 == 0, dash) for i, part in enumerate(parts))


def _fence_at(text: str, index: int) -> bool:
    fences = 0
    cursor = text.find("```")
    while 0 <= cursor < index:
        fences += 1
        cursor = text.find("```", cursor + 3)
    return fences % 2 == 1


_SENTENCE_END = re.compile(r"[.!?](?:[\"'’”\])}*_`]+)?(?=\s|\Z)")


def _last_complete_sentence_boundary(text: str) -> int:
    boundary = 0
    for match in _SENTENCE_END.finditer(text):
        end = match.end()
        if _fence_at(text, end):
            continue
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", end)
        line = text[line_start : line_end if line_end >= 0 else len(text)]
        # A period inside a table cell is not a safe row boundary.
        if line.lstrip().startswith("|"):
            continue
        boundary = end
    return boundary


def _open_fence_language(text: str) -> str | None:
    matches = re.findall(r"```([^\n]*)", text)
    if len(matches) % 2 == 0:
        return None
    return (matches[-1] or "").strip()


def split_slack_markdown(text: str, max_chars: int = SLACK_MARKDOWN_LIMIT) -> list[str]:
    """Split long Slack markdown without making normal prose look abruptly cut.

    Complete sentences are preferred. A pathological single sentence, table, or
    code block gets an explicit continuation sentence; fenced code is closed and
    reopened so every individual Slack block remains renderable.
    """
    if max_chars <= len(SLACK_CONTINUATION_SENTENCE) + 16:
        raise ValueError("Slack markdown chunks need room for continuation text")
    remaining = normalize_text_preserving_code_fences(text).strip()
    if not remaining:
        return []
    chunks: list[str] = []

    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        sentence_boundary = _last_complete_sentence_boundary(window)
        if sentence_boundary > 0:
            chunks.append(remaining[:sentence_boundary].rstrip())
            remaining = remaining[sentence_boundary:].lstrip()
            continue

        marker = f"\n\n{SLACK_CONTINUATION_SENTENCE}"
        provisional_limit = max_chars - len(marker) - 5
        provisional = remaining[:provisional_limit]
        line_boundary = provisional.rfind("\n")
        word_boundary = provisional.rfind(" ")
        # Prefer a reasonably full line boundary for tables and code. Falling back
        # to the latest word keeps pathological single lines within Slack's limit.
        boundary = line_boundary if line_boundary >= provisional_limit / 2 else word_boundary
        if boundary <= 0:
            boundary = provisional_limit
        body = remaining[:boundary].rstrip()
        nxt = remaining[boundary:].lstrip()
        language = _open_fence_language(body)
        if language is not None:
            body = f"{body}\n```"
            nxt = f"```{language}\n{nxt}"
        chunks.append(f"{body}{marker}")
        remaining = nxt

    if remaining:
        chunks.append(remaining)
    return chunks


def _inline(segment: str) -> str:
    segment = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"<\2|\1>", segment)
    segment = re.sub(r"\*\*([^*\n]+)\*\*", r"*\1*", segment)
    return re.sub(r"__([^_\n]+)__", r"*\1*", segment)


def to_slack_mrkdwn(text: str) -> str:
    """Legacy mrkdwn for section/context blocks and plain ``text`` on chat.postMessage.

    Subset only: *bold*, _italic_, `code`, <url|label>, ``-`` bullets.
    No tables, headers, or horizontal rules.
    """
    normalized = normalize_text_preserving_code_fences(text)
    out: list[str] = []
    for line in normalized.split("\n"):
        if re.match(r"^\s*([-*_])\1{2,}\s*$", line):
            continue
        header = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if header and header.group(1):
            out.append(f"*{_inline(header.group(1))}*")
            continue
        if re.match(r"^\s*\|.*\|\s*$", line):
            if re.match(r"^\s*\|?[\s:|-]+\|?\s*$", line) and "-" in line:
                continue
            cells = [_inline(cell.strip()) for cell in re.sub(r"^\||\|$", "", line.strip()).split("|")]
            cells = [cell for cell in cells if cell]
            if cells:
                out.append("  ·  ".join(cells))
            continue
        out.append(_inline(line))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


def plain_text_fallback(text: str) -> str:
    """Strip markdown-ish syntax for notification fallback ``text``."""
    out = normalize_text_preserving_code_fences(text)
    out = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"\1 (\2)", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"\1", out)
    out = re.sub(r"\*([^*]+)\*", r"\1", out)
    out = re.sub(r"_([^_]+)_", r"\1", out)
    out = re.sub(r"`([^`]+)`", r"\1", out)
    out = re.sub(r"^#{1,6}\s+", "", out, flags=re.M)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()[:SLACK_MRKDWN_SECTION_LIMIT]


def humanize_tool_action(raw: str) -> str:
    """Turn a tool id like ``tickets.tickets-create`` into "Tickets · Create"."""
    dot = raw.find(".")
    server = raw[:dot] if dot > 0 else ""
    tool = raw[dot + 1 :] if dot > 0 else raw
    if server and tool.lower().startswith(f"{server.lower()}-"):
        tool = tool[len(server) + 1 :]

    def words(value: str) -> str:
        return " ".join(part for part in re.split(r"[-_\s]+", value) if part)

    def cap(value: str) -> str:
        return value[:1].upper() + value[1:] if value else value

    app = cap(words(server)) if server else ""
    action = cap(words(tool))
    return f"{app} · {action}" if app else action


def markdown_reply_blocks(text: str) -> list[dict]:
    chunks = split_slack_markdown(text)
    if len(chunks) > 1:
        raise ValueError(
            "Slack markdown blocks have a cumulative 12,000-character payload limit; send chunks as separate messages"
        )
    return [{"type": "markdown", "text": chunk} for chunk in chunks]


def mrkdwn_reply_blocks(text: str) -> list[dict]:
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": to_slack_mrkdwn(text)[:SLACK_MRKDWN_SECTION_LIMIT]},
        }
    ]
