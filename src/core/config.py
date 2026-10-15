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
    # Empty locally (the dev docker-compose has no password); set in production.
    falkordb_password: str = field(default_factory=lambda: os.getenv("FALKORDB_PASSWORD", ""))

    slack_bot_token: str = field(default_factory=lambda: os.getenv("SLACK_BOT_TOKEN", ""))
    slack_signing_secret: str = field(default_factory=lambda: os.getenv("SLACK_SIGNING_SECRET", ""))
    # Slack user ids. The owner is the person who installed the app; admins are set by the owner.
    slack_owner_id: str = field(default_factory=lambda: os.getenv("SLACK_OWNER_ID", ""))
    slack_admin_ids: frozenset[str] = field(default_factory=lambda: _csv(os.getenv("SLACK_ADMIN_IDS", "")))

    bot_thread_ttl_seconds: int = 7 * 24 * 60 * 60

    # Multi-workspace install over OAuth (Basic Information -> App Credentials).
    slack_client_id: str = field(default_factory=lambda: os.getenv("SLACK_CLIENT_ID", ""))
    slack_client_secret: str = field(default_factory=lambda: os.getenv("SLACK_CLIENT_SECRET", ""))
    # Public origin Slack and browsers reach this server at, e.g. https://brain.example.com.
    public_url: str = field(default_factory=lambda: os.getenv("PUBLIC_URL", "").rstrip("/"))
    # Fernet key for bot tokens stored in FalkorDB. Needs the `cryptography` package.
    token_encryption_key: str = field(default_factory=lambda: os.getenv("TOKEN_ENCRYPTION_KEY", ""))
    # Bearer token for the install/status/disconnect routes; there is no web login.
    brain_admin_token: str = field(default_factory=lambda: os.getenv("BRAIN_ADMIN_TOKEN", ""))

    # Anthropic API key and model for every model profile (src/integrations/llm/model.py).
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "") or "claude-haiku-5-5")

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def slack_enabled(self) -> bool:
        return bool(self.slack_bot_token and self.slack_signing_secret)

    @property
    def slack_oauth_enabled(self) -> bool:
        return bool(self.slack_client_id and self.slack_client_secret and self.slack_signing_secret)


settings = Settings()
