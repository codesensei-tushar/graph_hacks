"""Bringing the workspace in: automatic rollout, admin-picked intros, and the
team_join / user_change lifecycle.

Port of company-brain `brain/turn/team-invite.ts`. Their per-org Durable Object
tables become Redis records (one current run per Slack team), and their alarms
become ``src.core.scheduler`` jobs:

- ``brain:team-invite:run:<team>``         the current run (theirs: brain_team_invite_run id=1)
- ``brain:team-invite:targets:<run>``      hash slack_user_id -> target
- ``brain:team-invite:lifecycle:<team>``   automatic lifecycle opt-in
- ``brain:member-notified:<team>``         welcome-DM ledger, so nobody is messaged twice

Billing entitlement checks are dropped. Starters come from install_greeting's
``compose_starters`` (defaults until research and a model exist).
"""

import json
import logging
import time
import uuid

from src.core import scheduler
from src.db.falkordb_client import kv
from src.domain.access.roles import role_at_least, role_of
from src.domain.slack.greet import first_name_of
from src.domain.slack.install_greeting import compose_starters, research_brief_for
from src.domain.slack.lifecycle_api import (
    SlackMember,
    get_slack_team_directory,
    invite_slack_user_to_channel,
    list_slack_users_page,
    lookup_slack_user_info,
    open_slack_conversation,
    post_slack_ephemeral,
    post_slack_message,
    post_slack_message_idempotent,
    update_slack_message,
)
from src.domain.slack.member_provisioning import provision_slack_workspace_member, revoke_slack_workspace_member
from src.domain.slack.team_invite_card import (
    AUTOMATIC_TEAM_INVITE_FALLBACK,
    TEAM_INVITE_CARD_FALLBACK,
    TEAM_INVITE_NOTIFY_ALL_ACTION_ID,
    TEAM_INVITE_PICK_BLOCK_ID,
    TEAM_INVITE_SELECT_ACTION_ID,
    TEAM_INVITE_SEND_ACTION_ID,
    MemberIntroParts,
    automatic_team_invite_progress_blocks,
    is_eligible_full_member,
    member_intro_blocks,
    member_intro_text,
    team_invite_card_blocks,
)
from src.domain.slack.workspace import SlackWorkspace, get_home_channel, get_workspace_by_team_id, register_workspace_reset_hook

log = logging.getLogger(__name__)

STEP_DELAY_SECONDS = 1
MAX_TARGET_ATTEMPTS = 3
# Every try of a team_join / user_change retry counts, Retry-After or not: a Slack outage that keeps
# answering Retry-After must not keep the job alive forever. Retry-After only sets the next delay.
MAX_LIFECYCLE_TRIES = 10
MAX_WAVE_MEMBERS = 5000
ACTIVE_AUTOMATIC_RUN_STATUSES = ("enumerating", "provisioning", "preparing", "running")

RUN_DIRECTORY_PAGE = "runTeamInviteDirectoryPage"
RUN_PROVISION_STEP = "runTeamInviteProvisionStep"
RUN_INVITE_STEP = "runTeamInviteStep"
RUN_TEAM_JOIN_RETRY = "runSlackTeamJoinRetry"
RUN_USER_CHANGE_RETRY = "runSlackUserChangeRetry"


# --- storage ------------------------------------------------------------------------


def _run_key(team_id: str) -> str:
    return f"brain:team-invite:run:{team_id}"


def _targets_key(run_id: str) -> str:
    return f"brain:team-invite:targets:{run_id}"


def _ledger_key(team_id: str) -> str:
    return f"brain:member-notified:{team_id}"


def _now() -> int:
    return int(time.time() * 1000)


def current_run(team_id: str) -> dict | None:
    raw = kv().get(_run_key(team_id))
    return json.loads(raw) if raw else None


def _save_run(run: dict) -> None:
    run["updated_at"] = _now()
    kv().set(_run_key(run["team_id"]), json.dumps(run))


def _update_run(team_id: str, run_id: str, **changes) -> dict | None:
    run = current_run(team_id)
    if not run or run["run_id"] != run_id:
        return None
    for key, value in changes.items():
        if key.endswith("__inc"):
            field = key[: -len("__inc")]
            run[field] = run.get(field, 0) + value
        else:
            run[key] = value
    _save_run(run)
    return run


def _targets(run_id: str) -> dict[str, dict]:
    raw = kv().hgetall(_targets_key(run_id)) or {}
    return {(k.decode() if isinstance(k, bytes) else k): json.loads(v) for k, v in raw.items()}


def _target(run_id: str, slack_user_id: str) -> dict | None:
    raw = kv().hget(_targets_key(run_id), slack_user_id)
    return json.loads(raw) if raw else None


def _put_target(run_id: str, slack_user_id: str, target: dict) -> None:
    kv().hset(_targets_key(run_id), slack_user_id, json.dumps(target))


