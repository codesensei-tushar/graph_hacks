"""Port of company-brain ``brain/slack/public-channel-rollout.ts``.

Joining public channels and catching up on them:

- A read-only status card in the home channel; the installer's DM copy carries the
  "Add me to my public channels" button (a channel message renders the same for
  every viewer, so only the DM gets the button).
- Beachhead: a few minutes after install, join the two busiest eligible channels on
  its own and post a read-out of what it learned.
- Full rollout (admin button): discover → join → collect (paged history and thread
  replies with persisted cursors) → crosscheck → introduce, one step per scheduled
  job. Slack ``Retry-After`` reschedules the step; failures back off and give up
  after ``MAX_STAGE_ATTEMPTS`` per channel and ``MAX_RUN_FAILURES`` per run.
- The window is frozen at start: the last ``PUBLIC_CHANNEL_ROLLOUT_DAYS`` days.
- Skips #general, the home channel, private, archived and Slack Connect channels.
- Introductions are exactly-once: a per-team ledger plus a stable Slack
  ``client_msg_id`` per channel.

company-brain keeps this state in Durable Object SQL tables. Each table is JSON in
Redis here, keyed by team (``brain:rollout:<team>:...``). History documents go to
FalkorDB in ``org_shared`` through ``upsert_brain_document`` (``addMemorySingle``). Entitlement checks, telemetry and profile-config sync
are dropped; cross-checking uses the connector-free branch.
"""

import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import asdict
from typing import Any, Literal
from urllib.parse import parse_qs

from src.core import scheduler
from src.core.config import settings
from src.db.falkordb_client import kv
from src.domain.access.roles import role_at_least, role_of
from src.domain.memory.containers import SHARED_TEAM_BRAIN_CONTAINER_TAG
from src.domain.slack.channels._compat import (
    as_dict,
    escape_slack_text,
    field,
    normalize_slack_message_content,
    slack_user_display_name,
)
from src.domain.slack.channels.directory import invalidate_channel_directory
from src.domain.memory.documents import document_statuses, upsert_brain_document
from src.domain.slack.channels.history_document import (
    SlackHistoryDocumentInput,
    build_slack_history_documents,
    recent_slack_channel_evidence,
)
from src.domain.slack.channels.introduction import (
    ChannelTheme,
    ToolCrosscheck,
    compose_channel_introduction,
    crosscheck_themes_with_connected_tools,
    extract_channel_themes,
)
from src.domain.slack.channels.policy import is_eligible_public_channel, is_slack_history_thread_root

log = logging.getLogger(__name__)

PUBLIC_CHANNEL_ROLLOUT_ACTION_ID = "brain_add_public_channels"
PUBLIC_CHANNEL_ROLLOUT_DAYS = 7

REQUIRED_SCOPES = [
    "channels:history",
    "channels:join",
    "channels:read",
    "chat:write",
    "users:read",
    "users:read.email",
]
NEXT_STEP_DELAY_SECONDS = 1
MEMORY_POLL_DELAY_SECONDS = 30
MAX_STAGE_ATTEMPTS = 5
MAX_RUN_FAILURES = 10
MAX_MEMORY_WAIT_MS = 24 * 60 * 60 * 1000
CROSSCHECK_CHANNEL_BATCH = 8
HOME_WELCOME_BUBBLE_DELAY_MS = 1000
# Let the install greeting land before the join notice follows it.
BEACHHEAD_DELAY_SECONDS = 5 * 60
BEACHHEAD_CHANNEL_COUNT = 2

# Never auto-picked; the admin-initiated full rollout may still include them.
BEACHHEAD_NAME_BLOCKLIST = re.compile(
    r"(^|[-_])(hr|people|finance|legal|security|compliance|incident|salar|payroll|comp)([-_]|$)", re.I
)
# Not blocked, just last in line: only picked when no work channel qualifies.
BEACHHEAD_SOCIAL_NAMES = re.compile(
    r"(^|[-_])(random|social|watercooler|memes|fun|offtopic|celebration|celebrations)([-_]|$)", re.I
)

JOB_RUN_ROLLOUT = "runPublicChannelRollout"
JOB_RUN_BEACHHEAD = "runPublicChannelBeachhead"

RolloutStatus = Literal["running", "done", "failed"]


# --- storage (the DO SQL tables, as Redis JSON) ---------------------------------------


def _p(team_id: str) -> str:
    return f"brain:rollout:{team_id}"


def _now() -> int:
    return int(time.time() * 1000)


def _get(key: str) -> dict | None:
    raw = kv().get(key)
    return json.loads(raw) if raw else None


def _put(key: str, value: dict) -> None:
    kv().set(key, json.dumps(value))


def _hget(key: str, member: str) -> dict | None:
    raw = kv().hget(key, member)
    return json.loads(raw) if raw else None


def _hput(key: str, member: str, value: dict) -> None:
    kv().hset(key, member, json.dumps(value))


def _hall(key: str) -> list[dict]:
    return [json.loads(v) for v in (kv().hgetall(key) or {}).values()]


def current_run(team_id: str) -> dict | None:
    return _get(f"{_p(team_id)}:run")


def _update_run(team_id: str, run_id: str, **changes) -> None:
    run = current_run(team_id)
    if run and run["run_id"] == run_id:
        run.update(changes)
        _put(f"{_p(team_id)}:run", run)


def rollout_card(team_id: str) -> dict | None:
    return _get(f"{_p(team_id)}:card")


def _channels(team_id: str) -> list[dict]:
    return _hall(f"{_p(team_id)}:channels")


def _channel(team_id: str, channel_id: str) -> dict | None:
    return _hget(f"{_p(team_id)}:channels", channel_id)


def _update_channel(team_id: str, run_id: str, channel_id: str, **changes) -> None:
    row = _channel(team_id, channel_id)
    if row and row["run_id"] == run_id:
        row.update(changes, updated_at=_now())
        _hput(f"{_p(team_id)}:channels", channel_id, row)


def _introduction(team_id: str, channel_id: str) -> dict | None:
    return _hget(f"{_p(team_id)}:introductions", channel_id)


