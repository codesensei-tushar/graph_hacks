"""Port of company-brain `routes/slack/index.ts` (events + interactions).

Slack needs an answer within 3 seconds, so every route verifies, dedupes, classifies,
returns ``{"ok": true}`` and hands the work to a background task. Install, OAuth,
status, disconnect and account-link routes live in ``slack_install.py``.

Event dispatch, in upstream order:

| Kind                         | Handler                                         |
|------------------------------|-------------------------------------------------|
| team_join / user_change      | ``team_invite.on_slack_team_join`` / ``on_slack_user_change`` |
| agent surface events         | acknowledged                                    |
| debug / mute reactions       | ``turn_service.run_slack_debug_reaction`` / ``run_slack_mute_reaction`` |
| channel membership           | ``channel_membership.run_slack_membership_event`` |
| explicit (DM, @mention, thread, plain-name wake) | ``turn_service.run_slack_turn`` |
| top-level chime candidate    | ``chime.run_slack_chime_in``                    |
| other messages               | ``turn_service.run_slack_context_event``        |

Every event and interaction runs in the org its Slack team is routed to (a ``WorkspaceRoute``
in the control graph, cached for a minute), resolved before any graph call; a team with no
route is the legacy org's, as every workspace installed before orgs is. Background tasks run
after the request's context is gone, so each is handed its org (``tenancy.in_org``).
"""

import json
import logging
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse, Response