def _transition_target(run_id: str, slack_user_id: str, from_status: str | tuple, **changes) -> bool:
    target = _target(run_id, slack_user_id)
    allowed = (from_status,) if isinstance(from_status, str) else from_status
    if not target or target["status"] not in allowed:
        return False
    target.update(changes)
    _put_target(run_id, slack_user_id, target)
    return True


def _first_target(run_id: str, status: str) -> tuple[str, dict] | None:
    for slack_user_id, target in _targets(run_id).items():
        if target["status"] == status:
            return slack_user_id, target
    return None


def automatic_lifecycle_enabled(team_id: str) -> bool:
    return bool(kv().exists(f"brain:team-invite:lifecycle:{team_id}"))


def enable_automatic_lifecycle(team_id: str) -> None:
    kv().set(f"brain:team-invite:lifecycle:{team_id}", "1")


# --- notification ledger -------------------------------------------------------------


def _notification_reason_is_pending(reason: str) -> bool:
    return reason in ("welcome_pending", "team_join_pending")


def member_notification_complete(team_id: str, slack_user_id: str) -> bool:
    raw = kv().hget(_ledger_key(team_id), slack_user_id)
    return bool(raw and not _notification_reason_is_pending(json.loads(raw)["reason"]))


def claim_member_notification(team_id: str, slack_user_id: str, proposed_client_message_id: str) -> dict:
    """``{"status": "complete"}`` or ``{"status": "pending", "client_message_id": ...}``.

    The pending claim keeps its client_msg_id so a retry after an ambiguous failure
    reuses it and Slack dedupes the DM.
    """
    entry = {"reason": "welcome_pending", "client_msg_id": proposed_client_message_id, "notified_at": _now()}
    kv().hsetnx(_ledger_key(team_id), slack_user_id, json.dumps(entry))
    row = json.loads(kv().hget(_ledger_key(team_id), slack_user_id))
    if not _notification_reason_is_pending(row["reason"]):
        return {"status": "complete"}
    if not row.get("client_msg_id"):
        row["client_msg_id"] = proposed_client_message_id
        kv().hset(_ledger_key(team_id), slack_user_id, json.dumps(row))
    return {"status": "pending", "client_message_id": row["client_msg_id"]}


def complete_member_notification(team_id: str, slack_user_id: str, client_message_id: str, reason: str) -> None:
    raw = kv().hget(_ledger_key(team_id), slack_user_id)
    if not raw:
        return
    row = json.loads(raw)
    if row.get("client_msg_id") == client_message_id and _notification_reason_is_pending(row["reason"]):
        row.update(reason=reason, notified_at=_now())
        kv().hset(_ledger_key(team_id), slack_user_id, json.dumps(row))


def skip_active_team_invite_target(team_id: str, slack_user_id: str, reason: str) -> bool:
    run = current_run(team_id)
    if not run or run["status"] not in ACTIVE_AUTOMATIC_RUN_STATUSES:
        return False
    if not _transition_target(run["run_id"], slack_user_id, ("discovered", "provisioned", "pending"), status="skipped", last_error=reason):
        return False
    _update_run(team_id, run["run_id"], skipped__inc=1)
    return True


# --- cards & schedules ---------------------------------------------------------------


def _refresh_card(workspace: SlackWorkspace, run: dict, status: str) -> None:
    if not run.get("card_ts"):
        return
    web = workspace.client.web
    if run["mode"] == "automatic":
        latest = current_run(run["team_id"]) or run
        targets = _targets(latest["run_id"]).values()
        provisioned = sum(1 for t in targets if t["status"] != "discovered")
        finished = sum(1 for t in targets if t["status"] in ("sent", "skipped", "deduped"))
        if status == "done":
            phase = "done"
        elif status == "failed":
            phase = "failed"
        elif latest["status"] == "enumerating":
            phase = "enumerating"
        elif latest["status"] in ("provisioning", "preparing"):
            phase = "provisioning"
        else:
            phase = "notifying"
        update_slack_message(
            web,
            latest["card_channel"],
            latest.get("card_ts") or run["card_ts"],
            AUTOMATIC_TEAM_INVITE_FALLBACK,
            automatic_team_invite_progress_blocks(
                phase=phase,
                total=latest.get("total", 0),
                processed=provisioned if phase == "provisioning" else finished,
                sent=latest.get("sent", 0),
                skipped=latest.get("skipped", 0),
                deduped=latest.get("deduped", 0),
            ),
        )
        return
    update_slack_message(
        web,
        run["card_channel"],
        run["card_ts"],
        TEAM_INVITE_CARD_FALLBACK,
        team_invite_card_blocks(status=status, sent=run.get("sent", 0), skipped=run.get("skipped", 0), total=run.get("total", 0)),
    )


def _owns_schedule(run: dict, payload: dict) -> bool:
    return run["mode"] != "automatic" or run.get("schedule_id") == payload.get("schedule_id")


