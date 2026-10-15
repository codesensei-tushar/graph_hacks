"""Port of company-brain `brain/memory/workspace-prompt.ts`.

The workspace prompt: persistent guidance for how the brain works and talks here.
One value per deployment, kept in Redis instead of the DO ``brain_workspace_prompt`` row.
"""

from src.db.falkordb_client import kv

MAX_WORKSPACE_PROMPT_LENGTH = 1_500
_KEY = "brain:workspace_prompt"


def normalize_workspace_prompt(value: str | None) -> str | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    return normalized[:MAX_WORKSPACE_PROMPT_LENGTH].strip() or None


def get_workspace_prompt() -> str | None:
    raw = kv().get(_KEY)
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else raw


def set_workspace_prompt(value: str | None) -> str | None:
    prompt = normalize_workspace_prompt(value)
    if prompt is None:
        kv().delete(_KEY)
    else:
        kv().set(_KEY, prompt)
    return prompt
