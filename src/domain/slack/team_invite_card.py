"""Port of company-brain `brain/slack/team-invite-card.ts` (+ the action ids from `brain/constants.ts`).

Tool-connect buttons on the member intro are kept as an option but nothing passes
any until connectors exist.
"""

from dataclasses import dataclass

TEAM_INVITE_SELECT_ACTION_ID = "brain_add_team_select"
TEAM_INVITE_SEND_ACTION_ID = "brain_add_team_send"
TEAM_INVITE_NOTIFY_ALL_ACTION_ID = "brain_add_team_notify_all"
TEAM_INVITE_PICK_BLOCK_ID = "brain_add_team_pick"

TEAM_INVITE_CARD_FALLBACK = "Bring your team in"
AUTOMATIC_TEAM_INVITE_FALLBACK = "Adding your workspace members"
MEMBER_CONNECT_ACTION_PREFIX = "brain_member_connect_"

DEFAULT_MEMBER_STARTERS = [
    "Catch me up on what the team shipped this week.",
    "What has the team decided recently, and who owns it?",
]

# Slackbot has a fixed workspace-independent id.
SLACKBOT_ID = "USLACKBOT"


def is_eligible_full_member(member, bot_user_id: str | None = None, team_id: str | None = None) -> bool:
    if member is None or not getattr(member, "id", None):
        return False
    if member.id in (SLACKBOT_ID, bot_user_id):
        return False
    if (
        getattr(member, "is_bot", False)
        or getattr(member, "is_restricted", False)
        or getattr(member, "is_ultra_restricted", False)
        or getattr(member, "is_stranger", False)
    ):
        return False
    member_team = getattr(member, "team_id", None)
    return not team_id or not member_team or member_team == team_id


def automatic_team_invite_progress_blocks(
    *, phase: str, total: int = 0, processed: int = 0, sent: int = 0, skipped: int = 0, deduped: int = 0
) -> list[dict]:
    if phase == "enumerating":
        status = "Finding eligible full workspace members…"
    elif phase == "provisioning":
        status = f"Adding members to the company graph… {processed}/{total}"
    elif phase == "notifying":
        status = f"Inviting members and sending welcome DMs… {processed}/{total}"
    elif phase == "done":
        status = (
            f"✅ Rollout complete: {sent} welcomed"
            + (f", {deduped} already notified" if deduped else "")
            + (f", {skipped} failed" if skipped else "")
            + "."
        )
    else:
        status = f"⚠️ Rollout stopped: {sent} welcomed" + (f", {skipped} failed" if skipped else "") + "."
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Adding your workspace to Company Brain* 👥\nFull members are being added, invited to #company-brain, and sent a personal introduction. Guests and external members are excluded.",
            },
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": status}]},
    ]


def team_invite_card_blocks(status: str = "idle", sent: int = 0, skipped: int = 0, total: int = 0) -> list[dict]:
    """The admin-facing "Add your team" card, posted into the installer DM.

    Multi-selects are invalid in `actions` blocks; an input block gives a full-width
    picker, and the selection only submits via the Send button, so picking is deliberate.
    """
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Bring your team in* 👥\nI'll DM each teammate a personal intro so they know what I can do for them.",
            },
        },
        {
            "type": "input",
            "block_id": TEAM_INVITE_PICK_BLOCK_ID,
            "label": {"type": "plain_text", "text": "Pick teammates", "emoji": True},
            "element": {
                "type": "multi_users_select",
                "action_id": TEAM_INVITE_SELECT_ACTION_ID,
                "placeholder": {"type": "plain_text", "text": "Search teammates…", "emoji": True},
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": TEAM_INVITE_SEND_ACTION_ID,
                    "text": {"type": "plain_text", "text": "Send intros", "emoji": True},
                    "value": "send",
                    "style": "primary",
                },
                {
                    "type": "button",
                    "action_id": TEAM_INVITE_NOTIFY_ALL_ACTION_ID,
                    "text": {"type": "plain_text", "text": "Notify everyone", "emoji": True},
                    "value": "all",
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Notify everyone?"},
                        "text": {
                            "type": "plain_text",
                            "text": "I'll DM every full member of this workspace a short intro. Guests and bots are skipped, and nobody is messaged twice.",
                        },
                        "confirm": {"type": "plain_text", "text": "Notify everyone"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
            ],
        },
    ]
    if status == "running":
        progress = f"Notifying your team… {sent}/{total} sent" + (f", {skipped} skipped" if skipped else "")
    elif status == "done":
        progress = (
            f"✅ Notified {sent} teammate{'' if sent == 1 else 's'}"
            + (f" ({skipped} skipped)" if skipped else "")
            + ". Re-run anytime — nobody gets messaged twice."
        )
    elif status == "failed":
        progress = f"⚠️ Stopped after {sent} of {total}. Run it again to retry the rest."
    else:
        progress = "Guests and external collaborators are never messaged."
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": progress}]})
    return blocks


@dataclass
class MemberIntroParts:
    team_id: str
    first_name: str | None = None
    company_name: str | None = None
    home_channel_id: str | None = None
    starters: list[str] | None = None
    has_connect_actions: bool = False


@dataclass(frozen=True)
class MemberConnectAction:
    slug: str
    label: str


def member_intro_text(parts: MemberIntroParts) -> str:
    """Deterministic member intro, same voice as install_greeting."""
    hi = f"Hey {parts.first_name}," if parts.first_name else "Hey,"
    subject = parts.company_name or "your company"
    home = f"<#{parts.home_channel_id}>" if parts.home_channel_id else "#company-brain"
    picks = parts.starters or DEFAULT_MEMBER_STARTERS
    lines = [
        f"{hi} 👋",
        "",
        f"I'm *Company Brain*, {subject}'s brain. I keep track of what the team ships, decides, and discusses, so nobody has to dig for it.",
        "",
        f"🔍  *What I can see:* public channels I've been added to (like {home}) and the tools the team connects.",
        "🔒  *What I can't see:* your DMs with anyone else, or private channels I'm not in.",
        "",
        "*Once you're connected, ask me things like:*",
        "",
    ]
    for starter in picks:
        lines += [f">{starter}", ""]
    lines.append(
        "Connect a tool below—or just ask me something—and I'm all yours. 🚀"
        if parts.has_connect_actions
        else "Ask me something and I'm all yours. 🚀"
    )
    return "\n".join(lines)


def member_intro_blocks(parts: MemberIntroParts, connect_actions: list[MemberConnectAction] | None = None) -> list[dict]:
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": member_intro_text(parts)}}]
    if connect_actions:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": f"{MEMBER_CONNECT_ACTION_PREFIX}{action.slug}",
                        "text": {"type": "plain_text", "text": f"Connect {action.label}", "emoji": True},
                        "value": action.slug,
                        "style": "primary",
                    }
                    for action in connect_actions
                ],
            }
        )
    return blocks
