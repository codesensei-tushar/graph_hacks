"""Installed Slack workspaces, who people are in them, and how an install is torn down.

Port of company-brain `brain/slack/workspace.ts` and `brain/turn/home-channel.ts`.

Their Postgres rows become FalkorDB nodes:

    (:SlackWorkspace {team_id, team_name, bot_user_id, bot_token_enc, installed_by,
                      scopes, app_id, home_channel_id, source, created_at, updated_at})
    (:SlackWorkspaceMember {id: "<team>:<user>", team_id, slack_user_id, person_id,
                            email, status, link_source, provisioned_person_id})
        -[:LINKS]->(:Person)
    (:SlackUser {id})-[:IS]->(:Person)          mirror of every active mapping

Their "organization" is the one company graph here, and an "org member" is a
Person (``org_member`` true or unset). Identity follows theirs: a stable map from
(team, Slack user) to a Person, bootstrapped once by email, then durable; a
web-confirmed link is never overwritten by an email match.

Single-workspace mode still works: with ``SLACK_BOT_TOKEN`` set, a workspace with
no stored install is synthesized from auth.test. That is the legacy org's only: every
function here works on the graph of the org in context (``tenancy.org_context``), and
another org's workspaces are always stored OAuth installs.
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from src.core import tenancy
from src.core.config import settings
from src.db.falkordb_client import connect, kv
from src.domain.slack import identity_lock
from src.domain.slack.lifecycle_api import (
    get_bot_conversations,
    post_slack_message_idempotent,
    uninstall_slack_app,
)
from src.domain.slack.token_crypto import decrypt_token
from src.integrations.slack.client import SlackClient, SlackUserInfo

log = logging.getLogger(__name__)

_BOT_IDENTITY_KEY = "slack:bot-identity"

SlackMemberLookup = str  # "found" | "no_slack_email" | "not_in_org"
# "mapped" | "email_matched" | "mapped_not_in_org" | "no_slack_email" | "not_in_org" | "provisioned"
SlackActorLookup = str


@dataclass(frozen=True)
class SlackWorkspace:
    team_id: str
    bot_user_id: str | None
    bot_name: str | None
    client: SlackClient
    team_name: str | None = None
    installed_by: str | None = None
    home_channel_id: str | None = None
    scopes: str | None = None
    # "oauth" for a stored install, "env" for SLACK_BOT_TOKEN single-workspace mode.
    source: str = "env"


@dataclass(frozen=True)
class SlackActor:
    slack_user_id: str
    name: str
    person_id: str | None
    is_member: bool
    lookup: SlackActorLookup = "mapped"


@dataclass(frozen=True)
class SlackOrgActor:
    person_id: str


@dataclass(frozen=True)
class SlackActorResolution:
    actor: SlackOrgActor | None
    lookup: SlackActorLookup


# --- display helpers ---------------------------------------------------------------


def _display_value(value: str | None) -> str | None:
    trimmed = " ".join((value or "").split())
    return trimmed or None


def escape_slack_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_display_value(value: str | None) -> str | None:
    displayed = _display_value(value)
    return escape_slack_text(displayed) if displayed else None


def slack_user_display_name(user) -> str | None:
    if user is None:
        return None
    return (
        _display_value(getattr(user, "display_name", None))
        or _display_value(getattr(user, "name", None))
        or _display_value(getattr(user, "real_name", None))
        or _display_value(getattr(user, "handle", None))
    )


def should_post_slack_org_member_denial(
    *,
    force_full_turn: bool = False,
    is_mention: bool = False,
    is_name_addressed: bool = False,
    is_dm: bool = False,
    is_assistant_thread: bool = False,
) -> bool:
    return bool(force_full_turn or is_mention or is_name_addressed or is_dm or is_assistant_thread)


def format_slack_profile_lookup_failure() -> str:
    return "I couldn't verify your Slack profile right now. Please try again in a moment."


def format_slack_org_member_denial(person_name: str | None = None, org_name: str | None = None) -> str:
    name = _slack_display_value(person_name)
    greeting = f"Hey {name}," if name else "Hey there,"
    org = _slack_display_value(org_name)
    org_label = org or "this workspace"
    admin_label = f"a {org} admin" if org else "a workspace admin"
    return (
        f"{greeting} I couldn't verify your {org_label} membership. Company Brain only answers full members of "
        f"this workspace; guests and external users don't have access. If your Slack email differs from the one "
        f"the team knows you by, DM me `link my account`. Otherwise, ask {admin_label} to invite you."
    )


# --- identity ----------------------------------------------------------------------


def _member_key(team_id: str, slack_user_id: str) -> str:
    return f"{team_id}:{slack_user_id}"


def get_org_actor_by_email(email: str | None) -> SlackOrgActor | None:
    """Map a Slack member's email to their Person, when that Person is an org member."""
    normalized = (email or "").strip().lower()
    if not normalized:
        return None
    _, graph = connect()
    rows = graph.query(
        """
        MATCH (p:Person)
        WHERE toLower(p.email) = $email AND coalesce(p.org_member, true) = true
        RETURN p.id LIMIT 1
        """,
        {"email": normalized},
    ).result_set
    return SlackOrgActor(person_id=rows[0][0]) if rows else None


