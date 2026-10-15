"""Slack HTTP surface. Port of company-brain ``routes/slack/index.ts`` (events + interactions).

Slack needs an answer within 3 seconds, so every route verifies, dedupes, classifies,
returns ``{"ok": true}`` and hands the work to a background task.
"""

import json
import logging
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse, Response

from src.core.config import settings
from src.db.falkordb_client import kv
from src.domain.slack import channel_membership, turn_service
from src.domain.slack.workspace import get_workspace_by_team_id, upsert_slack_user
from src.integrations.slack.bot_address import collect_bot_address_aliases, is_bot_addressed_by_name
from src.integrations.slack.client import APPROVAL_APPROVE_ACTION_ID, APPROVAL_DENY_ACTION_ID, SlackUserInfo
from src.integrations.slack.events import (
    SlackEventInner,
    SlackUrlVerification,
    is_addressed_to_other_slack_user,
    is_agent_surface_event,
    is_answered_event,
    is_channel_membership_event,
    is_context_retention_event,
    is_debug_reaction_event,
    is_direct_message,
    is_mute_reaction_event,
    is_name_wake_candidate,
    parse_envelope,
    parse_workspace_user_event,
)
from src.integrations.slack.verify import is_slack_retry, verify_slack_signature

log = logging.getLogger(__name__)
router = APIRouter(prefix="/slack", tags=["slack"])

EVENT_DEDUP_TTL_SECONDS = 60 * 60 * 24


def _log_event(message: str, event: SlackEventInner, **extra) -> None:
    parts = [
        f"type={event.type}",
        f"subtype={event.subtype or '-'}",
        f"channel={event.channel or '?'}",
        f"thread={event.thread_ts or '-'}",
        f"user={event.user or '?'}",
        *(f"{key}={value}" for key, value in extra.items()),
    ]
    log.info("[slack] %s %s", message, " ".join(parts))


def _seen(key: str) -> bool:
    return bool(kv().exists(key))


def _mark_seen(key: str) -> None:
    kv().set(key, "1", ex=EVENT_DEDUP_TTL_SECONDS)


def _run_and_mark(key: str, fn, *args, **kwargs) -> None:
    """Only mark an event seen after it ran, so Slack's retry can redeliver after a failure."""
    try:
        fn(*args, **kwargs)
        _mark_seen(key)
    except Exception:
        log.exception("[slack] %s failed", getattr(fn, "__name__", "handler"))


