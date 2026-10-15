import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _csv(value: str) -> frozenset[str]:
    return frozenset(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    falkordb_host: str = field(default_factory=lambda: os.getenv("FALKORDB_HOST", "localhost"))
    falkordb_port: int = field(default_factory=lambda: int(os.getenv("FALKORDB_PORT", "6379")))
    falkordb_graph: str = field(default_factory=lambda: os.getenv("FALKORDB_GRAPH", "company"))

    slack_bot_token: str = field(default_factory=lambda: os.getenv("SLACK_BOT_TOKEN", ""))
    slack_signing_secret: str = field(default_factory=lambda: os.getenv("SLACK_SIGNING_SECRET", ""))
    # Slack user ids. The owner is the person who installed the app; admins are set by the owner.
    slack_owner_id: str = field(default_factory=lambda: os.getenv("SLACK_OWNER_ID", ""))
    slack_admin_ids: frozenset[str] = field(default_factory=lambda: _csv(os.getenv("SLACK_ADMIN_IDS", "")))

    bot_thread_ttl_seconds: int = 7 * 24 * 60 * 60

    @property
    def slack_enabled(self) -> bool:
        return bool(self.slack_bot_token and self.slack_signing_secret)


settings = Settings()
