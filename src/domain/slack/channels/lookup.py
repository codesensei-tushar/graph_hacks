"""Port of company-brain ``brain/slack/channel-lookup.ts``.

Read a channel's history for one intent (summarize a window, find related
messages, extract likely action items, find a link) and search across the
channels the bot is in, gated by ``check_asker_can_search_channel``.
"""

import re
import time
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from typing import Any, Literal

from src.domain.memory.writeback import SlackMemoryScope
from src.domain.slack.channels._compat import as_dict, field, normalize_slack_message_content
from src.domain.slack.channels.directory import (
    SlackConversation,
    SlackResponseSurface,
    check_asker_can_search_channel,
    find_channels_named_in_query,
    get_channel_directory,
    rank_channels_for_query,
    resolve_channel,
)


def format_channel_message_text(
    text: str | None,
    user_names: dict[str, str] | None = None,
    group_handles: dict[str, str] | None = None,
) -> str:
    if not text:
        return ""

    def user(match: re.Match) -> str:
        user_id, label = match.group(1), match.group(2)
        name = (user_names or {}).get(user_id)
        return f"@{name}" if name else f"@{label}" if label else f"@{user_id}"

    def group(match: re.Match) -> str:
        handle = (group_handles or {}).get(match.group(1))
        return f"@{handle}" if handle else f"@{match.group(1)}"

    # Keep native channel tokens intact so routing can tell channel selections
    # from issue numbers and ordinary hashtags.
    out = re.sub(r"<@([A-Z0-9]+)(?:\|([^>]+))?>", user, text)
    out = re.sub(r"<!subteam\^[A-Z0-9]+\|([^>]+)>", lambda m: f"@{m.group(1)}", out)
    out = re.sub(r"<!subteam\^([A-Z0-9]+)>", group, out)
    out = re.sub(r"<!(here|channel|everyone)>", lambda m: f"@{m.group(1)}", out)
    return re.sub(r"\s+", " ", out).strip()


@dataclass
class SlackLookupContext:
    client: Any
    channel: str
    thread_ts: str | None = None
    team_id: str | None = None
    memory_scope: SlackMemoryScope | None = None
    # Explicit read surface; replaces what memory_scope implies.
    memory_container_tags: list[str] | None = None
    tz_offset_seconds: int = 0
    user_names: dict[str, str] = dc_field(default_factory=dict)


ChannelLookupIntent = Literal["summarize_window", "find_related", "extract_open_actions", "find_link"]
ChannelLookupWindow = Literal["today", "yesterday", "last_7_days", "last_24_hours", "last_30_days", "last_90_days"]


@dataclass
class ChannelLookupArgs:
    intent: ChannelLookupIntent
    query: str | None = None
    window: ChannelLookupWindow | None = None
    limit: int | None = None


@dataclass(frozen=True)
class SlackTimeRange:
    oldest_ms: int
    latest_ms: int
    label: str


DEFAULT_LIMIT = 80
MAX_LIMIT = 150
PAGE_SIZE = 200
SCAN_BUDGET: dict[str, dict[str, int]] = {
    "today": {"max_messages": 200, "max_pages": 1},
    "yesterday": {"max_messages": 200, "max_pages": 1},
    "last_24_hours": {"max_messages": 200, "max_pages": 1},
    "last_7_days": {"max_messages": 400, "max_pages": 2},
    "last_30_days": {"max_messages": 1000, "max_pages": 5},
    "last_90_days": {"max_messages": 1600, "max_pages": 8},
}
MAX_THREAD_PARENTS = 4
THREAD_REPLY_LIMIT = 12

ACTION_PATTERNS = [
    re.compile(r"\b(todo|to-do|action item|follow[- ]?up)\b", re.I),
    re.compile(r"\b(need to|needs to|should|must|have to)\b", re.I),
    re.compile(r"\b(i'll|i will|we'll|we will|can someone|could someone|who can)\b", re.I),
    re.compile(r"\b(assign|assigned to|owner:|please)\b", re.I),
]
COMPLETION_PATTERNS = [
    re.compile(r"\b(done|completed|finished|shipped|fixed|resolved|closed|merged|deployed)\b", re.I),
    re.compile(r"\b(took care of|handled|all set|no longer needed)\b", re.I),
]

