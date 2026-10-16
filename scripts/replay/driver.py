"""Replays the scripted workspace into the app through its real routes.

Builds Slack event envelopes and interaction payloads, signs them with the app's
signing secret the way Slack does, and posts them to ``/slack/events`` and
``/slack/interactions``. ``http`` is anything with ``post(path, content=, headers=)``:
the in-process Starlette ``TestClient`` (background tasks run before the response
returns, so replies are there when ``post`` returns) or an ``httpx.Client`` aimed at a
running instance.

With a fake workspace attached, every human message is recorded there too, so the bot
reads back whole threads and its replies can be collected. Against a real workspace,
``ids`` maps the scripted user and channel ids to the real ones and the replies land
in Slack.

GitHub history goes in either through the signed webhook (``post_installation_event``,
with ``client_for`` patched to ``FakeGitHubClient``) or straight through the backfill
service with the fake client (``load_github_history``), for an instance that shares
the graph.
"""

import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import urlencode

from src.seed import quillfern_workspace as script
from src.seed.quillfern_workspace import DemoStep, Repo, WorkspaceMessage
from scripts.replay.github_api import FakeGitHubClient, repo_payload
from scripts.replay.slack_api import FakeSlackChannel, FakeSlackMessage, FakeSlackUser, FakeSlackWorkspace, dm_channel_id


@dataclass
class Replayed:
    """One scripted message after replay: where it went and what the bot said back."""

    message: WorkspaceMessage
    channel: str
    ts: str | None
    thread_ts: str | None
    status: int
    replies: list[str] = field(default_factory=list)


def _require_app_pointed_at(base_url: str | None) -> None:
    """Refuse to replay against a fake unless the app's Slack client really targets it."""
    from src.integrations.slack import client as slack_client

    configured = slack_client.settings.slack_api_base_url
    if not base_url or configured != base_url:
        raise RuntimeError(
            f"the app's Slack client targets {configured!r}, not the fake at {base_url!r}; "
            "set SLACK_API_BASE_URL before anything imports src"
        )


def fake_workspace_from_script() -> FakeSlackWorkspace:
    """The scripted Quillfern workspace as fake Slack state."""
    fake = FakeSlackWorkspace(
        team_id=script.TEAM_ID, team_name=script.TEAM_NAME, bot_user_id=script.BOT_USER_ID, bot_token=script.BOT_TOKEN
    )
    for user in script.USERS:
        fake.add_user(FakeSlackUser(id=user.id, name=user.name, real_name=user.real_name, email=user.email))
        fake.open_dm(user.id)
    for channel in script.CHANNELS:
        fake.add_channel(FakeSlackChannel(id=channel.id, name=channel.name, is_private=channel.is_private, members=list(channel.members)))
    return fake


def register_fake_workspace() -> None:
    """Store the scripted workspace as an installed app, so events for its team are handled."""
    from src.domain.slack.token_crypto import encrypt_token
    from src.domain.slack.workspace import upsert_workspace

    upsert_workspace(
        team_id=script.TEAM_ID,
        bot_token_enc=encrypt_token(script.BOT_TOKEN),
        bot_user_id=script.BOT_USER_ID,
        team_name=script.TEAM_NAME,
        installed_by=script.USERS[0].id,
    )


def clear_team_state(replayer: "WorkspaceReplayer | None" = None, team_id: str = script.TEAM_ID) -> None:
    """Drop the scripted team's queued state (reaction outbox, thread turns, bot threads,
    approval expiry jobs) from the shared Redis, so nothing from a replay leaks into
    whatever runs next on the same FalkorDB."""
    from src.core import scheduler
    from src.domain.slack.channels.reset import reset_channel_state
    from src.domain.slack.turn.approval_expiry import _job_id
    from src.domain.slack.turn.reset import reset_turn_state

    reset_turn_state(team_id)
    reset_channel_state(team_id)
    for approval_id in (replayer.approval_ids if replayer else []):
        scheduler.cancel(_job_id(approval_id))


def load_github_history(repos: Iterable[Repo] = script.REPOS, installation_id: int = script.INSTALLATION_ID) -> list[dict]:
    """Backfill the scripted repos through the real backfill service with the fake client."""
    from src.domain.github import backfill

    client = FakeGitHubClient(repos, installation_id)
    return [backfill.backfill_repo(client, repo_payload(repo)) for repo in repos]


