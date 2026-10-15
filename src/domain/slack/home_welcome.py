"""Port of company-brain `brain/slack/home-welcome.ts`.

The retirement/shutdown notice (`companyBrainShutdownMessage`) is hosted-product
copy and is not ported.
"""

import re

COMPANY_BRAIN_HOME_WELCOME_VERSION = 2


def _installer_first_name(value: str | None) -> str | None:
    cleaned = " ".join(re.sub(r"[<>&\r\n]", " ", value or "").split())
    first = cleaned.split(" ")[0] if cleaned else ""
    return first[:80] or None


def company_brain_home_welcome_messages(admin_name: str | None = None) -> list[str]:
    name = _installer_first_name(admin_name)
    thanks = f"_Thanks for bringing me in, {name}._" if name else "_Thanks for bringing me in._"
    return [
        "\n".join(
            [
                "👋 *I'm your Company Brain.*",
                "Ask me what's going on, where something stands, or what the team decided — in here, any channel I'm in, or by DM.",
                thanks,
            ]
        )
    ]


def company_brain_disconnect_message() -> str:
    """Customer-initiated; DMs the bot sent cannot be unsent, so the channel is the only goodbye."""
    return "\n".join(
        [
            "*Company Brain has been disconnected from this workspace.*",
            "I won't reply here, in other channels, or in DMs anymore. This channel stays, along with everything already in it.",
            "Reinstall the app if you want me back.",
        ]
    )