def _parse_json_array(value: str | None) -> list:
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except ValueError:
        return []


def _rollout_error_code(error: BaseException) -> str:
    message = str(error)
    return message if re.fullmatch(r"[a-z0-9_:.-]{1,160}", message, re.I) else type(error).__name__ or "unknown_error"


def _themes(raw: str | None) -> list[ChannelTheme]:
    return [ChannelTheme(**t) for t in _parse_json_array(raw)]


def _checks(raw: str | None) -> list[ToolCrosscheck]:
    return [ToolCrosscheck(**c) for c in _parse_json_array(raw)]


# --- workspace -------------------------------------------------------------------------


def _workspace(team_id: str):
    from src.domain.slack.workspace import get_workspace_by_team_id

    return get_workspace_by_team_id(team_id)


def _granted_scopes(ws) -> set[str] | None:
    """None when the workspace doesn't record scopes (env-token install); then skip the check."""
    scopes = getattr(ws, "scopes", None)
    if scopes is None:
        return None
    if isinstance(scopes, str):
        return {s.strip() for s in re.split(r"[ ,]+", scopes) if s.strip()}
    return set(scopes)


def _installer(ws, explicit: str | None) -> str | None:
    return explicit or getattr(ws, "installed_by_slack_user_id", None) or settings.slack_owner_id or None


def _rollout_actor_name(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[<>&\r\n]", " ", value)).strip()[:80] or "your admin"


def _lookup_user(client, slack_user_id: str):
    """``lookupSlackUserInfo`` → the user, or None."""
    if not slack_user_id:
        return None
    result = client.lookup_slack_user_info(slack_user_id)
    if not field(result, "ok", False):
        return None
    return field(result, "user")


# --- home welcome + cards --------------------------------------------------------------


def _ensure_home_welcome(team_id: str, client, payload: dict) -> None:
    supplied = payload.get("welcome")
    if not supplied:
        return
    messages = [m.strip() for m in supplied.get("messages") or [] if m and m.strip()][:8]
    if not messages:
        return
    try:
        version = max(1, int(supplied.get("version") or 1))
    except (TypeError, ValueError):
        version = 1
    key = f"{_p(team_id)}:home_welcome"
    row = _get(key)
    if not row or row["team_id"] != team_id or row["home_channel_id"] != payload["home_channel_id"] or row["version"] != version:
        row = {
            "team_id": team_id,
            "home_channel_id": payload["home_channel_id"],
            "version": version,
            "next_message_index": 0,
            "completed_at": None,
        }
        _put(key, row)
    if row.get("completed_at") and row["next_message_index"] >= len(messages):
        return
    start = min(max(int(row.get("next_message_index") or 0), 0), len(messages))
    for index in range(start, len(messages)):
        ts = client.post_slack_message(payload["home_channel_id"], messages[index])
        if not ts:
            raise RuntimeError(f"home_welcome_message_{index + 1}_failed")
        row["next_message_index"] = index + 1
        _put(key, row)
        if index < len(messages) - 1:
            time.sleep(HOME_WELCOME_BUBBLE_DELAY_MS / 1000)
    row.update(next_message_index=len(messages), completed_at=_now())
    _put(key, row)


def public_channel_rollout_card_blocks(
    status: Literal["idle", "running", "done", "failed"],
    beachhead: dict | None = None,
    admin: dict | None = None,
) -> list[dict]:
    """Channel messages look the same to everyone, so only the DM copy has the button.

    ``beachhead``: {"channel_ids": [...], "readout_posted": bool}; ``admin``: {"home_channel_id": ...}.
    """
    refs = " and ".join(f"<#{cid}>" for cid in beachhead["channel_ids"]) if beachhead else ""
    here = f"in <#{admin['home_channel_id']}>" if admin else "here"
    readout_at = f"in <#{admin['home_channel_id']}>" if admin else "just below"
    if status == "idle":
        body = f"I'll join your most active public channels shortly and post what I learn {here}, so everyone gets a feel for what I'm good for. Invite me to any channel yourself, or an admin can add me across all of them at once."
    elif status == "running":
        body = (
            f"Joining {refs}, your busiest channels. First read-out {here} within the hour.\n_Remove me from any channel and I'll stay out._"
            if beachhead
            else f"Adding me across your public channels. I'll check in {here} once I'm caught up."
        )
    elif status == "done":
        if beachhead:
            body = (
                f"Caught up on {refs} — read-out {readout_at}."
                if beachhead.get("readout_posted")
                else f"I've read the last {PUBLIC_CHANNEL_ROLLOUT_DAYS} days of {refs}. Quiet week there, so not much to report yet, but I'm caught up and ready for questions."
            )
        else:
            body = "I'm across your public channels and caught up."
    else:
        body = "I hit a snag getting into channels. An admin can start me off again."

    button_label = None
    if admin:
        if status in ("idle", "failed"):
            button_label = "Add me to my public channels"
        elif status == "done":
            button_label = "Add me to the rest of my channels" if beachhead else "Check for new public channels"

    check = " ✅" if status == "done" else " ⚠️" if status == "failed" else ""
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*Getting to know your Slack*{check}\n{body}"}}]
    if button_label:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": PUBLIC_CHANNEL_ROLLOUT_ACTION_ID,
                        "value": "start",
                        "style": "primary",
                        "text": {"type": "plain_text", "text": button_label, "emoji": True},
                    }
                ],
            }
        )
    return blocks


def rollout_counts(team_id: str, run_id: str) -> dict[str, int]:
    rows = [r for r in _channels(team_id) if r["run_id"] == run_id]
    return {
        "discovered": len(rows),
        "joined": sum(1 for r in rows if r["join_status"] == "joined"),
        "ready": sum(1 for r in rows if r["stage"] == "done"),
        "introduced": sum(1 for r in rows if r.get("intro_ts")),
        "failed": sum(1 for r in rows if r["stage"] == "failed"),
    }


def get_public_channel_rollout_overview(team_id: str) -> dict | None:
    """Read-only snapshot; None until a rollout has ever started."""
    run = current_run(team_id)
    if not run:
        return None
    return {"status": run["status"], **rollout_counts(team_id, run["run_id"])}