def _approve_action_id() -> str:
    # Imported late: loading the Slack client builds the app settings, and the loader must
    # set SLACK_API_BASE_URL before that happens.
    from src.integrations.slack.client import APPROVAL_APPROVE_ACTION_ID

    return APPROVAL_APPROVE_ACTION_ID


def is_approval_card(message: FakeSlackMessage) -> bool:
    return any(
        element.get("action_id") == _approve_action_id()
        for block in message.blocks or []
        for element in block.get("elements") or []
    )


def reply_text(message: FakeSlackMessage) -> str:
    """What a reader sees: the markdown blocks when the bot sent them, else the plain-text fallback."""
    parts = []
    for block in message.blocks or []:
        if block.get("type") == "markdown" and block.get("text"):
            parts.append(block["text"])
        elif block.get("type") == "section" and isinstance(block.get("text"), dict) and block["text"].get("text"):
            parts.append(block["text"]["text"])
    return "\n".join(parts) if parts else message.text


def slack_signature(secret: str, body: bytes, timestamp: int | None = None) -> dict:
    ts = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), f"v0:{ts}:{body.decode()}".encode(), hashlib.sha256).hexdigest()
    return {"x-slack-signature": f"v0={digest}", "x-slack-request-timestamp": str(ts)}


def github_signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class WorkspaceReplayer:
    def __init__(
        self,
        http,
        *,
        signing_secret: str,
        team_id: str = script.TEAM_ID,
        bot_user_id: str = script.BOT_USER_ID,
        fake: FakeSlackWorkspace | None = None,
        fake_base_url: str | None = None,
        ids: dict[str, str] | None = None,
        response_url: str = "https://example.invalid/response",
    ):
        if fake is not None:
            _require_app_pointed_at(fake_base_url)
        self.http = http
        self.signing_secret = signing_secret
        self.team_id = team_id
        self.bot_user_id = bot_user_id
        self.fake = fake
        self.ids = ids or {}
        self.response_url = response_url
        self.approval_ids: list[str] = []
        self._last_ask: dict[str, str] = {}

    # --- plumbing --------------------------------------------------------------------------

    def _id(self, key: str) -> str:
        return self.ids.get(key, key)

    def _ts(self) -> str:
        return self.fake.next_ts() if self.fake else f"{time.time():.6f}"

    @staticmethod
    def _channel_type(channel: str) -> str:
        if channel.startswith("D"):
            return "im"
        return "group" if channel.startswith("G") else "channel"

    def _post(self, path: str, body: bytes, content_type: str) -> int:
        headers = {"content-type": content_type, **slack_signature(self.signing_secret, body)}
        return self.http.post(path, content=body, headers=headers).status_code

    def send_event(self, event: dict) -> int:
        envelope = {
            "token": "replay",
            "team_id": self.team_id,
            "api_app_id": "AREPLAY",
            "event": event,
            "type": "event_callback",
            "event_id": f"Ev{uuid.uuid4().hex[:16].upper()}",
            "event_time": int(time.time()),
        }
        return self._post("/slack/events", json.dumps(envelope).encode(), "application/json")

    def _record(self, channel: str, user: str, text: str, ts: str, thread_ts: str | None) -> None:
        if self.fake is not None:
            self.fake.say(channel, user, text, thread_ts=thread_ts, ts=ts)

    def replies(self, channel: str, thread_ts: str) -> list[str]:
        if self.fake is None:
            return []
        return [reply_text(m) for m in self.fake.bot_replies(channel, thread_ts) if not is_approval_card(m)]

    def replies_after(self, channel: str, ts: str) -> list[str]:
        if self.fake is None:
            return []
        return [reply_text(m) for m in self.fake.bot_replies(channel) if m.ts > ts and not is_approval_card(m)]

    def last_approval_id(self, channel: str) -> str | None:
        """The approval id on the newest Approve button the bot posted in ``channel``."""
        if self.fake is None:
            return None
        for message in reversed(self.fake.messages_in(channel)):
            if message.user != self.bot_user_id:
                continue
            for block in message.blocks or []:
                for element in block.get("elements") or []:
                    if element.get("action_id") == _approve_action_id() and element.get("value"):
                        return element["value"]
        return None

    # --- one message each --------------------------------------------------------------------

    def say(self, message: WorkspaceMessage, thread_ts: str | None = None) -> Replayed:
        channel, user = self._id(message.channel), self._id(message.user)
        ts = self._ts()
        self._record(channel, user, message.text, ts, thread_ts)
        event = {
            "type": "message",
            "user": user,
            "text": message.text,
            "ts": ts,
            "channel": channel,
            "channel_type": self._channel_type(channel),
            "event_ts": ts,
        }
        if thread_ts:
            event["thread_ts"] = thread_ts
        status = self.send_event(event)
        replies = self.replies(channel, thread_ts) if thread_ts else self.replies_after(channel, ts)
        return Replayed(message, channel, ts, thread_ts, status, replies)

    def ask(self, message: WorkspaceMessage, thread_ts: str | None = None) -> Replayed:
        """@mention the bot, at the top level or inside a thread (Slack sends app_mention for both)."""
        channel, user = self._id(message.channel), self._id(message.user)
        ts = self._ts()
        text = f"<@{self.bot_user_id}> {message.text}"
        self._record(channel, user, text, ts, thread_ts)
        event = {"type": "app_mention", "user": user, "text": text, "ts": ts, "channel": channel, "event_ts": ts}
        if thread_ts:
            event["thread_ts"] = thread_ts
        before = len(self.replies(channel, thread_ts)) if thread_ts else 0
        status = self.send_event(event)
        if thread_ts is None:
            self._last_ask[channel] = ts
        replies = self.replies(channel, thread_ts or ts)[before:]
        return Replayed(message, channel, ts, thread_ts or ts, status, replies)

    def follow_up(self, message: WorkspaceMessage) -> Replayed:
        """A follow-up in the thread of the last ask. It mentions the bot: without a model, a plain
        thread reply is passive context and gets no answer."""
        channel = self._id(message.channel)
        thread_ts = self._last_ask.get(channel)
        if thread_ts is None:
            raise ValueError(f"follow_up in {channel} before any ask")
        return self.ask(message, thread_ts)

    def dm(self, message: WorkspaceMessage) -> Replayed:
        user = self._id(message.user)
        channel = self.ids.get(dm_channel_id(message.user), dm_channel_id(user))
        if self.fake is not None:
            self.fake.open_dm(user)
        return self.say(WorkspaceMessage(channel, user, message.text, "dm"))

    def approve(self, message: WorkspaceMessage) -> Replayed:
        channel, user = self._id(message.channel), self._id(message.user)
        thread_ts = self._last_ask.get(channel)
        approval_id = self.last_approval_id(channel)
        if approval_id is None:
            return Replayed(message, channel, None, thread_ts, 0)
        self.approval_ids.append(approval_id)
        payload = {
            "type": "block_actions",
            "team": {"id": self.team_id},
            "user": {"id": user},
            "channel": {"id": channel},
            "actions": [{"action_id": _approve_action_id(), "value": approval_id, "type": "button"}],
            "response_url": self.response_url,
        }
        before = len(self.replies(channel, thread_ts)) if thread_ts else 0
        status = self._post("/slack/interactions", urlencode({"payload": json.dumps(payload)}).encode(), "application/x-www-form-urlencoded")
        replies = self.replies(channel, thread_ts)[before:] if thread_ts else []
        return Replayed(message, channel, None, thread_ts, status, replies)

    # --- whole scripts -------------------------------------------------------------------------

    def play(self, messages: Iterable[WorkspaceMessage]) -> list[Replayed]:
        handlers = {"say": self.say, "ask": self.ask, "follow_up": self.follow_up, "dm": self.dm, "approve": self.approve}
        return [handlers[message.kind](message) for message in messages]

    def play_steps(self, steps: Iterable[DemoStep]) -> list[tuple[DemoStep, list[Replayed]]]:
        return [(step, self.play(step.messages)) for step in steps]

    # --- GitHub -----------------------------------------------------------------------------

    def post_installation_event(self, webhook_secret: str, installation_id: int = script.INSTALLATION_ID, repos: Iterable[Repo] = script.REPOS) -> int:
        """The ``installation created`` webhook, which makes the app backfill every scripted repo."""
        payload = {
            "action": "created",
            "installation": {"id": installation_id},
            "repositories": [{"id": repo.id, "full_name": repo.full_name} for repo in repos],
        }
        body = json.dumps(payload).encode()
        headers = {
            "X-GitHub-Event": "installation",
            "X-GitHub-Delivery": f"replay-{uuid.uuid4().hex}",
            "X-Hub-Signature-256": github_signature(webhook_secret, body),
            "Content-Type": "application/json",
        }
        return self.http.post("/github/webhook", content=body, headers=headers).status_code