def _schedule_callback(team_id: str, run_id: str, callback: str, delay_seconds: float) -> bool:
    run = current_run(team_id)
    if not run or run["run_id"] != run_id:
        return False
    schedule_id = uuid.uuid4().hex
    if run["mode"] == "automatic":
        if run.get("schedule_id"):
            scheduler.cancel(run["schedule_id"])
        _update_run(team_id, run_id, schedule_id=schedule_id)
    scheduler.schedule(delay_seconds, callback, {"team_id": team_id, "run_id": run_id, "schedule_id": schedule_id}, job_id=schedule_id)
    return True


def _fail_owned_callback(payload: dict, reason: str) -> None:
    run = current_run(payload["team_id"])
    if not run or run["run_id"] != payload["run_id"] or run["mode"] != "automatic" or not _owns_schedule(run, payload):
        return
    _update_run(run["team_id"], run["run_id"], status="failed", schedule_id=None, failure_count__inc=1)
    log.error("[slack] automatic team invite callback failed run=%s reason=%s", run["run_id"], reason)


def reconcile_team_invite_schedule(team_id: str) -> None:
    """Re-arm an active automatic run whose schedule was lost (e.g. a restart mid-wave)."""
    run = current_run(team_id)
    if not run or run["mode"] != "automatic" or run["status"] not in ACTIVE_AUTOMATIC_RUN_STATUSES:
        return
    callback = {
        "enumerating": RUN_DIRECTORY_PAGE,
        "provisioning": RUN_PROVISION_STEP,
        "preparing": RUN_PROVISION_STEP,
        "running": RUN_INVITE_STEP,
    }[run["status"]]
    if run["status"] == "preparing":
        _update_run(team_id, run["run_id"], status="provisioning")
    _schedule_callback(team_id, run["run_id"], callback, STEP_DELAY_SECONDS)


def _wave_starters(workspace: SlackWorkspace) -> list[str] | None:
    try:
        brief = research_brief_for(workspace.team_id)
        return compose_starters(brief, workspace.team_name or "the company") if brief else None
    except Exception:
        return None


# --- automatic rollout ---------------------------------------------------------------


def start_automatic_team_invite_rollout(
    *, team_id: str, installer_slack_user_id: str, admin_channel: str | None = None, admin_message_ts: str | None = None
) -> None:
    prior = current_run(team_id)
    workspace = get_workspace_by_team_id(team_id)
    if workspace is None:
        return
    enable_automatic_lifecycle(team_id)

    if prior and prior["mode"] == "automatic" and prior["status"] in ACTIVE_AUTOMATIC_RUN_STATUSES:
        _update_run(
            team_id,
            prior["run_id"],
            card_channel=admin_channel or prior["card_channel"],
            card_ts=admin_message_ts or prior.get("card_ts"),
            requested_by_slack_user_id=installer_slack_user_id,
        )
        reconcile_team_invite_schedule(team_id)
        return

    run_id = uuid.uuid4().hex
    if prior:
        kv().delete(_targets_key(prior["run_id"]))
    _save_run(
        {
            "run_id": run_id,
            "team_id": team_id,
            "mode": "automatic",
            "status": "enumerating",
            "sent": 0,
            "skipped": 0,
            "deduped": 0,
            "total": 0,
            "card_channel": admin_channel or "",
            "card_ts": admin_message_ts,
            "starters": None,
            "failure_count": 0,
            "directory_cursor": None,
            "requested_by_slack_user_id": installer_slack_user_id,
            "schedule_id": None,
            "created_at": _now(),
        }
    )
    _schedule_callback(team_id, run_id, RUN_DIRECTORY_PAGE, STEP_DELAY_SECONDS)


def attach_automatic_team_invite_card(*, team_id: str, admin_channel: str, admin_message_ts: str) -> None:
    run = current_run(team_id)
    workspace = get_workspace_by_team_id(team_id)
    if workspace is None:
        return
    if not run or run["mode"] != "automatic":
        update_slack_message(
            workspace.client.web,
            admin_channel,
            admin_message_ts,
            AUTOMATIC_TEAM_INVITE_FALLBACK,
            automatic_team_invite_progress_blocks(phase="failed"),
        )
        return
    latest = _update_run(team_id, run["run_id"], card_channel=admin_channel, card_ts=admin_message_ts)
    if not latest:
        return
    status = "done" if latest["status"] == "done" else "failed" if latest["status"] in ("failed", "aborted") else "running"
    _refresh_card(workspace, latest, status)
    reconcile_team_invite_schedule(team_id)