def get_workspace_member(team_id: str, slack_user_id: str) -> dict | None:
    _, graph = connect()
    rows = graph.query(
        """
        MATCH (m:SlackWorkspaceMember {id: $id})
        RETURN m.person_id, m.status, m.link_source, m.provisioned_person_id, m.email
        """,
        {"id": _member_key(team_id, slack_user_id)},
    ).result_set
    if not rows:
        return None
    person_id, status, link_source, provisioned, email = rows[0]
    return {
        "person_id": person_id,
        "status": status,
        "link_source": link_source,
        "provisioned_person_id": provisioned,
        "email": email,
    }


def _write_member(
    team_id: str,
    slack_user_id: str,
    person_id: str,
    email: str | None,
    status: str,
    link_source: str,
    provisioned_person_id: str | None,
) -> None:
    _, graph = connect()
    graph.query(
        """
        MERGE (m:SlackWorkspaceMember {id: $id})
        SET m.team_id = $team, m.slack_user_id = $user, m.person_id = $person, m.email = $email,
            m.status = $status, m.link_source = $source, m.provisioned_person_id = $provisioned,
            m.updated_at = $now
        WITH m
        OPTIONAL MATCH (m)-[old:LINKS]->(:Person)
        DELETE old
        WITH m
        MATCH (p:Person {id: $person})
        MERGE (m)-[:LINKS]->(p)
        MERGE (u:SlackUser {id: $user})
        WITH u, p
        OPTIONAL MATCH (u)-[stale:IS]->(other:Person) WHERE other.id <> p.id
        DELETE stale
        WITH u, p
        MERGE (u)-[:IS]->(p)
        """,
        {
            "id": _member_key(team_id, slack_user_id),
            "team": team_id,
            "user": slack_user_id,
            "person": person_id,
            "email": (email or "").strip().lower() or None,
            "status": status,
            "source": link_source,
            "provisioned": provisioned_person_id,
            "now": int(time.time() * 1000),
        },
    )
    if status != "active":
        graph.query("MATCH (:SlackUser {id: $user})-[r:IS]->(:Person) DELETE r", {"user": slack_user_id})


def upsert_slack_workspace_member(
    *, team_id: str, slack_user_id: str, person_id: str, email: str | None, link_source: str
) -> None:
    """``link_source`` is ``email_match`` or ``web_confirmed``; a confirmed link always wins."""
    identity_lock.slack_identity_advisory_lock()
    existing = get_workspace_member(team_id, slack_user_id)
    confirmed = existing is not None and existing["link_source"] == "web_confirmed"
    _write_member(
        team_id,
        slack_user_id,
        existing["person_id"] if confirmed else person_id,
        email,
        "active",
        "web_confirmed" if confirmed else link_source,
        existing["provisioned_person_id"] if existing else None,
    )


def get_org_actor_by_slack_identity(team_id: str, slack_user_id: str, email: str | None = None) -> SlackActorResolution:
    """Stable Slack identity wins. Email is only a bootstrap, upgraded to a durable mapping."""
    if not (slack_user_id or "").strip():
        return SlackActorResolution(actor=None, lookup="no_slack_email")

    mapping = get_workspace_member(team_id, slack_user_id)
    if mapping:
        if mapping["status"] != "active" or not _person_is_org_member(mapping["person_id"]):
            return SlackActorResolution(actor=None, lookup="mapped_not_in_org")
        return SlackActorResolution(actor=SlackOrgActor(person_id=mapping["person_id"]), lookup="mapped")

    actor = get_org_actor_by_email(email)
    if actor is None:
        return SlackActorResolution(actor=None, lookup="not_in_org" if (email or "").strip() else "no_slack_email")
    upsert_slack_workspace_member(
        team_id=team_id, slack_user_id=slack_user_id, person_id=actor.person_id, email=email, link_source="email_match"
    )
    return SlackActorResolution(actor=actor, lookup="email_matched")


