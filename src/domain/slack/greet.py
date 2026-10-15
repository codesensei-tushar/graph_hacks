"""Port of company-brain `brain/slack/greet.ts`.

Copy changes from the hosted product: the bot introduces itself as Company Brain;
the "connect your tools" and free-trial beats are dropped (no connectors or
billing here yet); "creating their account" becomes adding them to the company graph.
"""

import logging

from src.domain.slack.lifecycle_api import (
    lookup_slack_user_by_email,
    lookup_slack_user_info,
    open_slack_conversation,
    post_slack_message,
)

log = logging.getLogger(__name__)

DEFAULT_STARTERS = [
    "Catch me up on what I missed this week.",
    "What has the team decided recently, and who owns it?",
]


def install_greeting(
    first_name: str | None = None,
    company_name: str | None = None,
    home_channel_id: str | None = None,
    starters: list[str] | None = None,
) -> str:
    """Deterministic skeleton so the voice never drifts; only the starters are generated."""
    hi = f"Hey {first_name}," if first_name else "Hey,"
    home = f"<#{home_channel_id}>" if home_channel_id else "#company-brain"
    subject = company_name or "your company"
    picks = starters or DEFAULT_STARTERS

    # Blank lines between every beat — Slack collapses tight blocks into a wall of text.
    lines = [
        f"{hi} thanks for bringing me on. 👋",
        "",
        "I'm *Company Brain*, your company's brain. I keep track of what your team ships, decides, and discusses, so nobody has to dig for it.",
        "",
        "*Ways to work with me*",
        "",
        "💬  *DM me here*",
        "Ask me anything, like a teammate who's read everything.",
        "",
        "📣  *@Company Brain in any channel*",
        "Mention me and I'll jump in with the whole thread as context.",
        "",
        f"📨  *Your team is joining {home}*",
        f"I'm adding every full workspace member to {home}, adding them to the company graph, and sending each person a welcome DM. Guests and external members are excluded.",
        "",
        f"🔍  I'm reading up on {subject} right now. I'll post what I learn in {home} shortly.",
        "",
        "*Try one of these — copy, paste, send:*",
        "",
    ]
    for starter in picks:
        lines += [f">{starter}", ""]
    lines.append("Or just tell me what you need and I'll take it from there. 🚀")
    return "\n".join(lines)


def first_name_of(name: str | None) -> str | None:
    parts = (name or "").strip().split()
    return parts[0] if parts else None


def member_greeting(first_name: str | None = None, org: str | None = None) -> str:
    hi = f"Hey {first_name}," if first_name else "Hey,"
    place = f" to *{org}*" if org else ""
    return f"{hi} welcome{place}. I'm Company Brain, I keep track of what the team's working on. Need to get up to speed? Just ask me anything."


def _dm_slack_user(web, slack_user_id: str, text: str) -> None:
    channel = open_slack_conversation(web, slack_user_id)
    if not channel:
        return
    post_slack_message(web, channel, text)


def greet_org_member_by_email(web, email: str | None, org_name: str | None = None) -> None:
    """Best-effort member welcome; resolves the Slack id by email, never raises."""
    try:
        if not email:
            return
        slack_user_id = lookup_slack_user_by_email(web, email)
        if not slack_user_id:
            return
        info = lookup_slack_user_info(web, slack_user_id)
        user = info.user if info.ok else None
        first = first_name_of((user.display_name or user.name) if user else None)
        _dm_slack_user(web, slack_user_id, member_greeting(first, org_name))
    except Exception:
        log.warning("[slack] greet member failed", exc_info=True)