def _refresh_rollout_card(team_id: str, client, status: RolloutStatus) -> None:
    run, card = current_run(team_id), rollout_card(team_id)
    if not run or not card:
        return
    channel_ids = _parse_json_array(run["channel_filter_json"]) if run.get("channel_filter_json") else None
    beachhead = None
    if channel_ids:
        # The read-out only posts when themes exist, so theme presence decides copy.
        has_themes = any(
            r["run_id"] == run["run_id"] and r["stage"] == "done" and r["theme_json"] != "[]" for r in _channels(team_id)
        )
        beachhead = {"channel_ids": channel_ids, "readout_posted": has_themes}
    client.update_slack_message(
        card["home_channel_id"], card["message_ts"], "Getting to know your Slack",
        public_channel_rollout_card_blocks(status, beachhead),
    )
    if card.get("admin_channel_id") and card.get("admin_message_ts"):
        client.update_slack_message(
            card["admin_channel_id"], card["admin_message_ts"], "Getting to know your Slack",
            public_channel_rollout_card_blocks(status, beachhead, {"home_channel_id": card["home_channel_id"]}),
        )
    card["updated_at"] = _now()
    _put(f"{_p(team_id)}:card", card)


def ensure_public_channel_rollout_card(payload: dict) -> None:
    """``payload``: team_id, home_channel_id, optional installer_slack_user_id and welcome {version, messages}."""
    team_id = payload["team_id"]
    existing = rollout_card(team_id)
    ws = _workspace(team_id)
    if ws is None:
        return
    _ensure_home_welcome(team_id, ws.client, payload)
    if existing and existing["team_id"] == team_id and existing["home_channel_id"] == payload["home_channel_id"]:
        return
    ts = ws.client.post_slack_message(
        payload["home_channel_id"], "Getting to know your Slack", None, public_channel_rollout_card_blocks("idle")
    )
    if not ts:
        return
    _put(
        f"{_p(team_id)}:card",
        {
            "team_id": team_id,
            "home_channel_id": payload["home_channel_id"],
            "message_ts": ts,
            "admin_channel_id": None,
            "admin_message_ts": None,
            "updated_at": _now(),
        },
    )


def ensure_admin_rollout_card(payload: dict) -> None:
    """The installer's DM copy, the one with the button.

    Posted from the greeting flow rather than bootstrap so it lands after the
    introduction instead of opening the DM with a button from a bot that has not
    said hello yet. ``payload``: team_id, installer_slack_user_id.
    """
    team_id = payload["team_id"]
    card = rollout_card(team_id)
    if not card or card.get("admin_channel_id"):
        return
    ws = _workspace(team_id)
    if ws is None:
        return
    admin = _post_admin_rollout_card(
        ws.client, card["home_channel_id"], _installer(ws, payload.get("installer_slack_user_id"))
    )
    if not admin:
        return
    card.update(admin_channel_id=admin[0], admin_message_ts=admin[1], updated_at=_now())
    _put(f"{_p(team_id)}:card", card)
    run = current_run(team_id)
    if run:
        _refresh_rollout_card(team_id, ws.client, run["status"])


def _post_admin_rollout_card(client, home_channel_id: str, installer_slack_user_id: str | None) -> tuple[str, str] | None:
    """The installer is the only admin we can resolve to a Slack DM at install time."""
    if not installer_slack_user_id:
        return None
    channel_id = client.open_slack_conversation(installer_slack_user_id)
    if not channel_id:
        return None
    message_ts = client.post_slack_message(
        channel_id, "Getting to know your Slack", None,
        public_channel_rollout_card_blocks("idle", admin={"home_channel_id": home_channel_id}),
    )
    return (channel_id, message_ts) if message_ts else None


# --- beachhead ---------------------------------------------------------------------------


def arm_public_channel_beachhead(payload: dict) -> None:
    """``payload``: team_id, optional installer_slack_user_id, notify_installer_dm."""
    scheduler.schedule(BEACHHEAD_DELAY_SECONDS, JOB_RUN_BEACHHEAD, payload)


def run_public_channel_beachhead(payload: dict) -> None:
    team_id = payload["team_id"]

    def bail(reason: str) -> None:
        log.info("[company-brain] beachhead skipped team=%s reason=%s", team_id, reason)

    card = rollout_card(team_id)
    if not card:
        return bail("no_card")
    active = current_run(team_id)
    # A prior finished run means channels were already handled; only block on those.
    if active:
        return bail(f"run_exists_{active['status']}")
    ws = _workspace(team_id)
    if ws is None:
        return bail("workspace_mismatch")
    scopes = _granted_scopes(ws)
    if scopes is not None and any(scope not in scopes for scope in REQUIRED_SCOPES):
        return bail("missing_scopes")

    picks = _pick_beachhead_channels(ws.client, card["home_channel_id"])
    if not picks:
        return bail("no_eligible_channels")

    installer = _installer(ws, payload.get("installer_slack_user_id"))
    profile = _lookup_user(ws.client, installer or "")
    # The installer may have left; the run still needs an owner for attribution.
    if not installer:
        return bail("no_actor")

    # The card's "joining <#a> and <#b>" state is the notice; dormant workspaces get a DM.
    if payload.get("notify_installer_dm") and installer:
        refs = " and ".join(f"<#{c['id']}>" for c in picks)
        _dm_slack_user_safe(
            ws.client,
            installer,
            f"I'm joining {refs}, your busiest {'channel' if len(picks) == 1 else 'channels'}, so I can actually be useful. First read-out in <#{card['home_channel_id']}> within the hour. Remove me from a channel anytime and I'll stay out.",
        )

    _begin_rollout_run(
        team_id,
        ws.client,
        home_channel_id=card["home_channel_id"],
        actor_slack_user_id=installer,
        actor_name=_actor_name(profile) if profile else "Company Brain",
        channel_filter=[c["id"] for c in picks],
    )
    log.info("[company-brain] beachhead join started team=%s channels=%s", team_id, ",".join(c["name"] for c in picks))


