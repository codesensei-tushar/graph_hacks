"""A fake Slack Web API for tests and the demo replay.

Serves Slack-shaped JSON over plain HTTP on a local port, so the real ``slack_sdk``
client (pointed here through ``SLACK_API_BASE_URL``) exercises the whole integration
layer. It holds one workspace: users, channels, messages, threads, reactions,
ephemerals, and native streams. Every call is recorded in ``workspace.calls``.

Errors come back Slack-style, ``{"ok": false, "error": "channel_not_found"}`` with
HTTP 200, which the SDK raises as ``SlackApiError`` exactly like the real service.

Provider-specific and logic-free: it stores what it is told and answers what it is
asked. Nothing here knows about the company graph, and nothing here imports ``src``.
"""

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


@dataclass
class FakeSlackUser:
    id: str
    name: str
    real_name: str
    email: str | None = None
    is_bot: bool = False
    is_restricted: bool = False

    def payload(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "real_name": self.real_name,
            "deleted": False,
            "is_bot": self.is_bot,
            "is_restricted": self.is_restricted,
            "is_ultra_restricted": False,
            "is_stranger": False,
            "profile": {"email": self.email, "real_name": self.real_name, "display_name": self.name},
        }


@dataclass
class FakeSlackChannel:
    id: str
    name: str
    is_private: bool = False
    is_im: bool = False
    members: list[str] = field(default_factory=list)

    def payload(self, bot_user_id: str) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "is_channel": not (self.is_im or self.is_private),
            "is_group": self.is_private and not self.is_im,
            "is_im": self.is_im,
            "is_private": self.is_private,
            "is_member": bot_user_id in self.members,
            "is_archived": False,
            "num_members": len(self.members),
        }


@dataclass
class FakeSlackMessage:
    channel: str
    ts: str
    user: str
    text: str
    thread_ts: str | None = None
    blocks: list | None = None
    reactions: dict[str, list[str]] = field(default_factory=dict)
    deleted: bool = False
    streaming: bool = False

    def payload(self) -> dict:
        body: dict = {"type": "message", "user": self.user, "text": self.text, "ts": self.ts}
        if self.thread_ts:
            body["thread_ts"] = self.thread_ts
        if self.blocks:
            body["blocks"] = self.blocks
        if self.reactions:
            body["reactions"] = [{"name": name, "users": users, "count": len(users)} for name, users in self.reactions.items()]
        return body


def _error(code: str) -> dict:
    return {"ok": False, "error": code}


def dm_channel_id(user_id: str) -> str:
    """The DM channel this fake opens for a user, stable so events can name it up front."""
    return "D" + user_id.removeprefix("U")


