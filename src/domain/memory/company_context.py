"""Port of company-brain `brain/memory/company-context.ts`.

The company description, upserted into org_shared under a deterministic custom id
(no duplicates) and fetched by metadata, not search, so it always primes the prompt.
"""

import logging

from src.domain.memory.containers import SHARED_TEAM_BRAIN_CONTAINER_TAG, build_org_entity_context
from src.domain.memory.documents import find_brain_document, upsert_brain_document

log = logging.getLogger(__name__)

COMPANY_CONTEXT_TYPE = "company_context"


def company_context_custom_id(org_id: str) -> str:
    return f"company_context_{org_id}"


def write_company_context(org_id: str, org_name: str, domain: str | None = None, about: str | None = None) -> dict:
    about = (about or "").strip()
    if not about:
        return {"written": False}
    content = build_org_entity_context(org_name, domain, about)
    try:
        upsert_brain_document(
            content=content,
            container_tag=SHARED_TEAM_BRAIN_CONTAINER_TAG,
            custom_id=company_context_custom_id(org_id),
            metadata={"type": COMPANY_CONTEXT_TYPE, "source": "company-brain", "title": f"About {org_name}"},
            title=f"About {org_name}",
        )
        return {"written": True}
    except Exception:
        log.exception("[company-context] write failed")
        return {"written": False}


def get_company_context(trace_id: str | None = None) -> str | None:
    try:
        row = find_brain_document(SHARED_TEAM_BRAIN_CONTAINER_TAG, [{"key": "type", "value": COMPANY_CONTEXT_TYPE}])
        content = (row.content or "").strip() if row else ""
        if trace_id:
            log.info("[company-brain][%s] company_context lookup found=%s chars=%d", trace_id, "yes" if content else "no", len(content))
        return content or None
    except Exception:
        log.exception("[company-context] lookup failed")
        return None
