"""The installed Slack workspace and who people are in it.

Single-workspace port of company-brain ``brain/slack/workspace.ts`` and the identity
rules in ``member-provisioning.ts``: the bot token comes from the environment
instead of an encrypted OAuth row. Identity follows theirs: a stable map from
Slack user id to a Person, bootstrapped once by email match, then durable. Bots,
guests (restricted users) and external users are not members.
"""

import json
import logging
from dataclasses import dataclass
from functools import lru_cache

from src.core.config import settings
from src.db.falkordb_client import connect, kv
from src.integrations.slack.client import SlackClient, SlackUserInfo

log = logging.getLogger(__name__)

_BOT_IDENTITY_KEY = "slack:bot-identity"


@dataclass(frozen=True)
class SlackWorkspace:
    team_id: str
    bot_user_id: str | None
    bot_name: str | None
    client: SlackClient


@dataclass(frozen=True)
class SlackActor:
    slack_user_id: str
    name: str
    person_id: str | None
    is_member: bool


@lru_cache(maxsize=1)
def _client() -> SlackClient:
    return SlackClient(settings.slack_bot_token)


def get_workspace_by_team_id(team_id: str) -> SlackWorkspace | None:
    if not settings.slack_enabled:
        return None
    identity = bot_identity()
    if not identity:
        return None
    if identity["team_id"] != team_id:
        log.warning("[slack] event for team=%s but the bot token belongs to team=%s", team_id, identity["team_id"])
        return None
    return SlackWorkspace(
        team_id=team_id,
        bot_user_id=identity.get("user_id"),
        bot_name=identity.get("user"),
        client=_client(),
    )


def bot_identity() -> dict | None:
    cached = kv().get(_BOT_IDENTITY_KEY)
    if cached:
        return json.loads(cached)
    try:
        response = _client().web.auth_test()
    except Exception:
        log.exception("[slack] auth.test failed; check SLACK_BOT_TOKEN")
        return None
    identity = {"team_id": response.get("team_id"), "user_id": response.get("user_id"), "user": response.get("user")}
    kv().set(_BOT_IDENTITY_KEY, json.dumps(identity), ex=24 * 60 * 60)
    return identity


def is_workspace_member(user: SlackUserInfo) -> bool:
    return not user.is_bot and not user.is_restricted


def upsert_slack_user(user: SlackUserInfo) -> SlackActor:
    """Write the SlackUser node and link it to a Person, matching by email the first time."""
    _, graph = connect()
    name = user.real_name or user.display_name or user.name or user.id
    graph.query(
        """
        MERGE (u:SlackUser {id: $id})
        SET u.name = $name, u.email = $email, u.is_member = $member
        """,
        {"id": user.id, "name": name, "email": user.email, "member": is_workspace_member(user)},
    )
    linked = graph.query("MATCH (u:SlackUser {id: $id})-[:IS]->(p:Person) RETURN p.id", {"id": user.id}).result_set
    person_id = linked[0][0] if linked else None
    if person_id is None and user.email:
        rows = graph.query(
            """
            MATCH (p:Person) WHERE toLower(p.email) = toLower($email)
            MATCH (u:SlackUser {id: $id})
            MERGE (u)-[:IS]->(p)
            RETURN p.id
            """,
            {"id": user.id, "email": user.email},
        ).result_set
        person_id = rows[0][0] if rows else None
    return SlackActor(slack_user_id=user.id, name=name, person_id=person_id, is_member=is_workspace_member(user))


def resolve_actor(workspace: SlackWorkspace, slack_user_id: str) -> SlackActor | None:
    """None means the profile lookup failed; callers should not guess."""
    info = workspace.client.user_info(slack_user_id)
    if info is None:
        return None
    return upsert_slack_user(info)


def format_slack_org_member_denial() -> str:
    return "I can only help full members of this workspace. Guests and external users don't have access to the company brain."


def format_slack_profile_lookup_failure() -> str:
    return "I couldn't verify your Slack profile just now, so I didn't run that. Try again in a minute."