def _actor_name(profile) -> str:
    email = field(profile, "email") or ""
    return _rollout_actor_name(slack_user_display_name(profile) or email.split("@")[0] or "your admin")


def _pick_beachhead_channels(client, home_channel_id: str) -> list[dict]:
    candidates = []
    cursor = None
    for _ in range(25):
        page = client.list_slack_public_channels_page(cursor=cursor)
        if not field(page, "ok", False):
            return []
        for channel in field(page, "items", []) or []:
            if not is_eligible_public_channel(channel, home_channel_id=home_channel_id):
                continue
            if field(channel, "is_member", False):
                continue
            name = field(channel, "name", "")
            if BEACHHEAD_NAME_BLOCKLIST.search(name):
                continue
            candidates.append(
                {
                    "id": field(channel, "id"),
                    "name": name,
                    "members": field(channel, "member_count", 0) or 0,
                    "social": bool(BEACHHEAD_SOCIAL_NAMES.search(name)),
                }
            )
        if field(page, "complete", False):
            break
        cursor = field(page, "next_cursor")
    candidates.sort(key=lambda c: (c["social"], -c["members"]))
    return candidates[:BEACHHEAD_CHANNEL_COUNT]


def deterministic_uuid(seed: str) -> str:
    """Slack only honors client_msg_id for UUID-shaped ids; derive one so retries reuse it."""
    digest = bytearray(hashlib.sha256(seed.encode()).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def _post_beachhead_readout(team_id: str, run: dict, client) -> dict:
    """The payoff that earns the wider ask. Posts only when there are real themes;
    a quiet week is reported on the card instead, without a notification."""
    channels = sorted(
        (r for r in _channels(team_id) if r["run_id"] == run["run_id"] and r["stage"] == "done"),
        key=lambda r: r["name"],
    )
    sections = []
    for channel in channels:
        themes = _themes(channel["theme_json"])[:3]
        if not themes:
            continue
        # Model output from channel content: escape so it cannot mint <!channel>.
        bullets = "\n".join(f"*{escape_slack_text(t.title)}* — {escape_slack_text(t.summary)}" for t in themes)
        sections.append(f"*<#{channel['channel_id']}>*\n{bullets}")
    if not sections:
        return {"ok": True}
    refs = " and ".join(f"<#{c['channel_id']}>" for c in channels)
    joined_sections = "\n\n".join(sections)
    posted = client.post_slack_message_idempotent(
        run["home_channel_id"],
        f"Here's what I learned from {refs}",
        deterministic_uuid(f"beachhead-readout:{run['run_id']}"),
        [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Here's what I learned from {refs}*\n\n{joined_sections}"}},
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "That's two channels. An admin can add me to the rest, or invite me to any channel yourself.",
                    }
                ],
            },
        ],
    )
    if field(posted, "ok", False):
        return {"ok": True}
    return {"ok": False, "retry_after_seconds": field(posted, "retry_after_seconds"), "error": field(posted, "error")}


def _dm_slack_user_safe(client, slack_user_id: str, text: str) -> None:
    try:
        channel = client.open_slack_conversation(slack_user_id)
        if channel:
            client.post_slack_message(channel, text)
    except Exception:
        pass


# --- start (admin button) ----------------------------------------------------------------


def start_public_channel_rollout(payload: dict) -> None:
    """``payload``: team_id, home_channel_id (the channel the click came from), slack_user_id."""
    team_id = payload["team_id"]
    ws = _workspace(team_id)
    card = rollout_card(team_id)
    # The button lives on the admin DM copy, so the click arrives from there.
    from_card = card and payload["home_channel_id"] in (card["home_channel_id"], card.get("admin_channel_id"))
    if ws is None or not card or not from_card:
        return
    client = ws.client
    reply_channel = payload["home_channel_id"]
    user = payload["slack_user_id"]
    scopes = _granted_scopes(ws)
    missing = [s for s in REQUIRED_SCOPES if scopes is not None and s not in scopes]
    if missing:
        client.post_slack_ephemeral(
            reply_channel,
            user,
            f"This installation needs to be reauthorized before I can join and read public channels. Missing Slack scopes: {', '.join(missing)}. Reinstall the Company Brain Slack app, then try again.",
        )
        return
    profile = _lookup_user(client, user)
    if profile is None or not role_at_least(role_of(user), "admin"):
        client.post_slack_ephemeral(reply_channel, user, "Only a Company Brain organization admin can add me across the workspace.")
        return
    active = current_run(team_id)
    if active and active["status"] == "running":
        client.post_slack_ephemeral(
            reply_channel, user, "I’m already working through the public channels. I’ll keep this card updated."
        )
        return
    _begin_rollout_run(
        team_id, client, home_channel_id=card["home_channel_id"], actor_slack_user_id=user, actor_name=_actor_name(profile)
    )
    client.post_slack_ephemeral(
        reply_channel,
        user,
        "On it. I’ll join eligible public channels, ingest the last seven days, cross-check channel themes against organization-shared read-only tools, and introduce myself only after memory processing finishes.",
    )


def _begin_rollout_run(
    team_id: str,
    client,
    *,
    home_channel_id: str,
    actor_slack_user_id: str,
    actor_name: str,
    channel_filter: list[str] | None = None,
) -> None:
    """Shared by the button and the beachhead: reset prior run state, insert the run, schedule step one."""
    now = _now()
    run_id = uuid.uuid4().hex
    prefix = _p(team_id)
    stale = [f"{prefix}:channels"]
    for pattern in (f"{prefix}:threads:*", f"{prefix}:messages:*", f"{prefix}:documents:*"):
        stale.extend(kv().scan_iter(match=pattern))
    if stale:
        kv().delete(*stale)
    _put(
        f"{prefix}:run",
        {
            "run_id": run_id,
            "team_id": team_id,
            "home_channel_id": home_channel_id,
            "status": "running",
            "phase": "discover",
            "actor_user_id": actor_slack_user_id,
            "actor_slack_user_id": actor_slack_user_id or "",
            "actor_name": actor_name,
            "window_start_ms": now - PUBLIC_CHANNEL_ROLLOUT_DAYS * 24 * 60 * 60 * 1000,
            "window_end_ms": now,
            "list_cursor": None,
            "configured_servers_json": "[]",
            "used_servers_json": "[]",
            "failure_count": 0,
            "last_error": None,
            "channel_filter_json": json.dumps(channel_filter) if channel_filter else None,
            "created_at": now,
            "updated_at": now,
        },
    )
    _refresh_rollout_card(team_id, client, "running")
    _schedule_next(team_id, run_id)