def run_team_invite_directory_page(payload: dict) -> None:
    team_id = payload["team_id"]
    run = current_run(team_id)
    if not run or run["run_id"] != payload["run_id"] or run["status"] != "enumerating" or run["mode"] != "automatic" or not _owns_schedule(run, payload):
        return
    workspace = get_workspace_by_team_id(team_id)
    if workspace is None:
        _fail_owned_callback(payload, "workspace_missing")
        return
    page = list_slack_users_page(workspace.client.web, run.get("directory_cursor"))
    if not page.ok:
        if page.retry_after_seconds:
            _schedule_callback(team_id, run["run_id"], RUN_DIRECTORY_PAGE, page.retry_after_seconds)
            return
        failures = run.get("failure_count", 0) + 1
        gave_up = failures >= MAX_TARGET_ATTEMPTS
        failed = _update_run(team_id, run["run_id"], failure_count=failures, status="failed" if gave_up else "enumerating", **({"schedule_id": None} if gave_up else {}))
        if gave_up:
            if failed:
                _refresh_card(workspace, failed, "failed")
        else:
            _schedule_callback(team_id, run["run_id"], RUN_DIRECTORY_PAGE, STEP_DELAY_SECONDS * failures)
        return

    existing = _targets(run["run_id"])
    added = 0
    for member in page.items:
        if member.id == run.get("requested_by_slack_user_id") or not is_eligible_full_member(member, workspace.bot_user_id, team_id):
            continue
        if len(existing) + added >= MAX_WAVE_MEMBERS:
            break
        if member.id in existing:
            continue
        _put_target(
            run["run_id"],
            member.id,
            {
                "status": "discovered",
                "attempts": 0,
                "client_msg_id": str(uuid.uuid4()),
                "email": (member.email or "").strip().lower() or None,
                "display_name": member.display_name or member.name,
                "person_id": None,
                "last_error": None,
            },
        )
        added += 1

    total = len(_targets(run["run_id"]))
    complete = page.complete or total >= MAX_WAVE_MEMBERS
    updated = _update_run(
        team_id,
        run["run_id"],
        total=total,
        directory_cursor=None if complete else page.next_cursor,
        failure_count=0,
        status="provisioning" if complete else "enumerating",
    )
    if updated:
        _refresh_card(workspace, updated, "running")
    _schedule_callback(team_id, run["run_id"], RUN_PROVISION_STEP if complete else RUN_DIRECTORY_PAGE, STEP_DELAY_SECONDS)


def run_team_invite_provision_step(payload: dict) -> None:
    team_id = payload["team_id"]
    run = current_run(team_id)
    if not run or run["run_id"] != payload["run_id"] or run["status"] != "provisioning" or run["mode"] != "automatic" or not _owns_schedule(run, payload):
        return
    workspace = get_workspace_by_team_id(team_id)
    if workspace is None:
        _fail_owned_callback(payload, "workspace_missing")
        return

    found = _first_target(run["run_id"], "discovered")
    if found is None:
        provisioned_count = sum(1 for t in _targets(run["run_id"]).values() if t["status"] == "provisioned")
        if provisioned_count == 0:
            finished = _update_run(team_id, run["run_id"], status="done", schedule_id=None)
            if finished:
                _refresh_card(workspace, finished, "done")
            return
        preparing = _update_run(team_id, run["run_id"], status="preparing")
        if preparing:
            _refresh_card(workspace, preparing, "running")
        starters = _wave_starters(workspace)
        current = current_run(team_id)
        if not current or current["run_id"] != run["run_id"] or current["status"] != "preparing":
            return
        running = _update_run(team_id, run["run_id"], status="running", starters=starters)
        if running:
            _refresh_card(workspace, running, "running")
        _schedule_callback(team_id, run["run_id"], RUN_INVITE_STEP, STEP_DELAY_SECONDS)
        return

    slack_user_id, target = found
    profile = lookup_slack_user_info(workspace.client.web, slack_user_id)
    if not profile.ok:
        if profile.reason == "slack_api_error":
            if profile.retry_after_seconds:
                _schedule_callback(team_id, run["run_id"], RUN_PROVISION_STEP, profile.retry_after_seconds)
                return
            attempts = target["attempts"] + 1
            give_up = attempts >= MAX_TARGET_ATTEMPTS
            _transition_target(
                run["run_id"], slack_user_id, "discovered",
                attempts=attempts, status="skipped" if give_up else "discovered", last_error=profile.error or profile.reason,
            )
            if give_up:
                _update_run(team_id, run["run_id"], skipped__inc=1)
        else:
            skip_active_team_invite_target(team_id, slack_user_id, profile.reason or "lookup_failed")
        _schedule_callback(team_id, run["run_id"], RUN_PROVISION_STEP, STEP_DELAY_SECONDS)
        return

    user = profile.user
    eligible = is_eligible_full_member(user, workspace.bot_user_id, team_id)
    if not eligible or not user.email:
        skip_active_team_invite_target(team_id, slack_user_id, "missing_slack_email" if eligible else "slack_member_ineligible")
        _schedule_callback(team_id, run["run_id"], RUN_PROVISION_STEP, STEP_DELAY_SECONDS)
        return

    try:
        provisioned = provision_slack_workspace_member(
            team_id=team_id, slack_user_id=slack_user_id, email=user.email, name=user.display_name or user.name
        )
        _transition_target(run["run_id"], slack_user_id, "discovered", status="provisioned", person_id=provisioned.person_id, last_error=None)
    except Exception as error:
        attempts = target["attempts"] + 1
        give_up = attempts >= MAX_TARGET_ATTEMPTS
        _transition_target(
            run["run_id"], slack_user_id, "discovered",
            attempts=attempts, status="skipped" if give_up else "discovered", last_error=str(error)[:500],
        )
        if give_up:
            _update_run(team_id, run["run_id"], skipped__inc=1)
    _schedule_callback(team_id, run["run_id"], RUN_PROVISION_STEP, STEP_DELAY_SECONDS)


