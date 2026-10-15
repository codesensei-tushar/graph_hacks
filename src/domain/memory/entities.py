"""Port of company-brain `brain/memory/entities.ts`.

Resolve a name to a canonical company/organization: the brain first (entity
documents in org_shared for the current reset generation), then the web through a
model with web search, caching the result back. With no model the web step returns
nothing, which is company-brain's behavior when that provider isn't configured.
"""

import json
import logging
import re
from dataclasses import dataclass, field

from src.domain.memory.cleanup import delete_stale_brain_memory_document
from src.domain.memory.containers import SHARED_TEAM_BRAIN_CONTAINER_TAG
from src.domain.memory.documents import list_brain_documents, upsert_brain_document
from src.domain.memory.tree import get_brain_memory_reset_epoch, is_brain_memory_reset_epoch_current
from src.integrations.llm.model import ModelRequest, ModelUnavailable, generate_text

log = logging.getLogger(__name__)

ENTITY_TYPE = "entity"

RESOLVE_PROMPT = (
    'Identify the company or organization referred to as "{ref}". Use web search. Return ONLY JSON: '
    '{{"canonical":"Official Name","domain":"example.com","aliases":["short name"]}}. domain is the primary website '
    "domain, no protocol or www. Use null for domain if unsure."
)


@dataclass
class ResolvedEntity:
    canonical: str
    domain: str | None
    aliases: list[str] = field(default_factory=list)
    contacts: list[str] = field(default_factory=list)
    source: str = "brain"  # "brain" | "web"


def _normalize(value: str) -> str:
    return value.strip().lower()


def _normalize_domain(value: str) -> str | None:
    raw = re.sub(r"^www\.", "", re.sub(r"/.*$", "", re.sub(r"^https?://", "", value.strip().lower())))
    return raw if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", raw) else None


def entity_custom_id(org_id: str, key: str, reset_epoch: int) -> str:
    return f"entity_{org_id}_{re.sub(r'[^a-z0-9]+', '-', key)}_g{reset_epoch}"


def _matches(ref: str, meta: dict) -> bool:
    aliases = meta.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [a for a in aliases.split(",") if a]
    haystack = [_normalize(str(v)) for v in [meta.get("canonical"), meta.get("domain"), *aliases] if v]
    domain = meta.get("domain")
    stem = _normalize(domain.split(".")[0]) if domain else ""
    return ref in haystack or bool(stem and stem == ref)


def _find_entity(ref: str, reset_epoch: int) -> ResolvedEntity | None:
    rows = list_brain_documents(
        SHARED_TEAM_BRAIN_CONTAINER_TAG,
        metadata=[{"key": "type", "value": ENTITY_TYPE}, {"key": "brain_reset_epoch", "value": str(reset_epoch)}],
        include_content=False,
        limit=100,
    )
    for row in rows:
        meta = row.metadata or {}
        if meta.get("canonical") and _matches(ref, meta):
            aliases = meta.get("aliases") or []
            return ResolvedEntity(
                canonical=meta["canonical"],
                domain=meta.get("domain"),
                aliases=aliases if isinstance(aliases, list) else [a for a in str(aliases).split(",") if a],
                contacts=list(meta.get("contacts") or []),
                source="brain",
            )
    return None


def _resolve_via_web(ref: str) -> ResolvedEntity | None:
    try:
        text = generate_text(ModelRequest(profile="main", system="", prompt=RESOLVE_PROMPT.format(ref=ref)))
    except ModelUnavailable:
        return None
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except ValueError:
        return None
    canonical = (parsed.get("canonical") or "").strip()
    if not canonical:
        return None
    return ResolvedEntity(
        canonical=canonical,
        domain=_normalize_domain(parsed["domain"]) if parsed.get("domain") else None,
        aliases=[a.strip() for a in parsed.get("aliases") or [] if a and a.strip()],
        source="web",
    )


def _write_entity(org_id: str, entity: ResolvedEntity, reset_epoch: int) -> None:
    if not is_brain_memory_reset_epoch_current(reset_epoch):
        return
    key = entity.domain or entity.canonical
    alias_line = f" Aliases: {', '.join(entity.aliases)}." if entity.aliases else ""
    content = f"{entity.canonical}{f' ({entity.domain})' if entity.domain else ''}.{alias_line}"
    metadata = {
        "type": ENTITY_TYPE,
        "source": "company-brain",
        "brain_reset_epoch": reset_epoch,
        "title": entity.canonical,
        "canonical": entity.canonical,
        "aliases": entity.aliases,
    }
    if entity.domain:
        metadata["domain"] = entity.domain
    result = upsert_brain_document(
        content=content,
        container_tag=SHARED_TEAM_BRAIN_CONTAINER_TAG,
        custom_id=entity_custom_id(org_id, key.lower(), reset_epoch),
        metadata=metadata,
        title=entity.canonical,
    )
    if result.status == "done" and not is_brain_memory_reset_epoch_current(reset_epoch):
        delete_stale_brain_memory_document(result.id)


def resolve_entity(org_id: str, reference: str, persist: bool = True) -> ResolvedEntity | None:
    """Brain first, then web (cached back unless the turn is read-only)."""
    ref = _normalize(reference)
    if not ref:
        return None
    reset_epoch = get_brain_memory_reset_epoch()
    try:
        known = _find_entity(ref, reset_epoch)
        # A reset may have advanced the epoch mid-query; don't serve a stale entity.
        if not is_brain_memory_reset_epoch_current(reset_epoch):
            return None
        if known:
            return known
        web = _resolve_via_web(ref)
        if not web:
            return None
        if persist:
            try:
                _write_entity(org_id, web, reset_epoch)
            except Exception:
                log.exception("[entities] write failed")
        return web
    except Exception:
        log.exception("[entities] resolve failed ref=%s", ref)
        return None