_DAY_MS = 24 * 60 * 60 * 1000


def slack_ts_to_ms(ts: str | None) -> int:
    if not ts:
        return 0
    try:
        return int(float(ts) * 1000)
    except ValueError:
        return 0


def ms_to_slack_ts(ms: float) -> str:
    return f"{ms / 1000:.6f}"


def _start_of_local_day(ms: int, tz_offset_seconds: int) -> int:
    local = datetime.fromtimestamp((ms + tz_offset_seconds * 1000) / 1000, tz=timezone.utc)
    midnight = datetime(local.year, local.month, local.day, tzinfo=timezone.utc)
    return int(midnight.timestamp() * 1000) - tz_offset_seconds * 1000


def parse_slack_time_window(window: str | None, now_ms: int, tz_offset_seconds: int = 0) -> SlackTimeRange:
    w = window or "last_7_days"
    today_start = _start_of_local_day(now_ms, tz_offset_seconds)
    if w == "today":
        return SlackTimeRange(today_start, now_ms, "today")
    if w == "yesterday":
        return SlackTimeRange(today_start - _DAY_MS, today_start, "yesterday")
    if w == "last_24_hours":
        return SlackTimeRange(now_ms - _DAY_MS, now_ms, "last 24 hours")
    if w == "last_30_days":
        return SlackTimeRange(now_ms - 30 * _DAY_MS, now_ms, "last 30 days")
    if w == "last_90_days":
        return SlackTimeRange(now_ms - 90 * _DAY_MS, now_ms, "last 90 days")
    return SlackTimeRange(now_ms - 7 * _DAY_MS, now_ms, "last 7 days")