@router.post("/events")
async def slack_events(request: Request, background: BackgroundTasks):
    raw_body = await request.body()
    try:
        body = json.loads(raw_body)
    except ValueError:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    if not settings.slack_signing_secret:
        # Slack checks this URL the moment the app is created from the manifest,
        # before the signing secret is in .env. Echoing the challenge reveals nothing.
        if body.get("type") == "url_verification" and isinstance(body.get("challenge"), str):
            return {"challenge": body["challenge"]}
        return JSONResponse({"error": "Slack is not configured yet"}, status_code=503)

    headers = {key.lower(): value for key, value in request.headers.items()}
    if not verify_slack_signature(raw_body, headers, settings.slack_signing_secret):
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    # team_join / user_change first: their nested user object fails the generic schema.
    workspace_user = parse_workspace_user_event(body)
    if workspace_user:
        team_id, event_id, _kind, user = workspace_user
        key = f"slack:evt:{event_id}"
        if _seen(key) or get_workspace_by_team_id(team_id) is None:
            return {"ok": True}
        info = SlackUserInfo(
            id=user.id,
            name=user.name,
            real_name=user.real_name,
            is_bot=bool(user.is_bot),
            is_restricted=bool(user.is_restricted or user.is_ultra_restricted or user.is_stranger or user.deleted),
        )
        background.add_task(_run_and_mark, key, upsert_slack_user, info)
        return {"ok": True}

    envelope = parse_envelope(body)
    if envelope is None:
        log.warning("[slack] envelope parse failed type=%s", body.get("type"))
        return {"ok": True}
    if isinstance(envelope, SlackUrlVerification):
        return {"challenge": envelope.challenge}

    team_id, event_id, event = envelope.team_id, envelope.event_id, envelope.event
    key = f"slack:evt:{event_id}"
    if _seen(key):
        _log_event("dedup skip", event, event_id=event_id)
        return {"ok": True}
    if is_slack_retry(headers):
        _log_event("retry", event, event_id=event_id)

    if is_agent_surface_event(event):
        _mark_seen(key)
        return {"ok": True}

    workspace = get_workspace_by_team_id(team_id)
    if workspace is None:
        _log_event("ignored", event, reason="workspace_not_connected")
        return {"ok": True}

    if is_debug_reaction_event(event) or is_mute_reaction_event(event):
        # Debug traces and mute arrive with the agent; acknowledge for now.
        _mark_seen(key)
        return {"ok": True}

    if is_channel_membership_event(event):
        _log_event("membership", event, event_id=event_id)
        background.add_task(
            _run_and_mark, key, channel_membership.run_slack_membership_event, workspace.client, workspace.bot_user_id, event
        )
        return {"ok": True}

    bot_user_id = workspace.bot_user_id
    addressed_elsewhere = not is_direct_message(event) and is_addressed_to_other_slack_user(event.text, bot_user_id)
    is_explicit_turn = not addressed_elsewhere and is_answered_event(event, bot_user_id)
    addressed_by_name = False
    if not addressed_elsewhere and not is_explicit_turn and is_name_wake_candidate(event, bot_user_id):
        aliases = collect_bot_address_aliases(workspace.bot_name, None, workspace.bot_name)
        if is_bot_addressed_by_name(event.text, aliases):
            is_explicit_turn = addressed_by_name = True
            _log_event("name wake", event, event_id=event_id)

    # Chime-in needs the agent's triage; until then those messages are kept as context.
    is_context_turn = not is_explicit_turn and is_context_retention_event(event, bot_user_id)
    if not is_explicit_turn and not is_context_turn:
        _log_event("ignored", event, event_id=event_id, reason="not_answered")
        return {"ok": True}

    # The same user message can arrive as several event types; lock on channel+ts per turn.
    if event.channel and event.ts:
        kind = "turn" if is_explicit_turn else "context"
        turn_key = f"slack:{kind}:{team_id}:{event.channel}:{event.ts}"
        if not kv().set(turn_key, "1", nx=True, ex=EVENT_DEDUP_TTL_SECONDS):
            _log_event("turn dedup skip", event, event_id=event_id)
            return {"ok": True}

    _log_event("dispatch" if is_explicit_turn else "context dispatch", event, event_id=event_id)
    if is_explicit_turn:
        background.add_task(_run_and_mark, key, turn_service.run_slack_turn, workspace, event, addressed_by_name)
    else:
        background.add_task(_run_and_mark, key, turn_service.run_slack_context_event, workspace, event)
    return {"ok": True}


def parse_interaction_payload(raw_body: bytes) -> dict | None:
    payload = parse_qs(raw_body.decode()).get("payload", [None])[0]
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or parsed.get("type") != "block_actions":
        return None
    action = (parsed.get("actions") or [{}])[0]
    action_id = action.get("action_id")
    approval_id = action.get("value")
    team_id = (parsed.get("team") or {}).get("id")
    user_id = (parsed.get("user") or {}).get("id")
    if not (team_id and user_id and approval_id and action_id):
        return None
    if action_id not in (APPROVAL_APPROVE_ACTION_ID, APPROVAL_DENY_ACTION_ID):
        return None
    return {
        "team_id": team_id,
        "user_id": user_id,
        "approval_id": approval_id,
        "approved": action_id == APPROVAL_APPROVE_ACTION_ID,
        "response_url": parsed.get("response_url"),
    }


@router.post("/interactions")
async def slack_interactions(request: Request, background: BackgroundTasks):
    raw_body = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}
    if not verify_slack_signature(raw_body, headers, settings.slack_signing_secret):
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    approval = parse_interaction_payload(raw_body)
    if approval:
        background.add_task(
            turn_service.run_slack_approval_decision,
            approval["team_id"],
            approval["approval_id"],
            approval["approved"],
            approval["user_id"],
            approval["response_url"],
        )
        return {"ok": True}
    return Response(status_code=200)
