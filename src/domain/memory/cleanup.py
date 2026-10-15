"""Port of company-brain `brain/memory/cleanup.ts`."""

import logging

from src.domain.memory.documents import bulk_delete_documents_and_memories, get_brain_document_by_custom_id

log = logging.getLogger(__name__)


def delete_brain_document_by_custom_id(custom_id: str) -> bool:
    """Exact custom id only: brain topic tags fuzzy-canonicalize onto each other."""
    try:
        row = get_brain_document_by_custom_id(custom_id)
        if not row:
            return False
        delete_stale_brain_memory_document(row.id)
        return True
    except Exception:
        log.exception("[brain-research-stale-cleanup] delete failed custom_id=%s", custom_id)
        return False


def delete_stale_brain_memory_document(document_id: str) -> None:
    """Drop a document the brain no longer wants remembered (its memory goes with it)."""
    if document_id:
        bulk_delete_documents_and_memories([document_id])