class FakeSlackWorkspace:
    """One workspace's state plus the Web API dispatch over it."""

    def __init__(self, *, team_id: str, team_name: str, bot_user_id: str, bot_token: str):
        self.team_id = team_id
        self.team_name = team_name
        self.bot_user_id = bot_user_id
        self.bot_token = bot_token
        self.users: dict[str, FakeSlackUser] = {}
        self.channels: dict[str, FakeSlackChannel] = {}
        self.messages: list[FakeSlackMessage] = []
        self.ephemerals: list[dict] = []
        self.response_url_posts: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        # Starts at now: the app dedupes turns by message ts, so a rerun must not reuse timestamps.
        self._clock = int(time.time() * 1_000_000)
        self._lock = threading.RLock()
        self.add_user(FakeSlackUser(id=bot_user_id, name="brain", real_name="Company Brain", is_bot=True))

    # --- setup and inspection (not Slack methods) --------------------------------------

    def add_user(self, user: FakeSlackUser) -> FakeSlackUser:
        self.users[user.id] = user
        return user

    def add_channel(self, channel: FakeSlackChannel) -> FakeSlackChannel:
        self.channels[channel.id] = channel
        return channel

    def open_dm(self, user_id: str) -> FakeSlackChannel:
        channel_id = dm_channel_id(user_id)
        if channel_id not in self.channels:
            self.add_channel(FakeSlackChannel(id=channel_id, name=channel_id.lower(), is_im=True, members=[user_id, self.bot_user_id]))
        return self.channels[channel_id]

    def next_ts(self) -> str:
        """Wall-clock based and strictly increasing, so a reply always sorts after the
        message it answers, even one stamped by a loader in another process."""
        with self._lock:
            self._clock = max(self._clock + 1_000, int(time.time() * 1_000_000))
            return f"{self._clock // 1_000_000}.{self._clock % 1_000_000:06d}"

    def say(self, channel: str, user: str, text: str, thread_ts: str | None = None, ts: str | None = None) -> FakeSlackMessage:
        """A human message, as typed into Slack. The replay records it here so threads read back whole."""
        message = FakeSlackMessage(channel=channel, ts=ts or self.next_ts(), user=user, text=text, thread_ts=thread_ts)
        with self._lock:
            self.messages.append(message)
            # Replies must sort after what they answer, even when the loader stamped it later than our clock.
            self._clock = max(self._clock, int(float(message.ts) * 1_000_000))
        return message

    def messages_in(self, channel: str, thread_ts: str | None = None) -> list[FakeSlackMessage]:
        with self._lock:
            rows = [m for m in self.messages if m.channel == channel and not m.deleted]
        if thread_ts is None:
            return rows
        return [m for m in rows if m.ts == thread_ts or m.thread_ts == thread_ts]

    def bot_replies(self, channel: str, thread_ts: str | None = None) -> list[FakeSlackMessage]:
        return [m for m in self.messages_in(channel, thread_ts) if m.user == self.bot_user_id and not m.streaming]

    def calls_to(self, method: str) -> list[dict]:
        return [body for name, body in self.calls if name == method]

    def _find(self, channel: str | None, ts: str | None) -> FakeSlackMessage | None:
        with self._lock:
            for message in self.messages:
                if message.channel == channel and message.ts == ts and not message.deleted:
                    return message
        return None

    # --- dispatch --------------------------------------------------------------------------

    def handle(self, method: str, token: str | None, body: dict) -> dict:
        with self._lock:
            self.calls.append((method, dict(body)))
        if token != self.bot_token:
            return _error("invalid_auth")
        handler = getattr(self, "_m_" + method.replace(".", "_"), None)
        if handler is None:
            return _error("unknown_method")
        return handler(body)

    def handle_response_url(self, body: dict) -> dict:
        """Slack's response_url endpoint for interaction follow-ups."""
        with self._lock:
            self.response_url_posts.append(dict(body))
        return {"ok": True}

    # --- control endpoints (not Slack): what a loader in another process needs ------------------

    def handle_control(self, action: str, body: dict) -> dict:
        """``say`` records a human message as if typed in Slack; ``messages`` reads a channel or
        thread back, with blocks, so the loader can collect replies and find approval cards."""
        if action == "say":
            message = self.say(body["channel"], body["user"], body.get("text") or "", thread_ts=body.get("thread_ts") or None, ts=body.get("ts") or None)
            return {"ok": True, "ts": message.ts}
        if action == "messages":
            rows = self.messages_in(body.get("channel") or "", body.get("thread_ts") or None)
            return {"ok": True, "messages": [{**m.payload(), "streaming": m.streaming} for m in rows]}
        return _error("unknown_control_action")

    # --- auth and users --------------------------------------------------------------------

    def _m_auth_test(self, _body: dict) -> dict:
        return {
            "ok": True,
            "url": f"https://{self.team_name.lower()}.example.com/",
            "team": self.team_name,
            "team_id": self.team_id,
            "user": self.users[self.bot_user_id].name,
            "user_id": self.bot_user_id,
            "bot_id": "B" + self.bot_user_id.removeprefix("U"),
        }

    def _m_users_info(self, body: dict) -> dict:
        user = self.users.get(body.get("user") or "")
        if user is None:
            return _error("user_not_found")
        return {"ok": True, "user": user.payload()}

    def _m_users_list(self, _body: dict) -> dict:
        return {"ok": True, "members": [u.payload() for u in self.users.values()], "response_metadata": {"next_cursor": ""}}

    def _m_users_lookupByEmail(self, body: dict) -> dict:
        wanted = (body.get("email") or "").lower()
        for user in self.users.values():
            if user.email and user.email.lower() == wanted:
                return {"ok": True, "user": user.payload()}
        return _error("users_not_found")

    def _m_users_conversations(self, body: dict) -> dict:
        types = set((body.get("types") or "public_channel").split(","))
        rows = [c for c in self.channels.values() if self.bot_user_id in c.members and self._channel_type(c) in types]
        return {"ok": True, "channels": [c.payload(self.bot_user_id) for c in rows], "response_metadata": {"next_cursor": ""}}

    def _m_usergroups_list(self, _body: dict) -> dict:
        return {"ok": True, "usergroups": []}

    # --- conversations ---------------------------------------------------------------------

    @staticmethod
    def _channel_type(channel: FakeSlackChannel) -> str:
        if channel.is_im:
            return "im"
        return "private_channel" if channel.is_private else "public_channel"

    def _m_conversations_info(self, body: dict) -> dict:
        channel = self.channels.get(body.get("channel") or "")
        if channel is None:
            return _error("channel_not_found")
        return {"ok": True, "channel": channel.payload(self.bot_user_id)}

    def _m_conversations_list(self, body: dict) -> dict:
        types = set((body.get("types") or "public_channel").split(","))
        rows = [c for c in self.channels.values() if not c.is_im and self._channel_type(c) in types]
        return {"ok": True, "channels": [c.payload(self.bot_user_id) for c in rows], "response_metadata": {"next_cursor": ""}}

    def _m_conversations_members(self, body: dict) -> dict:
        channel = self.channels.get(body.get("channel") or "")
        if channel is None:
            return _error("channel_not_found")
        return {"ok": True, "members": list(channel.members), "response_metadata": {"next_cursor": ""}}

    def _m_conversations_open(self, body: dict) -> dict:
        users = [u for u in (body.get("users") or "").split(",") if u]
        if len(users) != 1 or users[0] not in self.users:
            return _error("user_not_found")
        channel = self.open_dm(users[0])
        return {"ok": True, "channel": {"id": channel.id}}

    def _m_conversations_join(self, body: dict) -> dict:
        channel = self.channels.get(body.get("channel") or "")
        if channel is None:
            return _error("channel_not_found")
        if channel.is_private:
            return _error("method_not_supported_for_channel_type")
        if self.bot_user_id not in channel.members:
            channel.members.append(self.bot_user_id)
        return {"ok": True, "channel": channel.payload(self.bot_user_id)}

    def _m_conversations_history(self, body: dict) -> dict:
        channel = self.channels.get(body.get("channel") or "")
        if channel is None:
            return _error("channel_not_found")
        rows = [m for m in self.messages_in(channel.id) if not m.thread_ts or m.thread_ts == m.ts]
        rows.sort(key=lambda m: m.ts, reverse=True)
        limit = int(body.get("limit") or 100)
        return {"ok": True, "messages": [m.payload() for m in rows[:limit]], "has_more": False, "response_metadata": {"next_cursor": ""}}

    def _m_conversations_replies(self, body: dict) -> dict:
        channel = self.channels.get(body.get("channel") or "")
        if channel is None:
            return _error("channel_not_found")
        parent = self._find(channel.id, body.get("ts"))
        if parent is None:
            return _error("thread_not_found")
        rows = self.messages_in(channel.id, parent.ts)
        rows.sort(key=lambda m: m.ts)
        return {"ok": True, "messages": [m.payload() for m in rows], "has_more": False, "response_metadata": {"next_cursor": ""}}

    # --- messages --------------------------------------------------------------------------

    def _post(self, body: dict, *, text: str, streaming: bool = False) -> dict:
        channel = self.channels.get(body.get("channel") or "")
        if channel is None:
            return _error("channel_not_found")
        if self.bot_user_id not in channel.members:
            return _error("not_in_channel")
        message = FakeSlackMessage(
            channel=channel.id,
            ts=self.next_ts(),
            user=self.bot_user_id,
            text=text,
            thread_ts=body.get("thread_ts") or None,
            blocks=body.get("blocks") or None,
            streaming=streaming,
        )
        with self._lock:
            self.messages.append(message)
        return {"ok": True, "channel": channel.id, "ts": message.ts, "message": message.payload()}

    def _m_chat_postMessage(self, body: dict) -> dict:
        if not (body.get("text") or body.get("blocks")):
            return _error("no_text")
        return self._post(body, text=body.get("text") or "")

    def _m_chat_postEphemeral(self, body: dict) -> dict:
        channel = self.channels.get(body.get("channel") or "")
        if channel is None:
            return _error("channel_not_found")
        if (body.get("user") or "") not in self.users:
            return _error("user_not_found")
        with self._lock:
            self.ephemerals.append(dict(body))
        return {"ok": True, "message_ts": self.next_ts()}

    def _m_chat_update(self, body: dict) -> dict:
        message = self._find(body.get("channel"), body.get("ts"))
        if message is None:
            return _error("message_not_found")
        if message.user != self.bot_user_id:
            return _error("cant_update_message")
        if "text" in body:
            message.text = body.get("text") or ""
        if "blocks" in body:
            message.blocks = body.get("blocks") or None
        return {"ok": True, "channel": message.channel, "ts": message.ts, "text": message.text}

    def _m_chat_delete(self, body: dict) -> dict:
        message = self._find(body.get("channel"), body.get("ts"))
        if message is None:
            return _error("message_not_found")
        if message.user != self.bot_user_id:
            return _error("cant_delete_message")
        message.deleted = True
        return {"ok": True, "channel": message.channel, "ts": message.ts}

    def _m_chat_getPermalink(self, body: dict) -> dict:
        message = self._find(body.get("channel"), body.get("message_ts"))
        if message is None:
            return _error("message_not_found")
        permalink = f"https://{self.team_name.lower()}.example.com/archives/{message.channel}/p{message.ts.replace('.', '')}"
        return {"ok": True, "channel": message.channel, "permalink": permalink}

    # --- native streams (DMs and the assistant pane) ------------------------------------------

    def _m_chat_startStream(self, body: dict) -> dict:
        if not body.get("thread_ts"):
            return _error("invalid_arguments")
        return self._post(body, text="", streaming=True)

    def _append_chunks(self, message: FakeSlackMessage, chunks: list) -> None:
        for chunk in chunks or []:
            if chunk.get("type") == "markdown_text":
                message.text += chunk.get("text") or ""
            elif chunk.get("type") == "blocks":
                message.blocks = [*(message.blocks or []), *(chunk.get("blocks") or [])]

    def _m_chat_appendStream(self, body: dict) -> dict:
        message = self._find(body.get("channel"), body.get("ts"))
        if message is None or not message.streaming:
            return _error("message_not_found")
        self._append_chunks(message, body.get("chunks") or [])
        return {"ok": True, "channel": message.channel, "ts": message.ts}

    def _m_chat_stopStream(self, body: dict) -> dict:
        message = self._find(body.get("channel"), body.get("ts"))
        if message is None or not message.streaming:
            return _error("message_not_found")
        self._append_chunks(message, body.get("chunks") or [])
        if body.get("blocks"):
            message.blocks = [*(message.blocks or []), *body["blocks"]]
        message.streaming = False
        return {"ok": True, "channel": message.channel, "ts": message.ts}

    def _m_assistant_threads_setStatus(self, _body: dict) -> dict:
        return {"ok": True}

    # --- reactions -------------------------------------------------------------------------

    def _m_reactions_add(self, body: dict) -> dict:
        message = self._find(body.get("channel"), body.get("timestamp"))
        if message is None:
            return _error("message_not_found")
        users = message.reactions.setdefault(body.get("name") or "", [])
        if self.bot_user_id in users:
            return _error("already_reacted")
        users.append(self.bot_user_id)
        return {"ok": True}

    def _m_reactions_remove(self, body: dict) -> dict:
        message = self._find(body.get("channel"), body.get("timestamp"))
        if message is None:
            return _error("message_not_found")
        users = message.reactions.get(body.get("name") or "", [])
        if self.bot_user_id not in users:
            return _error("no_reaction")
        users.remove(self.bot_user_id)
        return {"ok": True}

    def _m_reactions_get(self, body: dict) -> dict:
        message = self._find(body.get("channel"), body.get("timestamp"))
        if message is None:
            return _error("message_not_found")
        return {"ok": True, "type": "message", "channel": message.channel, "message": message.payload()}