def _person_is_org_member(person_id: str | None) -> bool:
    if not person_id:
        return False
    _, graph = connect()
    rows = graph.query(
        "MATCH (p:Person {id: $id}) RETURN coalesce(p.org_member, true)",
        {"id": person_id},
    ).result_set
    return bool(rows and rows[0][0])


def is_workspace_member(user: SlackUserInfo) -> bool:
    """Bots, guests, external (Slack Connect) and deactivated users are not members."""
    return not (
        getattr(user, "deleted", False)
        or getattr(user, "is_bot", False)
        or getattr(user, "is_restricted", False)
        or getattr(user, "is_ultra_restricted", False)
        or getattr(user, "is_stranger", False)
    )


def upsert_slack_user(user: SlackUserInfo, team_id: str | None = None) -> SlackActor:
    """Write the SlackUser node and resolve its Person through the workspace mapping."""
    _, graph = connect()
    name = user.real_name or user.display_name or user.name or user.id
    member = is_workspace_member(user)
    graph.query(
        "MERGE (u:SlackUser {id: $id}) SET u.name = $name, u.email = $email, u.is_member = $member",
        {"id": user.id, "name": name, "email": user.email, "member": member},
    )
    team = team_id or _default_team_id()
    lookup = "not_in_org"
    person_id = None
    if team:
        resolution = get_org_actor_by_slack_identity(team, user.id, user.email)
        lookup = resolution.lookup
        person_id = resolution.actor.person_id if resolution.actor else None
        if resolution.lookup == "mapped_not_in_org":
            member = False
    if person_id is None and member and lookup != "mapped_not_in_org":
        linked = graph.query("MATCH (u:SlackUser {id: $id})-[:IS]->(p:Person) RETURN p.id", {"id": user.id}).result_set
        person_id = linked[0][0] if linked else None
    return SlackActor(slack_user_id=user.id, name=name, person_id=person_id, is_member=member, lookup=lookup)


def resolve_actor(workspace: SlackWorkspace, slack_user_id: str) -> SlackActor | None:
    """None means the profile lookup failed; callers should not guess.

    A full member with no mapping and no email match is provisioned as a Person on
    first contact, the same thing company-brain's install rollout and team_join do
    for every full member ahead of time.
    """
    info = workspace.client.user_info(slack_user_id)
    if info is None:
        return None
    actor = upsert_slack_user(info, workspace.team_id)
    if actor.person_id is None and actor.is_member and actor.lookup in ("not_in_org",) and info.email:
        from src.domain.slack.member_provisioning import provision_slack_workspace_member

        try:
            provisioned = provision_slack_workspace_member(
                team_id=workspace.team_id, slack_user_id=info.id, email=info.email, name=actor.name
            )
            return SlackActor(
                slack_user_id=actor.slack_user_id,
                name=actor.name,
                person_id=provisioned.person_id,
                is_member=True,
                lookup="provisioned",
            )
        except Exception:
            log.exception("[slack] provisioning on first contact failed user=%s", slack_user_id)
    return actor


# --- workspaces --------------------------------------------------------------------


@lru_cache(maxsize=64)
def _client_for(token: str) -> SlackClient:
    return SlackClient(token)


def _env_mode() -> bool:
    """Single-workspace mode (``SLACK_BOT_TOKEN``) applies to the legacy org only."""
    return settings.slack_enabled and tenancy.is_legacy()


def _env_client() -> SlackClient:
    return _client_for(settings.slack_bot_token)


def bot_identity() -> dict | None:
    """auth.test for the env bot token (single-workspace mode), cached for a day."""
    if not settings.slack_bot_token:
        return None
    cached = kv().get(_BOT_IDENTITY_KEY)
    if cached:
        return json.loads(cached)
    try:
        response = _env_client().web.auth_test()
    except Exception:
        log.exception("[slack] auth.test failed; check SLACK_BOT_TOKEN")
        return None
    identity = {
        "team_id": response.get("team_id"),
        "team": response.get("team"),
        "user_id": response.get("user_id"),
        "user": response.get("user"),
    }
    kv().set(_BOT_IDENTITY_KEY, json.dumps(identity), ex=24 * 60 * 60)
    return identity