def _schedule_next(team_id: str, run_id: str, delay_seconds: float = NEXT_STEP_DELAY_SECONDS) -> None:
    scheduler.schedule(delay_seconds, JOB_RUN_ROLLOUT, {"team_id": team_id, "run_id": run_id})


# --- message spool ------------------------------------------------------------------------


def _store_messages(team_id: str, run_id: str, channel_id: str, messages) -> None:
    prefix = _p(team_id)
    for raw in messages:
        message = as_dict(raw)
        ts = message.get("ts")
        if not ts:
            continue
        _hput(
            f"{prefix}:messages:{channel_id}",
            ts,
            {
                "ts": ts,
                "user": message.get("user"),
                "text": normalize_slack_message_content(message) or None,
                "bot_id": message.get("bot_id"),
                "subtype": message.get("subtype"),
                "app_id": message.get("app_id"),
                "thread_ts": message.get("thread_ts"),
                "reply_count": message.get("reply_count"),
                "files": message.get("files") or [],
                "reactions": message.get("reactions") or [],
            },
        )
        if is_slack_history_thread_root(message) and kv().hget(f"{prefix}:threads:{channel_id}", ts) is None:
            _hput(
                f"{prefix}:threads:{channel_id}",
                ts,
                {"run_id": run_id, "thread_ts": ts, "cursor": None, "status": "pending", "attempts": 0},
            )


def _stored_messages(team_id: str, channel_id: str) -> list[dict]:
    rows = _hall(f"{_p(team_id)}:messages:{channel_id}")
    rows.sort(key=lambda r: float(r["ts"]))
    return [{k: v for k, v in row.items() if v is not None} for row in rows]


def _next_channel(team_id: str, run_id: str, where: Literal["join", "collect", "crosscheck", "introduce"]) -> dict | None:
    def matches(row: dict) -> bool:
        if where == "join":
            return row["join_status"] == "pending"
        if where == "collect":
            return row["join_status"] == "joined" and row["stage"] in ("history", "threads", "package", "enqueue")
        if where == "crosscheck":
            return row["stage"] == "crosscheck"
        return row["stage"] == "wait_memory"

    rows = sorted((r for r in _channels(team_id) if r["run_id"] == run_id and matches(r)), key=lambda r: r["name"])
    return rows[0] if rows else None


# --- steps --------------------------------------------------------------------------------


def _discover_step(run: dict, client) -> None:
    team_id, run_id = run["team_id"], run["run_id"]
    page = client.list_slack_public_channels_page(cursor=run.get("list_cursor"))
    if not field(page, "ok", False):
        retry_after = field(page, "retry_after_seconds")
        if retry_after:
            _schedule_next(team_id, run_id, retry_after)
            return
        raise RuntimeError(f"channel_discovery:{field(page, 'error')}")
    now = _now()
    # Beachhead runs pin discovery to the exact channels named in the notice.
    channel_filter = set(_parse_json_array(run["channel_filter_json"])) if run.get("channel_filter_json") else None
    for channel in field(page, "items", []) or []:
        channel_id = field(channel, "id")
        if channel_filter is not None and channel_id not in channel_filter:
            continue
        if not is_eligible_public_channel(channel, home_channel_id=run["home_channel_id"]):
            continue
        if _channel(team_id, channel_id) is not None:
            continue
        introduced = _introduction(team_id, channel_id)
        # Already-introduced channels skip history/theme/crosscheck; beachhead runs
        # re-read regardless, since the read-out is the whole point.
        already_introduced = channel_filter is None and introduced is not None
        # Introduced before themes were stored durably: re-derive them in place.
        # Members only, so a channel the bot was removed from is never rejoined.
        needs_theme = (
            already_introduced
            and bool(field(channel, "is_member", False))
            and (not introduced.get("theme_json") or introduced["theme_json"] == "[]")
        )
        settled = already_introduced and not needs_theme
        _hput(
            f"{_p(team_id)}:channels",
            channel_id,
            {
                "run_id": run_id,
                "channel_id": channel_id,
                "name": field(channel, "name"),
                "topic": field(channel, "topic"),
                "purpose": field(channel, "purpose"),
                "was_member": bool(field(channel, "is_member", False)),
                "already_introduced": already_introduced,
                "theme_only": needs_theme,
                "join_status": "joined" if settled else "pending",
                "stage": "done" if settled else "history",
                "history_cursor": None,
                "theme_json": "[]",
                "check_json": "[]",
                "intro_client_id": str(uuid.uuid4()),
                "intro_ts": None,
                "attempts": 0,
                "last_error": None,
                "created_at": now,
                "updated_at": now,
            },
        )
    if field(page, "complete", False):
        _update_run(team_id, run_id, phase="join", list_cursor=None, updated_at=now)
        _refresh_rollout_card(team_id, client, "running")
    else:
        _update_run(team_id, run_id, list_cursor=field(page, "next_cursor"), updated_at=now)
    _schedule_next(team_id, run_id)


