"""Link a Slack identity to the right Person when Slack's email doesn't match.

Port of company-brain `brain/slack/account-link.ts`. There, the asker opens a
one-time link and confirms while signed in to their account. There is no web
login here, so the link page asks for the email of the Person record to claim, and
the claim is only allowed for an org-member Person with no other active Slack
mapping. The workspace installer gets a DM for every confirmed link, as an audit
trail. Persons carry no permissions of their own (roles and memory scopes follow
the Slack user id), so a wrong claim mislabels authorship but grants no access.

State: ``brain:slack:account-link:<sha256(token)>`` in Redis, TTL 15 minutes, single use.
"""

import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass

from src.core.config import settings
from src.db.falkordb_client import connect, kv
from src.domain.slack.lifecycle_api import open_slack_conversation, post_slack_ephemeral, post_slack_message
from src.domain.slack.workspace import _write_member, get_workspace_member, get_workspace_row

log = logging.getLogger(__name__)

ACCOUNT_LINK_TTL_MS = 15 * 60 * 1_000
_PENDING_PREFIX = "brain:slack:account-link:"


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _state_key(token_hash: str) -> str:
    return _PENDING_PREFIX + token_hash


def _active_key(team_id: str, slack_user_id: str) -> str:
    return f"{_PENDING_PREFIX}active:{team_id}:{slack_user_id}"


def account_link_base_url() -> str:
    return settings.public_url or "http://localhost:8000"


def create_slack_account_link_url(
    *, team_id: str, slack_user_id: str, slack_email: str | None = None, slack_display_name: str | None = None
) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = _sha256_hex(token)
    now = int(time.time() * 1000)
    # Consume any earlier unexpired link for this identity.
    previous = kv().get(_active_key(team_id, slack_user_id))
    if previous:
        kv().delete(_state_key(previous.decode() if isinstance(previous, bytes) else previous))
    state = {
        "team_id": team_id,
        "slack_user_id": slack_user_id,
        "slack_email": (slack_email or "").strip().lower() or None,
        "slack_display_name": (slack_display_name or "").strip() or None,
        "expires_at": now + ACCOUNT_LINK_TTL_MS,
        "consumed_at": None,
    }
    ttl = ACCOUNT_LINK_TTL_MS // 1000
    kv().set(_state_key(token_hash), json.dumps(state), ex=ttl * 2)  # kept past expiry so a late open says "expired"
    kv().set(_active_key(team_id, slack_user_id), token_hash, ex=ttl)
    return f"{account_link_base_url()}/slack/account-link/{token}"


def _account_link_blocks(org_name: str, link_url: str) -> list[dict]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"I couldn't verify your *{org_name}* membership using your Slack email. If the team knows you by another email, link your account. Otherwise, ask a *{org_name}* admin to invite you.",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Link Company Brain account", "emoji": True},
                    "url": link_url,
                    "style": "primary",
                }
            ],
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "This link expires in 15 minutes and can only be used once."}]},
    ]


def post_slack_account_link_prompt(
    *,
    web,
    team_id: str,
    slack_user_id: str,
    org_name: str,
    channel: str,
    is_dm: bool,
    thread_ts: str | None = None,
    slack_email: str | None = None,
    slack_display_name: str | None = None,
) -> bool:
    link_url = create_slack_account_link_url(
        team_id=team_id, slack_user_id=slack_user_id, slack_email=slack_email, slack_display_name=slack_display_name
    )
    text = (
        f"I couldn't verify your {org_name} membership using your Slack email. If the team knows you by another "
        f"email, link your account. Otherwise, ask a {org_name} admin to invite you."
    )
    blocks = _account_link_blocks(org_name, link_url)
    if is_dm:
        return bool(post_slack_message(web, channel, text, thread_ts, blocks))
    return post_slack_ephemeral(web, channel, slack_user_id, text, thread_ts, blocks)


def _load(token: str) -> tuple[str, dict] | None:
    token_hash = _sha256_hex(token)
    raw = kv().get(_state_key(token_hash))
    return (token_hash, json.loads(raw)) if raw else None


def _person_by_email(email: str) -> dict | None:
    _, graph = connect()
    rows = graph.query(
        "MATCH (p:Person) WHERE toLower(p.email) = $email RETURN p.id, p.name, coalesce(p.org_member, true) LIMIT 1",
        {"email": email.strip().lower()},
    ).result_set
    return {"id": rows[0][0], "name": rows[0][1], "org_member": bool(rows[0][2])} if rows else None