# --- admin-picked waves --------------------------------------------------------------


def parse_team_invite_interaction_payload(payload: dict) -> dict | None:
    """Port of `parseTeamInviteInteractionPayload` (routes/slack/index.ts) on a decoded block_actions payload."""
    if not isinstance(payload, dict) or payload.get("type") != "block_actions":
        return None
    action = (payload.get("actions") or [{}])[0]
    action_id = action.get("action_id")
    if action_id not in (TEAM_INVITE_SEND_ACTION_ID, TEAM_INVITE_NOTIFY_ALL_ACTION_ID):
        return None
    mode = "selected" if action_id == TEAM_INVITE_SEND_ACTION_ID else "all"
    user_ids = None
    if mode == "selected":
        picked = (
            ((payload.get("state") or {}).get("values") or {}).get(TEAM_INVITE_PICK_BLOCK_ID, {}).get(TEAM_INVITE_SELECT_ACTION_ID, {})
        ).get("selected_users")
        user_ids = [u for u in picked if isinstance(u, str)] if isinstance(picked, list) else []
        if not user_ids:
            return None
    team_id = (payload.get("team") or {}).get("id")
    slack_user_id = (payload.get("user") or {}).get("id")
    card_channel = (payload.get("channel") or {}).get("id")
    card_ts = (payload.get("message") or {}).get("ts")
    if not (team_id and slack_user_id and card_channel):
        return None
    return {
        "team_id": team_id,
        "requested_by_slack_user_id": slack_user_id,
        "card_channel": card_channel,
        "card_ts": card_ts,
        "mode": mode,
        "user_ids": user_ids,
    }


def start_team_invite_wave(
    *,
    team_id: str,
    mode: str,
    requested_by_slack_user_id: str,
    card_channel: str,
    card_ts: str | None = None,
    user_ids: list[str] | None = None,
) -> None:
    """``mode`` is ``selected`` (picker) or ``all``. Admin only."""
    prior = current_run(team_id)
    prior_busy = bool(prior and prior["status"] in ACTIVE_AUTOMATIC_RUN_STATUSES)
    run_id = uuid.uuid4().hex
    # Claim synchronously so concurrent or replayed clicks can't each start a run.
    if not prior_busy:
        if prior:
            kv().delete(_targets_key(prior["run_id"]))
        _save_run(
            {
                "run_id": run_id, "team_id": team_id, "mode": mode, "status": "preparing",
                "sent": 0, "skipped": 0, "deduped": 0, "total": 0,
                "card_channel": card_channel, "card_ts": card_ts, "starters": None,
                "failure_count": 0, "directory_cursor": None,
                "requested_by_slack_user_id": requested_by_slack_user_id, "schedule_id": None, "created_at": _now(),
            }
        )

    def abort_claim() -> None:
        _update_run(team_id, run_id, status="aborted", schedule_id=None)

    workspace = get_workspace_by_team_id(team_id)
    if workspace is None:
        if not prior_busy:
            abort_claim()
        return
    web = workspace.client.web

    def ephemeral(text: str) -> None:
        post_slack_ephemeral(web, card_channel, requested_by_slack_user_id, text)

    if prior_busy:
        ephemeral("I'm already working through the invites. I'll keep the card updated.")
        return

    if not role_at_least(role_of(requested_by_slack_user_id), "admin"):
        abort_claim()
        ephemeral("Only a Company Brain admin can invite the team.")
        return

    truncated = False
    if mode == "selected":
        # Picker payloads are attacker-shapeable: cap server-side; eligibility re-checks at send time.
        candidates = list(dict.fromkeys(user_ids or []))[:MAX_WAVE_MEMBERS]
    else:
        directory = get_slack_team_directory(web, MAX_WAVE_MEMBERS)
        truncated = len(directory) >= MAX_WAVE_MEMBERS
        candidates = [m.id for m in directory if is_eligible_full_member(m, workspace.bot_user_id, team_id)]
    candidates = [c for c in candidates if c != requested_by_slack_user_id and not member_notification_complete(team_id, c)]
    if not candidates:
        abort_claim()
        ephemeral("Everyone eligible has already been notified. 🎉" if mode == "all" else "Those teammates have already been notified. 🎉")
        return

    starters = _wave_starters(workspace)
    claimed = current_run(team_id)
    if not claimed or claimed["run_id"] != run_id:
        return
    for slack_user_id in candidates:
        _put_target(run_id, slack_user_id, {"status": "pending", "attempts": 0, "client_msg_id": str(uuid.uuid4()), "person_id": None, "last_error": None})
    run = _update_run(team_id, run_id, status="running", total=len(candidates), starters=starters)
    plural = "" if len(candidates) == 1 else "s"
    note = (
        f" Note: I could only load the first {MAX_WAVE_MEMBERS} workspace members, so some teammates beyond that "
        "aren't covered — use the picker for anyone missed."
        if truncated
        else ""
    )
    ephemeral(f"On it — DMing {len(candidates)} teammate{plural} a personal intro. Guests are skipped and nobody gets messaged twice.{note}")
    if run:
        _refresh_card(workspace, run, "running")
    _schedule_callback(team_id, run_id, RUN_INVITE_STEP, STEP_DELAY_SECONDS)


