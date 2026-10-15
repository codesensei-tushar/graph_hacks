"""The one seam between ported Slack code and a language model.

company-brain calls models for triage, chime-in, steering, introductions, and memory
extraction. Every call goes through Claude via the Anthropic SDK, with the key and model
from settings. Any failure (no key, API error, refusal, truncation, bad JSON) raises
``ModelUnavailable`` and each caller takes the fallback company-brain already uses
(triage -> PASS, gate -> APPEND, and so on).
"""

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

import anthropic

from src.core.config import settings

log = logging.getLogger(__name__)

# company-brain model profiles (docs/agent.md). All use settings.llm_model; the
# profile sets how hard the model thinks.
ModelProfile = Literal["triage", "gate", "fast", "main", "memory"]

_EFFORT: dict[str, str] = {
    "triage": "low",
    "gate": "low",
    "fast": "low",
    "memory": "medium",
    "main": "high",
}

# Thinking shares max_tokens with the answer, so short replies still need headroom.
_MIN_MAX_TOKENS = 16000

# Models that accept the server-side refusal fallback. Haiku does not.
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


class ModelUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelRequest:
    profile: ModelProfile
    system: str
    prompt: str
    # JSON schema the reply must match, when the caller wants structured output.
    schema: dict | None = None
    max_tokens: int = 1024


def is_model_available() -> bool:
    return settings.llm_enabled


@lru_cache(maxsize=1)
def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.llm_api_key)


def _complete(request: ModelRequest, output_format: dict | None = None) -> str:
    if not settings.llm_enabled:
        raise ModelUnavailable(f"no model configured for profile={request.profile}")

    output_config: dict = {"effort": _EFFORT[request.profile]}
    if output_format:
        output_config["format"] = output_format
    params: dict = {
        "model": settings.llm_model,
        "max_tokens": max(request.max_tokens, _MIN_MAX_TOKENS),
        "messages": [{"role": "user", "content": request.prompt}],
        "output_config": output_config,
    }
    if settings.llm_model in _FALLBACK_MODELS:
        # On a policy refusal the API retries on a fallback model inside the same call.
        params["betas"] = ["server-side-fallback-2026-07-01"]
        params["fallbacks"] = "default"
    if request.system:
        params["system"] = request.system

    try:
        response = _client().beta.messages.create(**params)
    except anthropic.APIError as error:
        log.warning("[llm] profile=%s request failed: %s", request.profile, error)
        raise ModelUnavailable(f"model call failed for profile={request.profile}") from error

    if response.stop_reason in ("refusal", "max_tokens"):
        raise ModelUnavailable(f"model stopped with {response.stop_reason} for profile={request.profile}")
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if not text:
        raise ModelUnavailable(f"empty reply for profile={request.profile}")
    return text


def generate_text(request: ModelRequest) -> str:
    return _complete(request)


def generate_object(request: ModelRequest) -> dict:
    if request.schema is None:
        raise ValueError("generate_object needs a schema")
    text = _complete(request, {"type": "json_schema", "schema": request.schema})
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ModelUnavailable(f"reply was not JSON for profile={request.profile}") from error
    if not isinstance(value, dict):
        raise ModelUnavailable(f"reply was not a JSON object for profile={request.profile}")
    return value