def _other_active_mapping(person_id: str, team_id: str, slack_user_id: str) -> bool:
    _, graph = connect()
    rows = graph.query(
        """
        MATCH (m:SlackWorkspaceMember {person_id: $person, status: 'active'})
        WHERE NOT (m.team_id = $team AND m.slack_user_id = $user)
        RETURN count(m)
        """,
        {"person": person_id, "team": team_id, "user": slack_user_id},
    ).result_set
    return bool(rows and rows[0][0])


@dataclass
class SlackAccountLinkPreview:
    status: str  # ready | expired | used | invalid
    team_id: str | None = None
    team_name: str | None = None
    slack_display_name: str | None = None
    slack_email: str | None = None


def get_slack_account_link_preview(token: str) -> SlackAccountLinkPreview:
    loaded = _load(token)
    if not loaded:
        return SlackAccountLinkPreview(status="invalid")
    _hash, state = loaded
    if state.get("consumed_at"):
        return SlackAccountLinkPreview(status="used")
    if state["expires_at"] <= int(time.time() * 1000):
        return SlackAccountLinkPreview(status="expired")
    row = get_workspace_row(state["team_id"]) or {}
    return SlackAccountLinkPreview(
        status="ready",
        team_id=state["team_id"],
        team_name=row.get("team_name"),
        slack_display_name=state.get("slack_display_name"),
        slack_email=state.get("slack_email"),
    )


@dataclass
class CompletedSlackAccountLink:
    ok: bool
    reason: str | None = None  # expired | used | invalid | not_in_org | claimed
    team_id: str | None = None
    slack_user_id: str | None = None
    person_id: str | None = None
    person_name: str | None = None


def complete_slack_account_link(token: str, person_email: str) -> CompletedSlackAccountLink:
    loaded = _load(token)
    if not loaded:
        return CompletedSlackAccountLink(ok=False, reason="invalid")
    token_hash, state = loaded
    if state.get("consumed_at"):
        return CompletedSlackAccountLink(ok=False, reason="used")
    now = int(time.time() * 1000)
    if state["expires_at"] <= now:
        return CompletedSlackAccountLink(ok=False, reason="expired")

    person = _person_by_email(person_email or "")
    if not person or not person["org_member"]:
        return CompletedSlackAccountLink(ok=False, reason="not_in_org")
    team_id, slack_user_id = state["team_id"], state["slack_user_id"]
    if _other_active_mapping(person["id"], team_id, slack_user_id):
        return CompletedSlackAccountLink(ok=False, reason="claimed")

    # Single use: claim the token before writing.
    state["consumed_at"] = now
    if not kv().set(_state_key(token_hash) + ":consumed", "1", nx=True, ex=ACCOUNT_LINK_TTL_MS // 1000 * 2):
        return CompletedSlackAccountLink(ok=False, reason="used")
    kv().set(_state_key(token_hash), json.dumps(state), keepttl=True)

    existing = get_workspace_member(team_id, slack_user_id)
    if existing and existing["person_id"] != person["id"] and existing.get("provisioned_person_id"):
        # The old Person was Slack-provisioned for this identity only: drop its
        # membership unless another active mapping still points at it.
        old_id = existing["provisioned_person_id"]
        if not _other_active_mapping(old_id, team_id, slack_user_id):
            _, graph = connect()
            graph.query(
                "MATCH (p:Person {id: $id}) WHERE p.provisioned_by = 'slack' SET p.org_member = false",
                {"id": old_id},
            )
    # A changed link targets an existing Person, so provisioning provenance must not transfer.
    provisioned = existing["provisioned_person_id"] if existing and existing["person_id"] == person["id"] else None
    _write_member(team_id, slack_user_id, person["id"], state.get("slack_email"), "active", "web_confirmed", provisioned)
    log.info("[slack] identity linked team=%s slack_user=%s person=%s source=web_confirmed", team_id, slack_user_id, person["id"])
    return CompletedSlackAccountLink(
        ok=True, team_id=team_id, slack_user_id=slack_user_id, person_id=person["id"], person_name=person["name"]
    )


def notify_slack_account_linked(web, *, slack_user_id: str, org_name: str) -> None:
    channel = open_slack_conversation(web, slack_user_id)
    if not channel:
        return
    post_slack_message(
        web,
        channel,
        f"Your Slack account is now linked to {org_name}. You can return to your conversation and try again.",
    )


def notify_installer_of_link(web, *, installer_slack_user_id: str | None, slack_user_id: str, person_name: str | None) -> None:
    """Audit trail for self-service claims."""
    if not installer_slack_user_id:
        return
    channel = open_slack_conversation(web, installer_slack_user_id)
    if channel:
        post_slack_message(web, channel, f"<@{slack_user_id}> linked their Slack account to {person_name or 'a person record'}.")
