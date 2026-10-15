"""The permissions graph.

Every memory lives in exactly one scope, the narrowest room it was said in:

- ``org_shared``           public channels. The whole org can read it.
- ``slack_channel_<id>``   one private channel (or group DM). Only its members.
- ``user_<id>``            one person's DM with the bot. Only that person.

Writing is narrow. Reading widens the more private the room is:

| Asking from        | Can read                                                    |
|--------------------|-------------------------------------------------------------|
| public channel     | org_shared                                                  |
| private channel    | that channel + org_shared                                   |
| DM with the bot    | own user scope + org_shared + every private channel they're in |

When the room's privacy can't be confirmed, fail closed: write nothing, read only org_shared.
"""

from dataclasses import dataclass
from enum import Enum

ORG_SHARED = "org_shared"


class Room(str, Enum):
    PUBLIC = "public"
    PRIVATE = "private"
    DM = "dm"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Seat:
    """Who is asking, and from which room."""

    room: Room
    user_id: str
    channel_id: str


def channel_scope(channel_id: str) -> str:
    return f"slack_channel_{channel_id}"


def user_scope(user_id: str) -> str:
    return f"user_{user_id}"


def write_scope(seat: Seat) -> str | None:
    if seat.room is Room.PUBLIC:
        return ORG_SHARED
    if seat.room is Room.PRIVATE:
        return channel_scope(seat.channel_id)
    if seat.room is Room.DM and seat.user_id:
        return user_scope(seat.user_id)
    return None


def read_scopes(seat: Seat, private_channels: list[str] | tuple[str, ...] = ()) -> list[str]:
    """``private_channels`` are the private channel ids the asker belongs to. Only used from a DM."""
    scopes = [ORG_SHARED]
    if seat.room is Room.PRIVATE:
        scopes.append(channel_scope(seat.channel_id))
    elif seat.room is Room.DM and seat.user_id:
        scopes.append(user_scope(seat.user_id))
        scopes.extend(channel_scope(channel_id) for channel_id in private_channels)
    return _unique(scopes)


def scope_label(scope: str, channel_names: dict[str, str] | None = None) -> str:
    if scope == ORG_SHARED:
        return "public channels"
    if scope.startswith("slack_channel_"):
        channel_id = scope.removeprefix("slack_channel_")
        name = (channel_names or {}).get(channel_id)
        return f"#{name}" if name else f"<#{channel_id}>"
    if scope.startswith("user_"):
        return "your DMs"
    return scope


def _unique(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen
