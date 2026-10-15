"""Port of company-brain `brain/memory/search-brain.ts` and `brain/search-brain-format.ts`.

supermemory ran a hybrid vector search per container. Here each container gets a
FalkorDB full-text query over ``Memory.text`` to find candidates, and the score is
the share of the query's meaningful terms the memory contains (0..1), so
company-brain's relevance threshold (0.3) keeps its meaning. Fan-out across every
readable container, dedupe by id keeping the best score, return the top 40.
"""

import logging
import re
import time

from src.db.falkordb_client import connect
from src.domain.memory.documents import LIVE, now_ms
from src.domain.memory.read_scope import resolve_brain_read_container_tags
from src.domain.memory.tags import normalize_brain_tag_key
from src.domain.memory.writeback import SlackMemoryScope

log = logging.getLogger(__name__)

BRAIN_RELEVANCE_THRESHOLD = 0.3
SEARCH_LIMIT = 40
CONTAINER_SEARCH_CONCURRENCY = 6  # kept for parity; FalkorDB queries run one container at a time

_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "into", "about", "what", "who", "why", "how", "did", "does",
    "are", "was", "were", "you", "your", "our", "has", "have", "had", "not", "but", "can", "will", "should", "would",
    "there", "their", "they", "them", "then", "than", "when", "where", "which", "been", "any", "all", "out", "get",
}


def query_terms(query: str) -> list[str]:
    tokens = re.split(r"[^a-z0-9]+", (query or "").lower())
    return list(dict.fromkeys(token for token in tokens if len(token) > 2 and token not in _STOPWORDS))


def _term_hits(term: str, words: set[str]) -> bool:
    if term in words:
        return True
    # Light stemming: "services" ~ "service", "invoic" ~ "invoices".
    return len(term) >= 4 and any(len(word) >= 4 and (word.startswith(term) or term.startswith(word)) for word in words)


def similarity(query: str, text: str) -> float:
    terms = query_terms(query)
    if not terms:
        return 0.0
    words = set(re.split(r"[^a-z0-9]+", (text or "").lower()))
    return sum(1 for term in terms if _term_hits(term, words)) / len(terms)


def _fulltext_query(terms: list[str]) -> str:
    # Prefix-match each term so plurals and stems still hit (RediSearch syntax).
    return "|".join(f"{term}*" if len(term) >= 4 else term for term in terms)


def search_container(
    q: str,
    container_tag: str,
    limit: int = SEARCH_LIMIT,
    threshold: float = BRAIN_RELEVANCE_THRESHOLD,
    focus_tags: list[str] | None = None,
) -> list[dict]:
    terms = query_terms(q)
    if not terms:
        return []
    focus = [normalize_brain_tag_key(tag) for tag in dict.fromkeys(t.strip() for t in focus_tags or []) if tag]
    _, graph = connect()
    rows = graph.query(
        f"""
        CALL db.idx.fulltext.queryNodes('Memory', $q) YIELD node AS m, score
        WHERE m.scope = $scope AND {LIVE}
          AND (size($focus) = 0 OR any(k IN coalesce(m.brain_tags, []) WHERE k IN $focus))
        OPTIONAL MATCH (author:SlackUser)-[:SAID]->(m)
        RETURN m.id, m.text, m.scope, m.channel_id, m.ts, m.title, m.brain_tags, m.updated_at,
               author.id, author.name, score
        ORDER BY score DESC
        LIMIT $candidates
        """,
        {"q": _fulltext_query(terms), "scope": container_tag, "now": now_ms(), "focus": focus, "candidates": max(limit * 3, 60)},
    ).result_set
    results = []
    for memory_id, text, scope, channel_id, ts, title, tags, updated_at, author_id, author_name, _score in rows:
        score = similarity(q, text or "")
        if score < threshold:
            continue
        results.append(
            {
                "id": memory_id,
                "memory": text or "",
                "result_type": "memory",
                "similarity": score,
                "container_tag": scope,
                "channel_id": channel_id,
                "ts": ts,
                "title": title,
                "tags": list(tags or []),
                "updated_at": updated_at,
                "author_id": author_id,
                "author": author_name or author_id,
            }
        )
    results.sort(key=lambda r: (-r["similarity"], -(r["updated_at"] or 0)))
    return results[:limit]


def search_brain(
    q: str,
    scope: SlackMemoryScope | None = None,
    trace_id: str | None = None,
    focus_tags: list[str] | None = None,
    container_tags_override: list[str] | None = None,
) -> dict:
    """Search every container the turn may read. Returns {results, timing, total}."""
    started = time.time()
    container_tags = resolve_brain_read_container_tags(scope, container_tags_override)
    searches = []
    for container_tag in container_tags:
        t = time.time()
        found = search_container(q, container_tag, focus_tags=focus_tags)
        searches.append(found)
        if trace_id:
            log.info(
                "[company-brain][%s] searchBrain container=%s results=%d wallMs=%d scores=%s",
                trace_id, container_tag, len(found), int((time.time() - t) * 1000), format_brain_score_summary(found),
            )
    by_id: dict[str, dict] = {}
    for result in (r for search in searches for r in search):
        existing = by_id.get(result["id"])
        if existing is None or result["similarity"] > existing["similarity"]:
            by_id[result["id"]] = result
    results = sorted(by_id.values(), key=lambda r: -r["similarity"])[:SEARCH_LIMIT]
    timing = int((time.time() - started) * 1000)
    if trace_id:
        log.info(
            "[company-brain][%s] searchBrain finish containers=%d deduped=%d returned=%d totalMs=%d",
            trace_id, len(container_tags), len(by_id), len(results), timing,
        )
    return {"results": results, "timing": timing, "total": len(results)}


# --- search-brain-format ------------------------------------------------------------


def text_brain_results(results: list[dict]) -> list[dict]:
    return [r for r in results if (r.get("memory") or r.get("chunk") or "").strip()]


def relevant_brain_results(results: list[dict]) -> list[dict]:
    return [r for r in text_brain_results(results) if r.get("similarity") is None or r["similarity"] >= BRAIN_RELEVANCE_THRESHOLD]


def format_brain_results(results: list[dict], filter_by_score: bool = True, limit: int | None = None) -> str:
    filtered = relevant_brain_results(results) if filter_by_score else text_brain_results(results)
    visible = filtered[:limit] if isinstance(limit, int) else filtered
    lines = []
    for r in visible:
        text = re.sub(r"\s+", " ", (r.get("memory") or r.get("chunk") or "").strip())
        if not text:
            continue
        kind = "Chunk" if r.get("chunk") or r.get("result_type") == "chunk" else "Memory"
        score = f" ({r['similarity'] * 100:.0f}%)" if isinstance(r.get("similarity"), (int, float)) else ""
        rid = f" {r['id']}" if r.get("id") else ""
        source = f" ({r['filepath']})" if r.get("filepath") else ""
        lines.append(f"{kind}{score}{rid}{source}: {text}")
    return "\n".join(f"{i + 1}. {line}" for i, line in enumerate(lines)) if lines else "(none found)"


def format_brain_score_summary(results: list[dict]) -> str:
    scores = [f"{r['similarity']:.3f}" for r in results if isinstance(r.get("similarity"), (int, float))][:12]
    return ",".join(scores) if scores else "-"


def count_brain_results(formatted: str) -> int:
    text = formatted.strip()
    if not text or text == "(none found)":
        return 0
    return len(re.findall(r"^\d+\.\s", text, flags=re.MULTILINE))


def brain_card_title(count: int) -> str:
    if count <= 0:
        return "No relevant memories found"
    return f"Found {count} relevant {'memory' if count == 1 else 'memories'}"