def _join_step(run: dict, client) -> None:
    team_id, run_id = run["team_id"], run["run_id"]
    channel = _next_channel(team_id, run_id, "join")
    if channel is None:
        _update_run(team_id, run_id, phase="collect", updated_at=_now())
        _refresh_rollout_card(team_id, client, "running")
        _schedule_next(team_id, run_id)
        return
    cid = channel["channel_id"]
    if channel["was_member"]:
        _update_channel(team_id, run_id, cid, join_status="joined", attempts=0)
        _schedule_next(team_id, run_id)
        return
    joined = client.join_slack_public_channel(cid)
    if not field(joined, "ok", False) and field(joined, "retry_after_seconds"):
        _schedule_next(team_id, run_id, field(joined, "retry_after_seconds"))
        return
    if not field(joined, "ok", False):
        error = field(joined, "error")
        attempts = channel["attempts"] + 1
        if attempts < MAX_STAGE_ATTEMPTS and error != "missing_scope":
            _update_channel(team_id, run_id, cid, attempts=attempts, last_error=error)
            _schedule_next(team_id, run_id, min(2**attempts, 60))
            return
        _update_channel(team_id, run_id, cid, join_status="failed", stage="failed", attempts=attempts, last_error=error)
    else:
        _update_channel(team_id, run_id, cid, join_status="joined", attempts=0, last_error=None)
        invalidate_channel_directory(team_id)
    _schedule_next(team_id, run_id)


def _package_channel(run: dict, channel: dict, bot_user_id: str | None) -> None:
    team_id, run_id, cid = run["team_id"], run["run_id"], channel["channel_id"]
    messages = _stored_messages(team_id, cid)
    # Theme recovery reads to derive a theme and stops there; this channel's
    # history was ingested by the run that introduced it.
    if not channel["theme_only"]:
        docs = build_slack_history_documents(
            SlackHistoryDocumentInput(
                team_id=team_id,
                channel_id=cid,
                channel_name=channel["name"],
                topic=channel.get("topic"),
                purpose=channel.get("purpose"),
                window_start_ms=run["window_start_ms"],
                window_end_ms=run["window_end_ms"],
                bot_user_id=bot_user_id,
                messages=messages,
            )
        )
        for index, doc in enumerate(docs):
            _hput(
                f"{_p(team_id)}:documents:{cid}",
                doc.custom_id,
                {
                    "custom_id": doc.custom_id,
                    "ord": index,
                    "content": doc.content,
                    "metadata_json": json.dumps(doc.metadata),
                    "document_id": None,
                    "status": "pending",
                    "attempts": 0,
                    "submitted_at": None,
                },
            )
    window = (run["window_start_ms"], run["window_end_ms"])
    evidence = recent_slack_channel_evidence(messages, bot_user_id, 40, window)
    valid_ts = []
    for message in messages:
        try:
            ms = float(message["ts"]) * 1000
        except (KeyError, ValueError):
            continue
        if window[0] <= ms <= window[1]:
            valid_ts.append(message["ts"])
    themes = extract_channel_themes(
        channel_id=cid,
        channel_name=channel["name"],
        topic=channel.get("topic"),
        purpose=channel.get("purpose"),
        evidence=evidence,
        valid_message_ts=valid_ts,
    )
    theme_json = json.dumps([asdict(t) for t in themes])
    # Recovery only wants the theme: ingesting again would mint fresh document ids
    # for the same days, since the id hashes this run's window bounds.
    if channel["theme_only"]:
        intro = _introduction(team_id, cid)
        if intro:
            intro["theme_json"] = theme_json
            _hput(f"{_p(team_id)}:introductions", cid, intro)
        _update_channel(team_id, run_id, cid, stage="done", theme_json=theme_json, attempts=0, last_error=None)
        kv().delete(f"{_p(team_id)}:messages:{cid}")
        return
    _update_channel(team_id, run_id, cid, stage="enqueue", theme_json=theme_json, attempts=0, last_error=None)


def _submit_document(run: dict, channel: dict, doc_row: dict) -> None:
    if not doc_row.get("content"):
        raise RuntimeError("history_document_missing_content")
    metadata = json.loads(doc_row["metadata_json"])
    result = upsert_brain_document(
        content=doc_row["content"],
        container_tag=SHARED_TEAM_BRAIN_CONTAINER_TAG,
        custom_id=doc_row["custom_id"],
        metadata=metadata,
        title=metadata.get("title"),
        author_id=run["actor_user_id"] or None,
        channel_id=channel["channel_id"],
        ts=metadata.get("latest_message_ts"),
    )
    if not result.id or result.status not in ("queued", "done"):
        raise RuntimeError(f"history_ingest_{result.status}")
    doc_row.update(document_id=result.id, status="submitted", content=None, submitted_at=_now(), attempts=0)
    _hput(f"{_p(run['team_id'])}:documents:{channel['channel_id']}", doc_row["custom_id"], doc_row)


