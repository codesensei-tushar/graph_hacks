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
    # The control graph: orgs, Slack workspace routes, external accounts, invite codes, audit (src/core/tenancy.py).
    control_graph: str = field(default_factory=lambda: os.getenv("BRAIN_CONTROL_GRAPH", "") or "brain_control")
    # A graph call with no org in context raises instead of falling back to the legacy org. Tests only.
    tenancy_strict: bool = field(default_factory=lambda: os.getenv("TENANCY_STRICT", "").lower() in ("1", "true", "yes"))
    # Graph writes only: a write with no org in context raises instead of landing in the legacy org's
    # graph. Reads keep the fallback. On in production (deploy/docker-compose.yml).
    tenancy_write_strict: bool = field(default_factory=lambda: os.getenv("TENANCY_WRITE_STRICT", "").lower() in ("1", "true", "yes"))
    # The org console (/console, /auth). Off by default: production is unchanged until it is turned on.
    console_enabled: bool = field(default_factory=lambda: os.getenv("CONSOLE_ENABLED", "").lower() in ("1", "true", "yes"))
    # /docs, /redoc and /openapi.json list every route, admin ones included. Off by default; local development only.
    docs_enabled: bool = field(default_factory=lambda: os.getenv("DOCS_ENABLED", "").lower() in ("1", "true", "yes"))
    # Seed the company graph on startup when it's empty. Local development only; off in production.
    seed_on_startup: bool = field(default_factory=lambda: os.getenv("SEED_ON_STARTUP", "").lower() in ("1", "true", "yes"))
    # Post a heads-up to the Slack home channel when a new public pull request reaches people who weren't on it.
    coordination_alerts: bool = field(default_factory=lambda: os.getenv("COORDINATION_ALERTS", "").lower() in ("1", "true", "yes"))
    # Which seed the startup seed and POST /ops/seed load: company_brain (ourselves) or quillfern (the demo company).
    seed_company: str = field(default_factory=lambda: os.getenv("SEED_COMPANY", "") or "company_brain")

    slack_bot_token: str = field(default_factory=lambda: os.getenv("SLACK_BOT_TOKEN", ""))
    # Slack Web API origin. Tests and the demo replay point it at a local fake (scripts/replay/slack_api.py).
    slack_api_base_url: str = field(default_factory=lambda: os.getenv("SLACK_API_BASE_URL", "") or "https://slack.com/api/")
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
    # MultiFernet keys, comma-separated; the first encrypts, all decrypt. TOKEN_ENCRYPTION_KEY is tried last.
    token_encryption_keys: tuple[str, ...] = field(
        default_factory=lambda: tuple(key.strip() for key in os.getenv("TOKEN_ENCRYPTION_KEYS", "").split(",") if key.strip()), repr=False
    )
    # Bearer token for the install/status/disconnect routes; there is no web login.
    brain_admin_token: str = field(default_factory=lambda: os.getenv("BRAIN_ADMIN_TOKEN", ""))
    # Console sessions: signed out after this long without a request, and after this long regardless.
    session_idle_seconds: int = field(default_factory=lambda: int(os.getenv("SESSION_IDLE_SECONDS", "") or 12 * 60 * 60))
    session_absolute_seconds: int = field(default_factory=lambda: int(os.getenv("SESSION_ABSOLUTE_SECONDS", "") or 7 * 24 * 60 * 60))
    # Bearer token that lifts /mcp's per-address rate limit; /mcp is public without it.
    # Separate from the admin token, which is never shared.
    mcp_token: str = field(default_factory=lambda: os.getenv("MCP_TOKEN", ""))

    # Anthropic API key and model for every model profile (src/integrations/llm/model.py).
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "") or "claude-haiku-5-5")

    # GitHub App (Settings -> Developer settings -> GitHub Apps). See docs/GITHUB.md.
    github_app_id: str = field(default_factory=lambda: os.getenv("GITHUB_APP_ID", ""))
    github_webhook_secret: str = field(default_factory=lambda: os.getenv("GITHUB_WEBHOOK_SECRET", ""))
    # The app's private key: a path to the .pem locally, or the PEM text itself in production.
    github_private_key_path: str = field(default_factory=lambda: os.getenv("GITHUB_PRIVATE_KEY_PATH", ""))
    github_private_key: str = field(default_factory=lambda: os.getenv("GITHUB_PRIVATE_KEY", ""))
    # OAuth credentials of the same app, used only to verify `link github` in Slack.
    github_client_id: str = field(default_factory=lambda: os.getenv("GITHUB_CLIENT_ID", ""))
    github_client_secret: str = field(default_factory=lambda: os.getenv("GITHUB_CLIENT_SECRET", ""))
    # Optional: a token used only to read public repos added by name (5,000 requests an hour instead of 60).
    # A fine-grained token with public-repository read access is enough. See docs/GITHUB.md.
    github_public_repo_token: str = field(default_factory=lambda: os.getenv("GITHUB_PUBLIC_REPO_TOKEN", ""), repr=False)

    # Connectors (docs/CONNECTORS.md): every CONNECTOR_<SOURCE>_<FIELD> variable, secrets included.
    # src/connectors/registry.py turns them into installs; kept out of repr so they never print.
    connector_env: dict[str, str] = field(
        default_factory=lambda: {key: value for key, value in os.environ.items() if key.startswith("CONNECTOR_")}, repr=False
    )

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def github_enabled(self) -> bool:
        return bool(self.github_app_id and self.github_webhook_secret and (self.github_private_key or self.github_private_key_path))

    @property
    def github_link_enabled(self) -> bool:
        return bool(self.github_enabled and self.github_client_id and self.github_client_secret)

    @property
    def slack_enabled(self) -> bool:
        return bool(self.slack_bot_token and self.slack_signing_secret)

    @property
    def console_signin_enabled(self) -> bool:
        """Sign in with Slack reuses the bot install's OAuth app credentials."""
        return bool(self.console_enabled and self.slack_client_id and self.slack_client_secret)

    @property
    def slack_oauth_enabled(self) -> bool:
        return bool(self.slack_client_id and self.slack_client_secret and self.slack_signing_secret)


settings = Settings()