from src.core.config import settings
from src.core.tenancy import OrgRef, in_org, org_context
from src.db.falkordb_client import kv
from src.domain import orgs
from src.domain.slack import channel_membership, turn_service
from src.domain.slack.channels import rollout
from src.domain.slack.team_invite import (
    on_slack_team_join,
    on_slack_user_change,
    parse_team_invite_interaction_payload,
    start_team_invite_wave,
)
from src.domain.slack.turn import chime
from src.domain.slack.workspace import get_workspace_by_team_id
from src.integrations.slack.bot_address import collect_bot_address_aliases, is_bot_addressed_by_name
from src.integrations.slack.client import APPROVAL_APPROVE_ACTION_ID, APPROVAL_DENY_ACTION_ID
from src.integrations.slack.events import (
    SlackEventInner,
    SlackUrlVerification,
    is_addressed_to_other_slack_user,
    is_agent_surface_event,
    is_answered_event,
    is_channel_membership_event,
    is_chime_in_event,
    is_context_retention_event,
    is_debug_reaction_event,
    is_direct_message,
    is_mute_reaction_event,
    is_name_wake_candidate,
    parse_envelope,
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


def _run_team_join(key: str, team_id: str, user: dict) -> None:
    # Only dedup terminal outcomes so Slack's retries can redeliver after transient failures.
    try:
        if on_slack_team_join(team_id, user):
            _mark_seen(key)
    except Exception:
        log.exception("[slack] on_slack_team_join failed")


class _OrgTasks:
    """``BackgroundTasks`` whose every task runs in ``org``."""

    def __init__(self, background: BackgroundTasks, org: OrgRef) -> None:
        self.background, self.org = background, org

    def add_task(self, fn, *args, **kwargs) -> None:
        self.background.add_task(in_org, self.org, fn, *args, **kwargs)


def _org_for_team(team_id: str | None) -> OrgRef:
    return orgs.org_ref_for_team(team_id)


def _workspace_user_event(body: dict) -> tuple[str, str, str, dict] | None:
    if body.get("type") != "event_callback":
        return None
    event = body.get("event") or {}
    if event.get("type") not in ("team_join", "user_change") or not isinstance(event.get("user"), dict):
        return None
    return body.get("team_id") or "", body.get("event_id") or "", event["type"], event["user"]


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
    workspace_user = _workspace_user_event(body)
    if workspace_user:
        team_id, event_id, kind, user = workspace_user
        key = f"slack:evt:{event_id}"
        if _seen(key):
            return {"ok": True}
        org = _org_for_team(team_id)
        tasks = _OrgTasks(background, org)
        with org_context(org):
            if get_workspace_by_team_id(team_id) is None:
                return {"ok": True}
        if kind == "team_join":
            tasks.add_task(_run_team_join, key, team_id, user)
        else:
            tasks.add_task(_run_and_mark, key, on_slack_user_change, team_id, user)
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

    org = _org_for_team(team_id)
    with org_context(org):
        return _dispatch_event(_OrgTasks(background, org), team_id, event_id, event, key)


def _dispatch_event(background: _OrgTasks, team_id: str, event_id: str, event: SlackEventInner, key: str) -> dict:
    """Everything after dedupe, in the team's org."""
    workspace = get_workspace_by_team_id(team_id)
    if workspace is None:
        _log_event("ignored", event, reason="workspace_not_connected")
        return {"ok": True}

    if is_debug_reaction_event(event):
        background.add_task(_run_and_mark, key, turn_service.run_slack_debug_reaction, workspace, event)
        return {"ok": True}
    if is_mute_reaction_event(event):
        background.add_task(_run_and_mark, key, turn_service.run_slack_mute_reaction, workspace, event)
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

    is_chime_turn = not addressed_elsewhere and not is_explicit_turn and is_chime_in_event(event, bot_user_id)
    is_context_turn = is_context_retention_event(event, bot_user_id)
    if not is_explicit_turn and not is_chime_turn and not is_context_turn:
        _log_event("ignored", event, event_id=event_id, reason="not_answered")
        return {"ok": True}

    # The same user message can race in via several event types; lock on channel+ts per turn.
    if event.channel and event.ts:
        kind = "chime" if is_chime_turn else "turn" if is_explicit_turn else "context"
        turn_key = f"slack:{kind}:{team_id}:{event.channel}:{event.ts}"
        if not kv().set(turn_key, "1", nx=True, ex=EVENT_DEDUP_TTL_SECONDS):
            _log_event("turn dedup skip", event, event_id=event_id, turn_key=turn_key)
            return {"ok": True}

    if is_explicit_turn:
        _log_event("dispatch", event, event_id=event_id, routing="directed_to_other_user" if addressed_elsewhere else "-")
        background.add_task(_run_and_mark, key, turn_service.run_slack_turn, workspace, event, addressed_by_name)
    elif is_chime_turn:
        _log_event("chime dispatch", event, event_id=event_id)
        background.add_task(_run_and_mark, key, _run_chime, workspace, event)
    else:
        _log_event("context dispatch", event, event_id=event_id)
        background.add_task(_run_and_mark, key, turn_service.run_slack_context_event, workspace, event)
    return {"ok": True}


def _run_chime(workspace, event: SlackEventInner) -> None:
    turn_service.retain_without_model(workspace, event)
    chime.run_slack_chime_in(workspace, event, run_turn=turn_service.run_turn)


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


def _decoded_payload(raw_body: bytes) -> dict | None:
    payload = parse_qs(raw_body.decode()).get("payload", [None])[0]
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


@router.post("/interactions")
async def slack_interactions(request: Request, background: BackgroundTasks):
    raw_body = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}
    if not verify_slack_signature(raw_body, headers, settings.slack_signing_secret):
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    decoded = _decoded_payload(raw_body)
    org = _org_for_team(((decoded or {}).get("team") or {}).get("id"))
    background = _OrgTasks(background, org)
    with org_context(org):
        return _dispatch_interaction(background, raw_body, decoded)


def _dispatch_interaction(background: _OrgTasks, raw_body: bytes, decoded: dict | None):
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

    rollout_click = rollout.parse_public_channel_rollout_interaction_payload(raw_body)
    if rollout_click:
        background.add_task(rollout.start_public_channel_rollout, rollout_click)
        return {"ok": True}

    team_invite = parse_team_invite_interaction_payload(decoded) if decoded else None
    if team_invite:
        background.add_task(start_team_invite_wave, **team_invite)
        return {"ok": True}
    return Response(status_code=200)