def _collect_step(run: dict, client, bot_user_id: str | None) -> None:
    team_id, run_id = run["team_id"], run["run_id"]
    channel = _next_channel(team_id, run_id, "collect")
    if channel is None:
        _update_run(team_id, run_id, phase="crosscheck", updated_at=_now())
        _refresh_rollout_card(team_id, client, "running")
        _schedule_next(team_id, run_id)
        return
    cid = channel["channel_id"]

    if channel["stage"] == "history":
        page = client.get_slack_channel_history_page(
            cid,
            oldest=str(run["window_start_ms"] / 1000),
            latest=str(run["window_end_ms"] / 1000),
            cursor=channel.get("history_cursor"),
        )
        if not field(page, "ok", False):
            retry_after = field(page, "retry_after_seconds")
            if retry_after:
                _schedule_next(team_id, run_id, retry_after)
                return
            attempts = channel["attempts"] + 1
            _update_channel(
                team_id, run_id, cid,
                attempts=attempts,
                stage="failed" if attempts >= MAX_STAGE_ATTEMPTS else "history",
                last_error=f"channel_history:{field(page, 'error')}",
            )
            _schedule_next(team_id, run_id, min(2**attempts, 60))
            return
        _store_messages(team_id, run_id, cid, field(page, "items", []) or [])
        complete = bool(field(page, "complete", False))
        _update_channel(
            team_id, run_id, cid,
            history_cursor=field(page, "next_cursor"),
            stage="threads" if complete else "history",
            attempts=0,
        )
        _schedule_next(team_id, run_id)
        return

    if channel["stage"] == "threads":
        threads_key = f"{_p(team_id)}:threads:{cid}"
        pending = sorted(
            (t for t in _hall(threads_key) if t["status"] in ("pending", "reading")),
            key=lambda t: float(t["thread_ts"]),
        )
        if not pending:
            _update_channel(team_id, run_id, cid, stage="package")
            _schedule_next(team_id, run_id)
            return
        thread = pending[0]
        page = client.get_slack_thread_history_page(cid, thread["thread_ts"], cursor=thread.get("cursor"))
        if not field(page, "ok", False):
            retry_after = field(page, "retry_after_seconds")
            if retry_after:
                _schedule_next(team_id, run_id, retry_after)
                return
            attempts = thread["attempts"] + 1
            if attempts < MAX_STAGE_ATTEMPTS:
                thread["attempts"] = attempts
                _hput(threads_key, thread["thread_ts"], thread)
                _schedule_next(team_id, run_id, min(2**attempts, 60))
                return
            thread.update(status="failed", attempts=attempts)
            _hput(threads_key, thread["thread_ts"], thread)
            _update_channel(team_id, run_id, cid, stage="failed", last_error=f"thread_history:{field(page, 'error')}")
        else:
            _store_messages(team_id, run_id, cid, field(page, "items", []) or [])
            thread.update(
                cursor=field(page, "next_cursor"),
                status="done" if field(page, "complete", False) else "reading",
                attempts=0,
            )
            _hput(threads_key, thread["thread_ts"], thread)
        _schedule_next(team_id, run_id)
        return

    if channel["stage"] == "package":
        _package_channel(run, channel, bot_user_id)
        _schedule_next(team_id, run_id)
        return

    docs_key = f"{_p(team_id)}:documents:{cid}"
    docs = sorted(_hall(docs_key), key=lambda d: d["ord"])
    pending_doc = next((d for d in docs if d["status"] == "pending"), None)
    if pending_doc:
        try:
            _submit_document(run, channel, pending_doc)
        except Exception as error:
            attempts = pending_doc["attempts"] + 1
            pending_doc.update(attempts=attempts, status="failed" if attempts >= MAX_STAGE_ATTEMPTS else "pending")
            _hput(docs_key, pending_doc["custom_id"], pending_doc)
            if attempts >= MAX_STAGE_ATTEMPTS:
                _update_channel(team_id, run_id, cid, stage="failed", last_error=_rollout_error_code(error))
            _schedule_next(team_id, run_id, min(2**attempts, 60))
            return
        _schedule_next(team_id, run_id)
        return
    failed = any(d["status"] == "failed" for d in docs)
    _update_channel(team_id, run_id, cid, stage="failed" if failed else "crosscheck")
    _schedule_next(team_id, run_id)


def _crosscheck_step(run: dict) -> None:
    team_id, run_id = run["team_id"], run["run_id"]
    channels = sorted(
        (r for r in _channels(team_id) if r["run_id"] == run_id and r["stage"] == "crosscheck"), key=lambda r: r["name"]
    )[:CROSSCHECK_CHANNEL_BATCH]
    if not channels:
        _update_run(team_id, run_id, phase="introduce", updated_at=_now())
        _schedule_next(team_id, run_id)
        return
    themes = [t for c in channels for t in _themes(c["theme_json"])]
    checked = crosscheck_themes_with_connected_tools(themes)
    for channel in channels:
        theme_ids = {t.id for t in _themes(channel["theme_json"])}
        checks = [asdict(c) for c in checked["checks"] if c.theme_id in theme_ids]
        _update_channel(team_id, run_id, channel["channel_id"], stage="wait_memory", check_json=json.dumps(checks))
    configured = list(dict.fromkeys(_parse_json_array(run["configured_servers_json"]) + checked["configured_servers"]))
    used = list(dict.fromkeys(_parse_json_array(run["used_servers_json"]) + checked["used_servers"]))
    _update_run(
        team_id, run_id,
        configured_servers_json=json.dumps(configured),
        used_servers_json=json.dumps(used),
        updated_at=_now(),
    )
    _schedule_next(team_id, run_id)


def _memory_ready(run: dict, channel: dict) -> Literal["ready", "waiting", "failed"]:
    docs_key = f"{_p(run['team_id'])}:documents:{channel['channel_id']}"
    docs = _hall(docs_key)
    if not docs:
        return "ready"
    if any(d["status"] == "failed" or not d.get("document_id") for d in docs):
        return "failed"
    ids = [d["document_id"] for d in docs]
    rows = list(document_statuses(ids).values())
    if any(row.status == "failed" for row in rows):
        return "failed"
    if len(rows) == len(ids) and all(row.status == "done" for row in rows):
        for doc in docs:
            doc["status"] = "done"
            _hput(docs_key, doc["custom_id"], doc)
        return "ready"
    oldest = min(d.get("submitted_at") or _now() for d in docs)
    return "failed" if _now() - oldest > MAX_MEMORY_WAIT_MS else "waiting"