def send_member_intro(
    workspace: SlackWorkspace,
    *,
    slack_user_id: str,
    starters: list[str] | None,
    client_message_id: str | None,
) -> dict:
    """``{"ok": True}`` or ``{"ok": False, "retry_after_seconds": n?}``."""
    web = workspace.client.web
    info = lookup_slack_user_info(web, slack_user_id)
    if not info.ok:
        return {"ok": False, "retry_after_seconds": info.retry_after_seconds}
    user = info.user
    if user.is_bot or user.is_restricted:
        return {"ok": False}
    # Picker payloads can name Slack Connect / external users; never DM anyone from another workspace.
    if user.is_stranger or (user.team_id and user.team_id != workspace.team_id):
        return {"ok": False}
    channel = open_slack_conversation(web, slack_user_id)
    if not channel:
        return {"ok": False}
    home = get_home_channel(workspace.team_id)
    parts = MemberIntroParts(
        team_id=workspace.team_id,
        first_name=first_name_of(user.display_name or user.name),
        company_name=workspace.team_name,
        home_channel_id=home["channel_id"] if home else None,
        starters=starters,
    )
    if not client_message_id:
        ts = post_slack_message(web, channel, member_intro_text(parts), None, member_intro_blocks(parts))
        return {"ok": bool(ts)}
    res = post_slack_message_idempotent(web, channel, member_intro_text(parts), client_message_id, member_intro_blocks(parts))
    return {"ok": True} if res.ok else {"ok": False, "retry_after_seconds": res.retry_after_seconds}


def run_team_invite_step(payload: dict) -> None:
    """One DM per tick; 429s reschedule with Slack's Retry-After instead of sleeping."""
    team_id = payload["team_id"]
    run = current_run(team_id)
    if not run or run["run_id"] != payload["run_id"] or run["status"] != "running" or not _owns_schedule(run, payload):
        return
    workspace = get_workspace_by_team_id(team_id)
    if workspace is None:
        _fail_owned_callback(payload, "workspace_missing")
        return
    web = workspace.client.web
    run_id = run["run_id"]
    target_status = "provisioned" if run["mode"] == "automatic" else "pending"
    found = _first_target(run_id, target_status)
    if found is None:
        finished = _update_run(team_id, run_id, status="done", schedule_id=None)
        if finished:
            _refresh_card(workspace, finished, "done")
        return
    slack_user_id, target = found

    if run["mode"] == "automatic":
        home = get_home_channel(team_id)
        if not home:
            if _transition_target(run_id, slack_user_id, target_status, status="skipped", last_error="missing_home_channel"):
                _update_run(team_id, run_id, skipped__inc=1)
            _schedule_callback(team_id, run_id, RUN_INVITE_STEP, STEP_DELAY_SECONDS)
            return
        invited = invite_slack_user_to_channel(web, home["channel_id"], slack_user_id)
        if not invited.ok:
            if invited.retry_after_seconds:
                _schedule_callback(team_id, run_id, RUN_INVITE_STEP, invited.retry_after_seconds)
                return
            attempts = target["attempts"] + 1
            give_up = attempts >= MAX_TARGET_ATTEMPTS
            moved = _transition_target(
                run_id, slack_user_id, target_status,
                attempts=attempts, status="skipped" if give_up else "provisioned", last_error=(invited.error or "")[:500],
            )
            if give_up and moved:
                _update_run(team_id, run_id, skipped__inc=1)
            _schedule_callback(team_id, run_id, RUN_INVITE_STEP, STEP_DELAY_SECONDS)
            return

    claim = claim_member_notification(team_id, slack_user_id, target.get("client_msg_id") or str(uuid.uuid4()))
    if claim["status"] == "complete":
        if _transition_target(run_id, slack_user_id, target_status, status="deduped", last_error=None):
            _update_run(team_id, run_id, deduped__inc=1)
        _schedule_callback(team_id, run_id, RUN_INVITE_STEP, STEP_DELAY_SECONDS)
        return
    if not _transition_target(run_id, slack_user_id, target_status, client_msg_id=claim["client_message_id"]):
        _schedule_callback(team_id, run_id, RUN_INVITE_STEP, STEP_DELAY_SECONDS)
        return

    try:
        result = send_member_intro(
            workspace, slack_user_id=slack_user_id, starters=run.get("starters"), client_message_id=claim["client_message_id"]
        )
    except Exception:
        result = {"ok": False}

    if result["ok"]:
        complete_member_notification(team_id, slack_user_id, claim["client_message_id"], f"invite_{run['mode']}")
        if _transition_target(run_id, slack_user_id, target_status, status="sent"):
            _update_run(team_id, run_id, sent__inc=1)
    elif result.get("retry_after_seconds"):
        _schedule_callback(team_id, run_id, RUN_INVITE_STEP, result["retry_after_seconds"])
        return
    else:
        attempts = target["attempts"] + 1
        give_up = attempts >= MAX_TARGET_ATTEMPTS
        moved = _transition_target(run_id, slack_user_id, target_status, attempts=attempts, status="skipped" if give_up else target_status)
        if give_up and moved:
            _update_run(team_id, run_id, skipped__inc=1)

    updated = current_run(team_id)
    if updated and updated["run_id"] == run_id and (
        updated["mode"] != "automatic" or (updated["sent"] + updated["skipped"] + updated["deduped"]) % 25 == 0
    ):
        _refresh_card(workspace, updated, "running")
    _schedule_callback(team_id, run_id, RUN_INVITE_STEP, STEP_DELAY_SECONDS)