def _default_team_id() -> str | None:
    rows = list_workspace_rows()
    if rows:
        return rows[0]["team_id"]
    identity = bot_identity()
    return identity.get("team_id") if identity else None


_FIELDS = ("team_id", "team_name", "bot_user_id", "bot_token_enc", "installed_by", "scopes", "app_id", "home_channel_id", "source", "created_at")


def list_workspace_rows() -> list[dict]:
    _, graph = connect()
    rows = graph.query(
        f"MATCH (w:SlackWorkspace) RETURN {', '.join('w.' + f for f in _FIELDS)} ORDER BY w.team_name, w.team_id"
    ).result_set
    return [dict(zip(_FIELDS, row, strict=True)) for row in rows]


def get_workspace_row(team_id: str) -> dict | None:
    _, graph = connect()
    rows = graph.query(
        f"MATCH (w:SlackWorkspace {{team_id: $team}}) RETURN {', '.join('w.' + f for f in _FIELDS)}",
        {"team": team_id},
    ).result_set
    return dict(zip(_FIELDS, rows[0], strict=True)) if rows else None


def _workspace_from_row(row: dict) -> SlackWorkspace | None:
    if row.get("bot_token_enc"):
        try:
            token = decrypt_token(row["bot_token_enc"])
        except Exception:
            log.exception("[slack] bot token decrypt failed team=%s", row["team_id"])
            return None
        client, source = _client_for(token), "oauth"
    elif settings.slack_bot_token and tenancy.is_legacy():
        client, source = _env_client(), "env"
    else:
        return None
    return SlackWorkspace(
        team_id=row["team_id"],
        bot_user_id=row.get("bot_user_id"),
        bot_name=None,
        client=client,
        team_name=row.get("team_name"),
        installed_by=row.get("installed_by"),
        home_channel_id=row.get("home_channel_id"),
        scopes=row.get("scopes"),
        source=source,
    )


def get_workspace_by_team_id(team_id: str) -> SlackWorkspace | None:
    row = get_workspace_row(team_id)
    if row is not None and row.get("bot_token_enc"):
        return _workspace_from_row(row)

    # Single-workspace mode: the env token's own team.
    if not _env_mode():
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
        client=_env_client(),
        team_name=(row or {}).get("team_name") or identity.get("team"),
        installed_by=(row or {}).get("installed_by") or settings.slack_owner_id or None,
        home_channel_id=(row or {}).get("home_channel_id"),
        source="env",
    )


def list_connected_workspaces() -> list[SlackWorkspace]:
    out = [w for w in (_workspace_from_row(row) for row in list_workspace_rows() if row.get("bot_token_enc")) if w]
    if _env_mode():
        identity = bot_identity()
        if identity and all(w.team_id != identity["team_id"] for w in out):
            env_ws = get_workspace_by_team_id(identity["team_id"])
            if env_ws:
                out.append(env_ws)
    return out


def list_all_connected_workspaces() -> list[tuple[tenancy.OrgRef, SlackWorkspace]]:
    """Every org's connected workspaces, each with its org: the legacy org's, then each other org's
    (startup recovery). An org whose graph cannot be read is logged and skipped."""
    from src.domain import orgs

    refs = [tenancy.legacy_org()]
    try:
        refs += [org.ref() for org in orgs.list_orgs() if not org.legacy]
    except Exception:
        log.exception("[slack] listing orgs failed; recovering the legacy org only")
    found = []
    for ref in refs:
        try:
            with tenancy.org_context(ref):
                found += [(ref, workspace) for workspace in list_connected_workspaces()]
        except Exception:
            log.exception("[slack] listing workspaces failed org=%s", ref.id)
    return found


def get_workspace_status() -> dict:
    """Connection status for the install card: ``{connected, team_name}``."""
    workspaces = list_connected_workspaces()
    return {"connected": bool(workspaces), "team_name": workspaces[0].team_name if workspaces else None}


def list_connected_slack_installs() -> list[dict]:
    return [
        {"team_id": row["team_id"], "team_name": row["team_name"], "bot_user_id": row["bot_user_id"], "installed_at": row["created_at"]}
        for row in list_workspace_rows()
    ]