def format_slack_ts_human(ts: str, tz_offset_seconds: int = 0) -> str:
    ms = slack_ts_to_ms(ts)
    if not ms:
        return ts
    moment = datetime.fromtimestamp((ms + tz_offset_seconds * 1000) / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%d %H:%M")


@dataclass
class FormattedSlackMessage:
    ts: str
    when: str
    speaker: str
    text: str
    user_id: str | None = None
    thread_ts: str | None = None
    reply_count: int | None = None


def _speaker(message: dict, user_names: dict[str, str] | None) -> str:
    user_id = message.get("user")
    if user_id:
        return (user_names or {}).get(user_id, user_id)
    return "bot" if message.get("bot_id") else "user"


def format_channel_messages(messages, user_names: dict[str, str] | None = None, tz_offset_seconds: int = 0) -> list[FormattedSlackMessage]:
    out: list[FormattedSlackMessage] = []
    for raw in messages:
        m = as_dict(raw)
        text = format_channel_message_text(normalize_slack_message_content(m), user_names)
        if not text:
            continue
        reply_count = m.get("reply_count")
        out.append(
            FormattedSlackMessage(
                ts=m.get("ts") or "",
                when=format_slack_ts_human(m.get("ts") or "", tz_offset_seconds),
                user_id=m.get("user"),
                speaker=_speaker(m, user_names),
                text=text,
                thread_ts=m.get("thread_ts"),
                reply_count=reply_count if isinstance(reply_count, int) else None,
            )
        )
    return out


def _tokenize(text: str) -> list[str]:
    return [t for t in re.split(r"\s+", re.sub(r"[^a-z0-9\s]", " ", text.lower())) if len(t) > 2]


def score_related_message(text: str, query: str) -> float:
    q_tokens = set(_tokenize(query))
    if not q_tokens:
        return 0
    m_tokens = _tokenize(text)
    if not m_tokens:
        return 0
    hits = sum(1 for t in m_tokens if t in q_tokens)
    phrase = query.lower().strip()
    if len(phrase) > 4 and phrase in text.lower():
        hits += 3
    return hits / max(len(q_tokens), 1)


def looks_like_action(text: str) -> bool:
    return any(p.search(text) for p in ACTION_PATTERNS)


def looks_like_completion(text: str) -> bool:
    return any(p.search(text) for p in COMPLETION_PATTERNS)


@dataclass
class ExtractedActionItem:
    ts: str
    when: str
    speaker: str
    text: str
    status: Literal["likely_open", "likely_done"]
    status_reason: str


def extract_action_items_from_messages(messages: list[FormattedSlackMessage]) -> list[ExtractedActionItem]:
    ordered = sorted(messages, key=lambda m: slack_ts_to_ms(m.ts))
    items: list[ExtractedActionItem] = []
    next_completion: FormattedSlackMessage | None = None
    for msg in reversed(ordered):
        completion_after = next_completion
        if looks_like_action(msg.text):
            items.append(
                ExtractedActionItem(
                    ts=msg.ts,
                    when=msg.when,
                    speaker=msg.speaker,
                    text=msg.text,
                    status="likely_done" if completion_after else "likely_open",
                    status_reason=(
                        f"Later message at {completion_after.when} suggests completion."
                        if completion_after
                        else "No later completion signal in this window."
                    ),
                )
            )
        if looks_like_completion(msg.text):
            next_completion = msg
    items.reverse()
    return items


def _compact_lines(messages: list[FormattedSlackMessage], max_lines: int = 40) -> list[str]:
    return [f"[{m.when}] {m.speaker}: {m.text}" for m in messages[-max_lines:]]


def _is_thread_parent(message: dict) -> bool:
    reply_count = message.get("reply_count")
    return isinstance(reply_count, int) and reply_count > 0 and bool(message.get("ts"))


def _expand_thread_snippets(ctx: SlackLookupContext, parents: list[dict]) -> list[str]:
    snippets: list[str] = []
    for parent in parents[:MAX_THREAD_PARENTS]:
        if not parent.get("ts"):
            continue
        replies = ctx.client.get_slack_thread(ctx.channel, parent["ts"], THREAD_REPLY_LIMIT)
        formatted = format_channel_messages(replies, ctx.user_names, ctx.tz_offset_seconds)
        if len(formatted) <= 1:
            continue
        snippets.append(f"Thread {parent['ts']}:")
        snippets.extend(_compact_lines(formatted[1:], THREAD_REPLY_LIMIT))
    return snippets


@dataclass
class ExtractedLink:
    ts: str
    when: str
    speaker: str
    label: str
    url: str
    kind: Literal["link", "file"]
    context: str


SLACK_LINK_RE = re.compile(r"<(https?://[^|>]+)(?:\|([^>]*))?>", re.I)
BARE_URL_RE = re.compile(r"(https?://[^\s|<>]+)", re.I)


def extract_links_from_messages(messages, user_names: dict[str, str] | None = None, tz_offset_seconds: int = 0) -> list[ExtractedLink]:
    out: list[ExtractedLink] = []
    seen: set[str] = set()
    for raw in messages:
        m = as_dict(raw)
        ts = m.get("ts") or ""
        when = format_slack_ts_human(ts, tz_offset_seconds)
        speaker = _speaker(m, user_names)
        text = normalize_slack_message_content(m)
        context = format_channel_message_text(text, user_names)

        def push(url: str, label: str, kind: str, key: str) -> None:
            if not url or key in seen:
                return
            seen.add(key)
            out.append(ExtractedLink(ts=ts, when=when, speaker=speaker, url=url, label=label or url, kind=kind, context=context))

        for match in SLACK_LINK_RE.finditer(text):
            push(match.group(1) or "", (match.group(2) or "").strip(), "link", f"{ts}:{match.group(1)}")
        for match in BARE_URL_RE.finditer(SLACK_LINK_RE.sub(" ", text)):
            push(match.group(1) or "", match.group(1) or "", "link", f"{ts}:{match.group(1)}")
        for f in m.get("files") or []:
            url = f.get("url_private") or f.get("url_private_download") or ""
            push(url, f.get("name") or "file", "file", f"{ts}:{f.get('id')}")
    return out


def _history(client, channel: str, *, oldest: str, latest: str, limit: int, max_messages: int, max_pages: int) -> tuple[bool, list[dict], str | None]:
    result = client.get_slack_channel_history(
        channel, oldest=oldest, latest=latest, limit=limit, max_messages=max_messages, max_pages=max_pages
    )
    messages = [as_dict(m) for m in (field(result, "messages", []) or [])]
    return bool(field(result, "ok", False)), messages, field(result, "error")


def run_slack_channel_lookup(ctx: SlackLookupContext, args: ChannelLookupArgs, now_ms: int | None = None) -> str:
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    rng = parse_slack_time_window(args.window, now_ms, ctx.tz_offset_seconds)
    budget = SCAN_BUDGET[args.window or "last_7_days"]
    limit = min(max(args.limit or DEFAULT_LIMIT, 1), MAX_LIMIT)
    ok, raw, error = _history(
        ctx.client,
        ctx.channel,
        oldest=ms_to_slack_ts(rng.oldest_ms),
        latest=ms_to_slack_ts(rng.latest_ms),
        limit=PAGE_SIZE,
        max_messages=budget["max_messages"],
        max_pages=budget["max_pages"],
    )
    if not ok and not raw:
        return f"Channel history unavailable ({error}). The bot may be rate-limited; please retry shortly."
    # Rate-limiting cut pagination short after at least one page: say so on negatives.
    incomplete_caveat = (
        " (scan incomplete — rate-limited mid-pagination; older messages in this window may be missing)" if not ok else ""
    )

    if args.intent == "find_link":
        query = (args.query or "").strip()
        links = extract_links_from_messages(raw, ctx.user_names, ctx.tz_offset_seconds)
        if not links:
            return f"No links or files shared in {rng.label}.{incomplete_caveat}"
        if query:
            scored = [(link, score_related_message(f"{link.label} {link.url} {link.context}", query)) for link in links]
            ranked = [link for link, score in sorted((s for s in scored if s[1] > 0), key=lambda s: s[1], reverse=True)]
        else:
            ranked = sorted(links, key=lambda link: slack_ts_to_ms(link.ts), reverse=True)
        top = ranked[: min(limit, 15)]
        if not top:
            return f'No links or files matching "{query}" in {rng.label}.{incomplete_caveat}'
        header = f'Links/files ({rng.label}){f" matching \"{query}\"" if query else ""}:{incomplete_caveat}'
        return "\n".join(
            [header]
            + [f"[{l.when}] {l.speaker} — {l.label}: {l.url}{' (file)' if l.kind == 'file' else ''}" for l in top]
        )

    formatted = format_channel_messages(raw, ctx.user_names, ctx.tz_offset_seconds)
    if not formatted:
        return f"No channel messages found for {rng.label}. The bot may lack access to this channel or the window was empty.{incomplete_caveat}"

    if args.intent == "summarize_window":
        lines = _compact_lines(formatted)
        snippets = _expand_thread_snippets(ctx, [m for m in raw if _is_thread_parent(m)])
        return "\n".join(
            [f"Channel activity ({rng.label}, {len(formatted)} messages){incomplete_caveat}:", *lines]
            + (["", "Thread snippets:", *snippets] if snippets else [])
        )

    if args.intent == "find_related":
        query = (args.query or "").strip()
        if not query:
            return "find_related requires a query describing what to match in channel history."
        scored = sorted(
            ((m, score_related_message(m.text, query)) for m in formatted),
            key=lambda item: item[1],
            reverse=True,
        )
        scored = [item for item in scored if item[1] > 0][:20]
        if not scored:
            return f"No related messages found in {rng.label} for: {query}{incomplete_caveat}"
        top_ts = {m.ts for m, _ in scored[:5]}
        parents = [m for m in raw if m.get("ts") in top_ts and _is_thread_parent(m)]
        snippets = _expand_thread_snippets(ctx, parents)
        return "\n".join(
            [f'Related channel messages ({rng.label}) for "{query}"{incomplete_caveat}:']
            + [f"[{m.when}] {m.speaker} (match {round(score * 100)}%): {m.text}" for m, score in scored]
            + (["", "Thread snippets:", *snippets] if snippets else [])
        )

    actions = extract_action_items_from_messages(formatted)
    open_items = [a for a in actions if a.status == "likely_open"]
    done_items = [a for a in actions if a.status == "likely_done"]
    if not actions:
        return f"No likely action items detected in {rng.label}. Slack has no task state — this is inferred from message language only.{incomplete_caveat}"
    return "\n".join(
        [
            f"Likely action items in channel ({rng.label}){incomplete_caveat}. Status is inferred from message wording, not a task system.",
            "",
            f"Likely open ({len(open_items)}):" if open_items else "Likely open: none detected.",
            *[f"- [{a.when}] {a.speaker}: {a.text} ({a.status_reason})" for a in open_items],
            "",
            f"Likely done ({len(done_items)}):" if done_items else "Likely done: none detected.",
            *[f"- [{a.when}] {a.speaker}: {a.text} ({a.status_reason})" for a in done_items],
        ]
    )


@dataclass
class SlackChannelsSearchArgs:
    query: str
    window: ChannelLookupWindow | None = None
    channels: list[str] | None = None


@dataclass
class SlackChannelsSearchAuth:
    asker_slack_user_id: str | None = None
    is_org_member: bool = False
    response_surface: SlackResponseSurface | None = None
    asker_is_restricted: bool = False


MAX_SLACK_FANOUT = 5
FANOUT_MAX_MESSAGES = 300
FANOUT_MAX_PAGES = 2


def _format_channel_name_list(channels: list[SlackConversation]) -> str:
    names = [f"#{c.name}" for c in channels]
    if len(names) <= 1:
        return names[0] if names else "that channel"
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])}, and {names[-1]}"