# --- team_join / user_change lifecycle -----------------------------------------------


def _terminal() -> dict:
    return {"terminal": True}


def _retry(retry_after_seconds: int | None = None) -> dict:
    return {"terminal": False, "retry_after_seconds": retry_after_seconds}


def process_slack_team_join(team_id: str, user: dict) -> dict:
    """Soft hello for later joiners. Silent until the workspace has run a team invite,
    so workspaces that never opted into outreach stay quiet. Terminal outcomes are
    delivered or permanently ineligible; transient ones retry."""
    try:
        if (
            user.get("deleted")
            or user.get("is_bot")
            or user.get("is_restricted")
            or user.get("is_ultra_restricted")
            or user.get("is_stranger")
            or (user.get("team_id") and user["team_id"] != team_id)
        ):
            return _terminal()
        run = current_run(team_id)
        legacy_enabled = bool(run and run["mode"] != "automatic" and run["status"] in ("running", "done"))
        if not automatic_lifecycle_enabled(team_id) and not legacy_enabled:
            return _terminal()
        workspace = get_workspace_by_team_id(team_id)
        if workspace is None or user["id"] == workspace.bot_user_id:
            return _terminal()
        web = workspace.client.web
        profile = lookup_slack_user_info(web, user["id"])
        if not profile.ok:
            return _retry(profile.retry_after_seconds)
        if not is_eligible_full_member(profile.user, workspace.bot_user_id, team_id):
            return _terminal()
        if not profile.user.email:
            return _retry()
        provision_slack_workspace_member(
            team_id=team_id, slack_user_id=user["id"], email=profile.user.email, name=profile.user.display_name or profile.user.name
        )
        home = get_home_channel(team_id)
        if not home:
            return _retry()
        invited = invite_slack_user_to_channel(web, home["channel_id"], user["id"])
        if not invited.ok:
            return _retry(invited.retry_after_seconds)

        # Restore access before deduplicating the welcome DM, so user_change can
        # re-onboard a promoted or reactivated member without a second introduction.
        claim = claim_member_notification(team_id, user["id"], str(uuid.uuid4()))
        if claim["status"] == "complete":
            return _terminal()
        result = send_member_intro(
            workspace,
            slack_user_id=user["id"],
            starters=(run or {}).get("starters"),
            client_message_id=claim["client_message_id"],
        )
        if result["ok"]:
            complete_member_notification(team_id, user["id"], claim["client_message_id"], "team_join")
            return _terminal()
        # Keep the pending claim (and its UUID) so a retry is idempotent.
        return _retry(result.get("retry_after_seconds"))
    except Exception:
        log.warning("[slack] team join hello failed", exc_info=True)
        return _retry()


def on_slack_team_join(team_id: str, user: dict) -> bool:
    outcome = process_slack_team_join(team_id, user)
    if not outcome["terminal"]:
        scheduler.schedule(
            outcome.get("retry_after_seconds") or STEP_DELAY_SECONDS * 5,
            RUN_TEAM_JOIN_RETRY,
            {"team_id": team_id, "user": user, "attempt": 1},
        )
    # Once a durable retry is scheduled, the HTTP event can be deduplicated.
    return True