def upsert_workspace(
    *,
    team_id: str,
    bot_token_enc: str | None,
    bot_user_id: str | None = None,
    team_name: str | None = None,
    installed_by: str | None = None,
    scopes: str | None = None,
    app_id: str | None = None,
    source: str = "oauth",
) -> None:
    identity_lock.slack_workspace_advisory_lock()
    now = int(time.time() * 1000)
    _, graph = connect()
    graph.query(
        """
        MERGE (w:SlackWorkspace {team_id: $team})
        ON CREATE SET w.created_at = $now
        SET w.team_name = $name, w.bot_token_enc = $token, w.bot_user_id = $bot,
            w.installed_by = $installer, w.scopes = $scopes, w.app_id = $app,
            w.source = $source, w.updated_at = $now
        """,
        {
            "team": team_id,
            "name": team_name,
            "token": bot_token_enc,
            "bot": bot_user_id,
            "installer": installed_by,
            "scopes": scopes,
            "app": app_id,
            "source": source,
            "now": now,
        },
    )


def ensure_workspace_bot_user_id(workspace: SlackWorkspace, prefetched_user_id: str | None = None) -> str | None:
    """Backfill the bot user id for installs stored before it was persisted."""
    if workspace.bot_user_id:
        return workspace.bot_user_id
    bot_user_id = prefetched_user_id or workspace.client.bot_user_id()
    if not bot_user_id:
        return None
    _, graph = connect()
    graph.query(
        "MATCH (w:SlackWorkspace {team_id: $team}) SET w.bot_user_id = $bot, w.updated_at = $now",
        {"team": workspace.team_id, "bot": bot_user_id, "now": int(time.time() * 1000)},
    )
    return bot_user_id


# --- home channel (port of turn/home-channel.ts) ------------------------------------


def set_home_channel(team_id: str, channel_id: str) -> None:
    _, graph = connect()
    graph.query(
        """
        MERGE (w:SlackWorkspace {team_id: $team})
        ON CREATE SET w.created_at = $now, w.source = 'env'
        SET w.home_channel_id = $channel, w.updated_at = $now
        """,
        {"team": team_id, "channel": channel_id, "now": int(time.time() * 1000)},
    )


def get_home_channel(team_id: str) -> dict | None:
    row = get_workspace_row(team_id)
    if not row or not row.get("home_channel_id"):
        return None
    return {"channel_id": row["home_channel_id"], "team_id": team_id}


# --- teardown ----------------------------------------------------------------------

# Modules that keep Slack state register here so a disconnect clears it
# (company-brain's `resetSlackWorkspaceState` on the agent).
_reset_hooks: list[Callable[[str], None]] = []


def register_workspace_reset_hook(hook: Callable[[str], None]) -> None:
    if hook not in _reset_hooks:
        _reset_hooks.append(hook)


def reset_slack_workspace_state(team_id: str) -> None:
    """Clears home channel, team-invite runs and Slack-owned schedules. Raises on failure."""
    for hook in list(_reset_hooks):
        hook(team_id)
    _, graph = connect()
    graph.query("MATCH (w:SlackWorkspace {team_id: $team}) SET w.home_channel_id = NULL", {"team": team_id})
    kv().delete(_BOT_IDENTITY_KEY)


def _rows_for_teardown(team_id: str | None) -> list[dict]:
    rows = [row for row in list_workspace_rows() if team_id is None or row["team_id"] == team_id]
    if not any(row.get("bot_token_enc") for row in rows) and _env_mode():
        identity = bot_identity()
        if identity and (team_id is None or identity["team_id"] == team_id):
            existing = next((row for row in rows if row["team_id"] == identity["team_id"]), None)
            if existing is None:
                rows.append({"team_id": identity["team_id"], "bot_token_enc": None, "home_channel_id": None})
    return rows


def _web_for_row(row: dict):
    if row.get("bot_token_enc"):
        return _client_for(decrypt_token(row["bot_token_enc"])).web
    return _env_client().web


@dataclass
class SlackAnnounceResult:
    outcome: str  # done | hold | not_connected
    posted: int = 0
    already_posted: int = 0
    skipped: int = 0


