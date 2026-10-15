"""Plain-name wake ("hey brain, ..."). Port of company-brain ``brain/slack/bot-address.ts``."""

import re

PRODUCT_ALIASES = ("company brain", "company-brain")
DIRECT_GREETING_PREFIX = r"(?:(?:hey|hi|hello|yo)(?:\s+there)?[\s,!:;-]+)?"


def clean_mention(text: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<@[^>]+>", "", text or "")).strip()


def collect_bot_address_aliases(name: str | None, display_name: str | None, handle: str | None) -> list[str]:
    out = list(PRODUCT_ALIASES)

    def add(value: str | None) -> None:
        trimmed = (value or "").strip().lower()
        if len(trimmed) >= 2 and trimmed not in out:
            out.append(trimmed)

    add(name)
    add(display_name)
    add(handle)
    for field in (name, display_name):
        parts = (field or "").strip().split()
        add(parts[0] if parts else None)
    return out


def is_bot_addressed_by_name(text: str | None, aliases: list[str]) -> bool:
    """True when the message plainly addresses the bot by name (no Slack @mention token)."""
    normalized = clean_mention(text).lower()
    if not normalized or not aliases:
        return False
    for alias in aliases:
        if not alias or len(alias) < 2:
            continue
        pattern = rf"^{DIRECT_GREETING_PREFIX}@?{re.escape(alias)}(?:\b|[\s,:;!?.\"'])"
        if re.search(pattern, normalized, re.IGNORECASE):
            return True
    return False
