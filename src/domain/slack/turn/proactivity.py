"""Port of company-brain `brain/slack/proactivity.ts`.

The org's proactivity settings lived on the org row; here they are one JSON value
in Redis (``brain:proactivity``), read and written through ``get/set_brain_proactivity``.
"""

from typing import Any

from src.domain.slack.turn._state import get_json, set_json

BRAIN_PROACTIVITY_DEFAULTS = ("all_channels", "own_channel_only")
BRAIN_CHANNEL_PROACTIVITY = ("proactive", "quiet")

DEFAULT_BRAIN_PROACTIVITY = "all_channels"
HOME_CHANNEL_NAME = "company-brain"

PROACTIVITY_FILTER_REASON = "Proactive replies are disabled for this channel by the org's proactivity settings."

_SETTINGS_KEY = "brain:proactivity"


def parse_brain_proactivity(raw: Any) -> dict:
    """Tolerates junk: unknown keys/values are dropped, never raised on."""
    if not isinstance(raw, dict):
        return {}
    settings: dict = {}
    if raw.get("default") in BRAIN_PROACTIVITY_DEFAULTS:
        settings["default"] = raw["default"]
    channels = raw.get("channels")
    if isinstance(channels, dict):
        valid = {channel_id: mode for channel_id, mode in channels.items() if mode in BRAIN_CHANNEL_PROACTIVITY}
        if valid:
            settings["channels"] = valid
    # A merge spreads this parse; dropping journey here would erase the opt-in.
    journey = raw.get("journey")
    if isinstance(journey, dict) and isinstance(journey.get("enabled"), bool):
        settings["journey"] = {"enabled": journey["enabled"]}
    return settings


def resolve_channel_proactivity(
    *,
    settings: Any,
    channel_id: str | None,
    home_channel_id: str | None = None,
    channel_name: str | None = None,
) -> str:
    """Precedence: home channel (always proactive) > per-channel override > org default.

    Only gates channel surfaces; DM/mention/name-wake paths never call this.
    """
    if not channel_id:
        return "proactive"
    if home_channel_id and channel_id == home_channel_id:
        return "proactive"
    if channel_name == HOME_CHANNEL_NAME:
        return "proactive"
    parsed = parse_brain_proactivity(settings)
    override = (parsed.get("channels") or {}).get(channel_id)
    if override:
        return override
    return "proactive" if parsed.get("default", DEFAULT_BRAIN_PROACTIVITY) == "all_channels" else "quiet"


def get_brain_proactivity() -> dict:
    return parse_brain_proactivity(get_json(_SETTINGS_KEY))


def set_brain_proactivity(patch: dict) -> dict:
    """Merge a partial update, the same way their settings PATCH spreads the parse."""
    current = get_brain_proactivity()
    incoming = parse_brain_proactivity(patch)
    merged = {**current, **incoming}
    if "channels" in incoming:
        merged["channels"] = {**current.get("channels", {}), **incoming["channels"]}
    set_json(_SETTINGS_KEY, merged)
    return merged