def _plural(n: int) -> str:
    return "" if n == 1 else "s"


def run_slack_channels_search(
    ctx: SlackLookupContext,
    auth: SlackChannelsSearchAuth,
    args: SlackChannelsSearchArgs,
    now_ms: int | None = None,
) -> str:
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    query = args.query.strip()
    if not query:
        return "search_slack_channels needs a query describing what to find."
    not_found: list[str] = []
    truncated_explicit = 0
    if args.channels:
        candidates: list[SlackConversation] = []
        for ref in args.channels:
            res = resolve_channel(ctx.client, ctx.team_id, ref)
            if res.status == "ok" and res.channel:
                candidates.append(res.channel)
            else:
                not_found.append(ref)
        truncated_explicit = max(len(candidates) - MAX_SLACK_FANOUT, 0)
    else:
        directory = get_channel_directory(ctx.client, ctx.team_id)
        if not directory:
            return "I'm not in any channels I can search yet. Invite me to a channel with /invite, then ask again."
        named = find_channels_named_in_query(directory, query, MAX_SLACK_FANOUT)
        if not named:
            fresh = get_channel_directory(ctx.client, ctx.team_id, force_refresh=True)
            if fresh:
                directory = fresh
                named = find_channels_named_in_query(directory, query, MAX_SLACK_FANOUT)
        ranked = rank_channels_for_query(directory, query, MAX_SLACK_FANOUT)
        candidates = []
        for c in named + ranked:
            if all(picked.id != c.id for picked in candidates):
                candidates.append(c)
            if len(candidates) >= MAX_SLACK_FANOUT:
                break
    candidates = candidates[:MAX_SLACK_FANOUT]

    gated = [
        (
            c,
            check_asker_can_search_channel(
                ctx.client,
                ctx.team_id,
                c,
                auth.asker_slack_user_id,
                current_channel_id=ctx.channel,
                is_org_member=auth.is_org_member is True,
                response_surface=auth.response_surface,
                asker_is_restricted=auth.asker_is_restricted is True,
            ),
        )
        for c in candidates
    ]
    allowed = [c for c, access in gated if access.ok]
    denied = [(c, access) for c, access in gated if not access.ok]
    membership_reasons = ("private_channel_requires_membership", "unknown_asker")
    explicit_single = bool(args.channels) and len(args.channels) == 1
    if explicit_single and any(a.reason in membership_reasons for _, a in denied):
        channels = [c for c, a in denied if a.reason in membership_reasons]
        return f"I can't go through {_format_channel_name_list(channels)} because you're not a member of {'it' if len(channels) == 1 else 'them'}."
    if explicit_single and any(a.reason == "private_channel_non_dm_response" for _, a in denied):
        channels = [c for c, a in denied if a.reason == "private_channel_non_dm_response"]
        return f"I can't answer from {_format_channel_name_list(channels)} here. DM me and I can answer there."
    if not allowed:
        if any(a.reason in membership_reasons for _, a in denied):
            return "I can't go through that channel because you're not a member of it."
        if any(a.reason == "private_channel_non_dm_response" for _, a in denied):
            return "I can't answer from that private channel here. DM me and I can answer there."
        if any(a.reason == "not_org_member" for _, a in denied):
            return "I can only search other Slack channels for confirmed org members."
        parts = ["No searchable channels matched. Private channels are searchable only by their members."]
        if truncated_explicit:
            parts.append(
                f"Only checked the first {MAX_SLACK_FANOUT} resolved requested channels; skipped {truncated_explicit} additional channel{_plural(truncated_explicit)}."
            )
        if denied:
            parts.append(f"Skipped {len(denied)} channel{_plural(len(denied))}.")
        if not_found:
            parts.append("Some requested channels weren't found.")
        return " ".join(parts)

    rng = parse_slack_time_window(args.window, now_ms, ctx.tz_offset_seconds)
    incomplete: list[str] = []
    merged: list[tuple[str, FormattedSlackMessage, float]] = []
    for c in allowed:
        ok, raw, _error = _history(
            ctx.client,
            c.id,
            oldest=ms_to_slack_ts(rng.oldest_ms),
            latest=ms_to_slack_ts(rng.latest_ms),
            limit=PAGE_SIZE,
            max_messages=FANOUT_MAX_MESSAGES,
            max_pages=FANOUT_MAX_PAGES,
        )
        if not ok:
            incomplete.append(c.name)
            if not raw:
                continue
        formatted = format_channel_messages(raw, ctx.user_names, ctx.tz_offset_seconds)
        scored = sorted(
            ((c.name, m, score_related_message(m.text, query)) for m in formatted),
            key=lambda item: item[2],
            reverse=True,
        )
        merged.extend([item for item in scored if item[2] > 0][:5])
    merged = sorted(merged, key=lambda item: item[2], reverse=True)[:20]

    searched_label = ", ".join(f"#{c.name}" for c in allowed)
    incomplete_names = ", ".join(f"#{n}" for n in incomplete)
    if not merged:
        incomplete_label = f" History for {incomplete_names} was incomplete (rate-limited mid-pagination)." if incomplete else ""
        return f'No matches for "{query}" in {rng.label} across {searched_label}.{incomplete_label}'
    footer: list[str] = []
    if incomplete:
        footer.append(f"History for {incomplete_names} was incomplete (rate-limited mid-pagination; some messages may be missing).")
    if truncated_explicit:
        footer.append(
            f"Only searched the first {MAX_SLACK_FANOUT} resolved requested channels; skipped {truncated_explicit} additional channel{_plural(truncated_explicit)}."
        )
    if denied:
        if args.channels:
            footer.append(f"Skipped {_format_channel_name_list([c for c, _ in denied])}.")
        else:
            footer.append(f"Skipped {len(denied)} channel{_plural(len(denied))}.")
    if not_found:
        footer.append("Some requested channels weren't found.")
    return "\n".join(
        [f'Cross-channel matches for "{query}" ({rng.label}, searched {searched_label}):']
        + [f"[#{channel}] [{m.when}] {m.speaker} (match {round(score * 100)}%): {m.text}" for channel, m, score in merged]
        + (["", *footer] if footer else [])
    )