def announce_slack_workspace_farewell(kind: str, text: str, team_id: str | None = None) -> SlackAnnounceResult:
    """Runs before teardown so the token and home-channel id are still available."""
    from src.domain.slack.farewell import SlackFarewellDeps, post_slack_farewell

    rows = _rows_for_teardown(team_id)
    if not rows:
        return SlackAnnounceResult(outcome="not_connected")
    deps = SlackFarewellDeps(
        post_message_idempotent=post_slack_message_idempotent,
        list_bot_conversations=lambda web: get_bot_conversations(web, 1000, True),
    )
    result = SlackAnnounceResult(outcome="done")
    for row in rows:
        try:
            web = _web_for_row(row)
        except Exception:
            log.warning("[slack] farewell decrypt failed team=%s", row["team_id"])
            result.skipped += 1
            continue
        outcome = post_slack_farewell(
            web=web,
            team_id=row["team_id"],
            kind=kind,
            text=text,
            known_home_channel_id=row.get("home_channel_id"),
            deps=deps,
        )
        if outcome == "hold":
            result.outcome = "hold"
            return result
        if outcome == "posted":
            result.posted += 1
        elif outcome == "already_posted":
            result.already_posted += 1
        else:
            result.skipped += 1
    log.info(
        "[slack] farewell kind=%s posted=%d already_posted=%d skipped=%d",
        kind, result.posted, result.already_posted, result.skipped,
    )
    return result


@dataclass
class SlackTeardownResult:
    disconnected: bool
    revoked: bool
    reason: str | None = None  # not_connected | cleanup_failed


def _unroute(team_id: str) -> None:
    """A removed workspace stops routing to its org (the legacy org's teams need no route)."""
    if tenancy.is_legacy():
        return
    from src.domain import orgs

    try:
        orgs.unroute_team(team_id)
    except Exception:
        log.exception("[slack] removing the workspace route failed team=%s", team_id)


def disconnect_slack_workspace(team_id: str | None = None) -> SlackTeardownResult:
    """A held farewell fails before teardown so the retry can still post it."""
    from src.domain.slack.home_welcome import company_brain_disconnect_message

    announced = announce_slack_workspace_farewell("bye", company_brain_disconnect_message(), team_id)
    if announced.outcome == "not_connected":
        return SlackTeardownResult(disconnected=False, revoked=False, reason="not_connected")
    if announced.outcome == "hold":
        return SlackTeardownResult(disconnected=False, revoked=False, reason="cleanup_failed")
    return teardown_slack_workspace(team_id)


def teardown_slack_workspace(team_id: str | None = None, uninstall=uninstall_slack_app) -> SlackTeardownResult:
    """State clears before any node is deleted so a failure stays retryable."""
    rows = _rows_for_teardown(team_id)
    if not rows:
        return SlackTeardownResult(disconnected=False, revoked=False, reason="not_connected")

    for row in rows:
        try:
            reset_slack_workspace_state(row["team_id"])
        except Exception:
            log.exception("[slack] state reset failed team=%s, leaving workspace intact", row["team_id"])
            return SlackTeardownResult(disconnected=False, revoked=False, reason="cleanup_failed")

    revoked = True
    removed = pending = 0
    replaced = False
    _, graph = connect()
    for row in rows:
        try:
            outcome = uninstall(_web_for_row(row))
        except Exception:
            log.warning("[slack] uninstall failed team=%s", row["team_id"], exc_info=True)
            outcome = "terminal"
        if outcome == "transient":
            pending += 1
            revoked = False
            continue
        revoked = revoked and outcome == "revoked"

        # Delete only the install we revoked: a reinstall meanwhile stores a new token.
        deleted = graph.query(
            """
            MATCH (w:SlackWorkspace {team_id: $team})
            WHERE coalesce(w.bot_token_enc, '') = coalesce($token, '')
            WITH w, w.team_id AS team DETACH DELETE w RETURN count(team)
            """,
            {"team": row["team_id"], "token": row.get("bot_token_enc")},
        ).result_set
        gone = int(deleted[0][0]) if deleted else 0
        survivor = get_workspace_row(row["team_id"])
        if gone == 0 and survivor is not None:
            replaced = True
            continue
        graph.query("MATCH (m:SlackWorkspaceMember {team_id: $team}) DETACH DELETE m", {"team": row["team_id"]})
        _unroute(row["team_id"])
        removed += 1

    if pending or replaced:
        log.warning("[slack] disconnect incomplete removed=%d pending=%d replaced=%s", removed, pending, replaced)
        return SlackTeardownResult(disconnected=False, revoked=False, reason="cleanup_failed")
    log.info("[slack] disconnected installations=%d", removed)
    return SlackTeardownResult(disconnected=True, revoked=revoked)
