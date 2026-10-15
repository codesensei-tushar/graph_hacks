"""Port of company-brain `brain/slack/install-greeting.ts`, plus the installer DM from
`greetSlackInstaller` in `brain/slack/onboarding-card.ts` (minus its tool-connect card).

Starters come from a model given the company research brief. There is no model or
research yet, so ``compose_starters`` returns None and the greeting uses the
default starters, exactly as company-brain does when either is missing.
"""

import logging
import re
import time

from src.domain.slack.greet import first_name_of, install_greeting
from src.domain.slack.lifecycle_api import lookup_slack_user_info, open_slack_conversation, post_slack_message
from src.domain.slack.team_invite_card import AUTOMATIC_TEAM_INVITE_FALLBACK, automatic_team_invite_progress_blocks
from src.integrations.llm.model import ModelRequest, ModelUnavailable, generate_object

log = logging.getLogger(__name__)

BUBBLE_DELAY_MS = 1000

_STARTERS_SCHEMA = {
    "type": "object",
    "properties": {
        "starters": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 2,
            "description": (
                "Exactly two copy-ready questions a teammate could paste back right now, each answerable from the "
                "company research. Each under 14 words, written as a direct ask, no leading bullet or number, no quotes."
            ),
        }
    },
    "required": ["starters"],
}


def research_brief_for(team_id: str) -> str:
    """Company research isn't built yet; an empty brief means "use the defaults"."""
    return ""


def compose_starters(brief: str, company: str) -> list[str] | None:
    if not brief:
        return None
    try:
        out = generate_object(
            ModelRequest(
                profile="fast",
                system="",
                prompt=(
                    f"You are Company Brain, just added to {company}'s Slack. Using only the research below, write two "
                    f"questions the team could ask you right now that you could genuinely answer.\n\n"
                    f"Research:\n{brief}\n\nMake them specific to {company}, not generic. Never invent facts."
                ),
                schema=_STARTERS_SCHEMA,
            )
        )
    except ModelUnavailable:
        return None
    except Exception:
        log.warning("[slack] compose starters failed", exc_info=True)
        return None
    starters = []
    for starter in out.get("starters") or []:
        clean = re.sub(r"^[\"'>\-\d.\s]+", "", str(starter).strip())
        if clean:
            starters.append(clean)
    return starters[:2] or None


def compose_install_bubbles(
    team_id: str, first_name: str | None, company_name: str | None = None, home_channel_id: str | None = None
) -> list[str]:
    base = {"first_name": first_name, "company_name": company_name, "home_channel_id": home_channel_id}
    try:
        brief = research_brief_for(team_id)
        if not brief:
            return [install_greeting(**base)]
        starters = compose_starters(brief, company_name or "the company")
        return [install_greeting(**base, starters=starters)]
    except Exception:
        log.warning("[slack] compose install bubbles failed", exc_info=True)
        return [install_greeting(**base)]


def greet_slack_installer(*, workspace, slack_user_id: str | None, team_name: str | None = None, sleep=time.sleep) -> None:
    """Best-effort installer welcome DM, then the automatic team rollout card. Never raises."""
    from src.domain.slack import team_invite
    from src.domain.slack.workspace import get_home_channel

    try:
        if not slack_user_id:
            log.warning("[slack] greet skipped: no authed slack user team=%s", workspace.team_id)
            return
        team_invite.start_automatic_team_invite_rollout(team_id=workspace.team_id, installer_slack_user_id=slack_user_id)
        web = workspace.client.web
        channel = open_slack_conversation(web, slack_user_id)
        if not channel:
            log.warning("[slack] installer DM unavailable; workspace rollout continues team=%s", workspace.team_id)
            return
        info = lookup_slack_user_info(web, slack_user_id)
        user = info.user if info.ok else None
        first = first_name_of((user.display_name or user.name) if user else None)
        home = get_home_channel(workspace.team_id)

        # First bubble is the thread parent; the rest reply under it.
        bubbles = compose_install_bubbles(
            workspace.team_id, first, team_name or workspace.team_name, home["channel_id"] if home else None
        )
        parent_ts = None
        for index, bubble in enumerate(bubbles):
            ts = post_slack_message(web, channel, bubble, parent_ts)
            parent_ts = parent_ts or ts
            if index < len(bubbles) - 1:
                sleep(BUBBLE_DELAY_MS / 1000)
        if not parent_ts:
            return
        sleep(BUBBLE_DELAY_MS / 1000)
        # Top level, not under the greeting's thread: a thread reply is easy to miss.
        card_ts = post_slack_message(
            web, channel, AUTOMATIC_TEAM_INVITE_FALLBACK, None, automatic_team_invite_progress_blocks(phase="enumerating")
        )
        if card_ts:
            try:
                team_invite.attach_automatic_team_invite_card(
                    team_id=workspace.team_id, admin_channel=channel, admin_message_ts=card_ts
                )
            except Exception:
                log.warning("[slack] team invite card failed", exc_info=True)
        _call_rollout_hook("ensure_admin_rollout_card", team_id=workspace.team_id, installer_slack_user_id=slack_user_id)
    except Exception:
        log.warning("[slack] greet installer failed", exc_info=True)


def _call_rollout_hook(name: str, **kwargs) -> None:
    """Public-channel rollout lives in another module; call it when present."""
    try:
        from src.domain.slack.channels import rollout as public_channel_rollout
    except ImportError:
        return
    hook = getattr(public_channel_rollout, name, None)
    if hook is None:
        return
    try:
        # Rollout hooks take one payload dict, like their scheduler jobs.
        hook(kwargs)
    except Exception:
        log.warning("[slack] %s failed", name, exc_info=True)