def _introduce_step(run: dict, client) -> None:
    team_id, run_id = run["team_id"], run["run_id"]
    channel = _next_channel(team_id, run_id, "introduce")
    if channel is None:
        counts = rollout_counts(team_id, run_id)
        # Deliver before the run flips to done, or a failed post is never retried
        # while the card still claims the read-out happened.
        if run.get("channel_filter_json"):
            readout = _post_beachhead_readout(team_id, run, client)
            if not readout["ok"]:
                if readout.get("retry_after_seconds"):
                    _schedule_next(team_id, run_id, readout["retry_after_seconds"])
                    return
                raise RuntimeError("beachhead_readout_failed")
        _update_run(team_id, run_id, status="done", updated_at=_now(), failure_count=0)
        _refresh_rollout_card(team_id, client, "done")
        log.info(
            "[company-brain] public-channel rollout done run=%s discovered=%d joined=%d introduced=%d failed=%d",
            run_id, counts["discovered"], counts["joined"], counts["introduced"], counts["failed"],
        )
        return
    cid = channel["channel_id"]
    ready = _memory_ready(run, channel)
    if ready == "waiting":
        _schedule_next(team_id, run_id, MEMORY_POLL_DELAY_SECONDS)
        return
    if ready == "failed":
        _update_channel(team_id, run_id, cid, stage="failed", last_error="memory_processing_failed")
        _schedule_next(team_id, run_id)
        return

    if channel["already_introduced"]:
        # Re-read for a channel whose durable theme was lost: keep the theme, skip the intro.
        if channel["theme_json"] and channel["theme_json"] != "[]":
            intro = _introduction(team_id, cid)
            if intro:
                intro["theme_json"] = channel["theme_json"]
                _hput(f"{_p(team_id)}:introductions", cid, intro)
        _update_channel(team_id, run_id, cid, stage="done")
        kv().delete(f"{_p(team_id)}:messages:{cid}")
        _schedule_next(team_id, run_id)
        return

    introduction = compose_channel_introduction(
        channel_name=channel["name"],
        installer_name=run["actor_name"],
        themes=_themes(channel["theme_json"]),
        checks=_checks(channel["check_json"]),
    )
    posted = client.post_slack_message_idempotent(
        cid,
        introduction,
        channel["intro_client_id"],
        [
            {"type": "section", "text": {"type": "mrkdwn", "text": introduction[:2900]}},
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "I'll chime in when the conversation is something I can help with. Not wanted in this channel? Remove me and I'll stay out.",
                    }
                ],
            },
        ],
    )
    if not field(posted, "ok", False):
        retry_after = field(posted, "retry_after_seconds")
        if retry_after:
            _schedule_next(team_id, run_id, retry_after)
            return
        attempts = channel["attempts"] + 1
        error = field(posted, "error")
        if attempts < MAX_STAGE_ATTEMPTS:
            _update_channel(team_id, run_id, cid, attempts=attempts, last_error=error)
            _schedule_next(team_id, run_id, min(2**attempts, 60))
            return
        _update_channel(team_id, run_id, cid, stage="failed", attempts=attempts, last_error=error)
        _schedule_next(team_id, run_id)
        return
    now = _now()
    ts = field(posted, "ts")
    _hput(
        f"{_p(team_id)}:introductions",
        cid,
        {"team_id": team_id, "channel_id": cid, "intro_ts": ts, "introduced_at": now, "theme_json": channel["theme_json"]},
    )
    _update_channel(team_id, run_id, cid, stage="done", intro_ts=ts, attempts=0, last_error=None)
    kv().delete(f"{_p(team_id)}:messages:{cid}")
    _schedule_next(team_id, run_id)


def is_channel_awaiting_rollout_introduction(team_id: str, channel_id: str) -> bool:
    run = current_run(team_id)
    if not run or run["status"] != "running":
        return False
    row = _channel(team_id, channel_id)
    return bool(
        row and row["run_id"] == run["run_id"] and not row["already_introduced"] and row["stage"] not in ("done", "failed")
    )


def run_public_channel_rollout(payload: dict) -> None:
    """One rollout step. ``payload``: team_id, run_id."""
    team_id = payload["team_id"]
    run = current_run(team_id)
    if not run or run["run_id"] != payload["run_id"] or run["status"] != "running":
        return
    try:
        ws = _workspace(team_id)
        if ws is None:
            raise RuntimeError("workspace_not_found")
        phase = run["phase"]
        if phase == "discover":
            _discover_step(run, ws.client)
        elif phase == "join":
            _join_step(run, ws.client)
        elif phase == "collect":
            _collect_step(run, ws.client, ws.bot_user_id)
        elif phase == "crosscheck":
            _crosscheck_step(run)
        else:
            _introduce_step(run, ws.client)
        latest = current_run(team_id)
        if latest and latest["run_id"] == run["run_id"] and latest["status"] == "running":
            _update_run(team_id, run["run_id"], failure_count=0, last_error=None)
    except Exception as error:
        message = _rollout_error_code(error)
        failures = run["failure_count"] + 1
        log.warning(
            "[company-brain] public-channel rollout step failed run=%s phase=%s failures=%d: %s",
            run["run_id"], run["phase"], failures, message,
        )
        if failures >= MAX_RUN_FAILURES:
            _update_run(team_id, run["run_id"], status="failed", failure_count=failures, last_error=message, updated_at=_now())
            try:
                ws = _workspace(team_id)
                if ws is not None:
                    _refresh_rollout_card(team_id, ws.client, "failed")
            except Exception:
                pass
            return
        _update_run(team_id, run["run_id"], failure_count=failures, last_error=message, updated_at=_now())
        _schedule_next(team_id, run["run_id"], min(2**failures, 5 * 60))


# --- interaction ---------------------------------------------------------------------------


def parse_public_channel_rollout_interaction_payload(raw_body: bytes) -> dict | None:
    """Port of ``parsePublicChannelRolloutInteractionPayload`` from ``routes/slack/index.ts``."""
    payload = parse_qs(raw_body.decode("utf-8", "replace")).get("payload", [None])[0]
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or parsed.get("type") != "block_actions":
        return None
    actions = parsed.get("actions") or [{}]
    if (actions[0] or {}).get("action_id") != PUBLIC_CHANNEL_ROLLOUT_ACTION_ID:
        return None
    team_id = (parsed.get("team") or {}).get("id")
    slack_user_id = (parsed.get("user") or {}).get("id")
    home_channel_id = (parsed.get("channel") or {}).get("id")
    if not (isinstance(team_id, str) and isinstance(slack_user_id, str) and isinstance(home_channel_id, str)):
        return None
    return {"team_id": team_id, "slack_user_id": slack_user_id, "home_channel_id": home_channel_id}


def handle_public_channel_rollout_interaction(raw_body: bytes) -> bool:
    """Route hook: True when the body was the rollout button (and the rollout was started)."""
    parsed = parse_public_channel_rollout_interaction_payload(raw_body)
    if parsed is None:
        return False
    start_public_channel_rollout(parsed)
    return True


def register_jobs() -> None:
    scheduler.register(JOB_RUN_ROLLOUT, run_public_channel_rollout)
    scheduler.register(JOB_RUN_BEACHHEAD, run_public_channel_beachhead)


register_jobs()