def _retry_lifecycle(job: str, payload: dict, retry_after: float | None, failed: bool = False) -> None:
    """Try ``job`` again, unless it has had ``MAX_LIFECYCLE_TRIES`` tries (all of them count) or, with no
    Retry-After, ``MAX_TARGET_ATTEMPTS`` failed ones. Retry-After sets the delay, never the count."""
    attempt = payload["attempt"]
    tries = int(payload.get("tries") or attempt)
    if tries >= MAX_LIFECYCLE_TRIES or (not retry_after and attempt >= MAX_TARGET_ATTEMPTS):
        log.error("[slack] %s gave up after %d tries team=%s user=%s", job, tries, payload["team_id"], (payload.get("user") or {}).get("id"))
        return
    scheduler.schedule(
        retry_after or STEP_DELAY_SECONDS * 5 * attempt,
        job,
        {**payload, "attempt": attempt if retry_after and not failed else attempt + 1, "tries": tries + 1},
    )


def run_slack_team_join_retry(payload: dict) -> None:
    outcome = process_slack_team_join(payload["team_id"], payload["user"])
    if outcome["terminal"]:
        return
    _retry_lifecycle(RUN_TEAM_JOIN_RETRY, payload, outcome.get("retry_after_seconds"))


def process_slack_user_change(team_id: str, user: dict) -> dict:
    workspace = get_workspace_by_team_id(team_id)
    if workspace is None:
        return _terminal()
    profile = lookup_slack_user_info(workspace.client.web, user["id"])
    if not profile.ok and profile.reason in ("slack_api_error", "missing_user_id"):
        return _retry(profile.retry_after_seconds)
    if profile.ok and is_eligible_full_member(profile.user, workspace.bot_user_id, team_id):
        member: SlackMember = profile.user
        return process_slack_team_join(
            team_id,
            {
                "id": user["id"],
                "is_bot": member.is_bot,
                "is_restricted": member.is_restricted,
                "is_ultra_restricted": member.is_ultra_restricted,
                "is_stranger": member.is_stranger,
                "team_id": member.team_id,
            },
        )
    skip_active_team_invite_target(team_id, user["id"], "slack_member_revoked")
    revoke_slack_workspace_member(team_id=team_id, slack_user_id=user["id"])
    return _terminal()


def on_slack_user_change(team_id: str, user: dict) -> None:
    try:
        outcome = process_slack_user_change(team_id, user)
        if not outcome["terminal"]:
            scheduler.schedule(
                outcome.get("retry_after_seconds") or STEP_DELAY_SECONDS * 5,
                RUN_USER_CHANGE_RETRY,
                {"team_id": team_id, "user": user, "attempt": 1},
            )
    except Exception:
        scheduler.schedule(STEP_DELAY_SECONDS * 5, RUN_USER_CHANGE_RETRY, {"team_id": team_id, "user": user, "attempt": 1})


def run_slack_user_change_retry(payload: dict) -> None:
    try:
        outcome = process_slack_user_change(payload["team_id"], payload["user"])
    except Exception:
        log.warning("[slack] user_change lifecycle sync failed team=%s user=%s", payload["team_id"], payload["user"].get("id"), exc_info=True)
        _retry_lifecycle(RUN_USER_CHANGE_RETRY, payload, None, failed=True)
        return
    if outcome["terminal"]:
        return
    _retry_lifecycle(RUN_USER_CHANGE_RETRY, payload, outcome.get("retry_after_seconds"))


def _reset_team_invite_state(team_id: str) -> None:
    run = current_run(team_id)
    if run:
        if run.get("schedule_id"):
            scheduler.cancel(run["schedule_id"])
        kv().delete(_targets_key(run["run_id"]))
    kv().delete(_run_key(team_id), f"brain:team-invite:lifecycle:{team_id}", _ledger_key(team_id))
    for name in (RUN_DIRECTORY_PAGE, RUN_PROVISION_STEP, RUN_INVITE_STEP, RUN_TEAM_JOIN_RETRY, RUN_USER_CHANGE_RETRY):
        scheduler.cancel_matching(name, lambda payload: payload.get("team_id") == team_id)


scheduler.register(RUN_DIRECTORY_PAGE, run_team_invite_directory_page)
scheduler.register(RUN_PROVISION_STEP, run_team_invite_provision_step)
scheduler.register(RUN_INVITE_STEP, run_team_invite_step)
scheduler.register(RUN_TEAM_JOIN_RETRY, run_slack_team_join_retry)
scheduler.register(RUN_USER_CHANGE_RETRY, run_slack_user_change_retry)
register_workspace_reset_hook(_reset_team_invite_state)