_JSON_FIELDS = ("blocks", "chunks", "attachments", "metadata")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802  (http.server API)
        self._serve()

    def do_POST(self) -> None:  # noqa: N802
        self._serve()

    def _serve(self) -> None:
        workspace: FakeSlackWorkspace = self.server.workspace  # type: ignore[attr-defined]
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        content_type = self.headers.get("Content-Type") or ""
        body: dict = {}
        if raw:
            if "json" in content_type:
                body = json.loads(raw.decode("utf-8"))
            else:
                body = {key: values[-1] for key, values in parse_qs(raw.decode("utf-8")).items()}
        body.update({key: values[-1] for key, values in parse_qs(parsed.query).items()})
        for key in _JSON_FIELDS:
            if isinstance(body.get(key), str):
                try:
                    body[key] = json.loads(body[key])
                except ValueError:
                    pass
        auth = self.headers.get("Authorization") or ""
        token = auth.removeprefix("Bearer ").strip() or body.pop("token", None)

        if parsed.path.startswith("/response/"):
            data = workspace.handle_response_url(body)
        elif parsed.path.startswith("/replay/"):
            data = workspace.handle_control(parsed.path.rsplit("/", 1)[-1], body)
        else:
            data = workspace.handle(parsed.path.rsplit("/", 1)[-1], token, body)
        payload = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args) -> None:  # quiet
        return


class FakeSlackServer:
    """Serves one ``FakeSlackWorkspace`` on 127.0.0.1 at a free port (or ``port``)."""

    def __init__(self, workspace: FakeSlackWorkspace, port: int = 0):
        self.workspace = workspace
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.workspace = workspace  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="fake-slack-api", daemon=True)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def base_url(self) -> str:
        """What ``SLACK_API_BASE_URL`` should be set to."""
        return f"http://127.0.0.1:{self.port}/api/"

    def response_url(self, name: str) -> str:
        """A response_url for an interaction payload; posts land in ``workspace.response_url_posts``."""
        return f"http://127.0.0.1:{self.port}/response/{name}"

    def start(self) -> "FakeSlackServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def __enter__(self) -> "FakeSlackServer":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()
