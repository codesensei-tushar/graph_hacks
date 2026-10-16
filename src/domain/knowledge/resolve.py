"""Resolve the names a question uses to nodes in the graph, before any tool runs.

The agent never guesses. A name resolves only when the graph has a node whose name,
alias, or title appears in the text as a whole phrase. When the words name two
different nodes side by side ("payments gateway" when the graph has a Billing Service
known as "payments" and an API Gateway known as "gateway"), nothing resolves and both
come back as candidates, so the reply can say what the graph does know. Ingestion
code that only needs a best guess for classification uses ``best_match`` instead.
"""

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from src.db.falkordb_client import connect

ORG_SHARED = "org_shared"
SCOPED = "coalesce({var}.scope, 'org_shared') IN $scopes"

DEFAULT_LABELS = ("Service", "Person", "Team", "Document", "Ticket", "Decision")

# Words a question uses around a name. Stripped before fuzzy comparison and when quoting
# what the asker named; never used to decide a match.
STOPWORDS = frozenset(
    {
        "who", "what", "why", "how", "did", "does", "the", "and", "for", "with", "this", "that", "from", "into",
        "about", "should", "look", "owns", "own", "owner", "owned", "knows", "know", "worked", "work", "break",
        "breaks", "goes", "down", "if", "when", "new", "ticket", "just", "landed", "show", "path", "a", "an",
        "of", "on", "to", "in", "is", "are", "we", "our", "me", "my", "please", "else", "has", "it", "move",
        "moved", "off", "service", "services", "runs", "run", "do", "can", "you", "tell", "which", "where",
        "connected", "connect", "related", "between", "hops", "hop", "depth", "depends", "depend", "at", "all",
    }
)

FUZZY_FLOOR = 0.6
MAX_CANDIDATES = 3


@dataclass(frozen=True)
class Entity:
    id: str
    name: str
    label: str


@dataclass(frozen=True)
class Candidate:
    entity: Entity
    # The name or alias that brought this candidate up, e.g. "payments".
    via: str


@dataclass
class Resolution:
    query: str
    entity: Entity | None = None
    candidates: list[Candidate] = field(default_factory=list)
    # "" when found; otherwise "ambiguous" (two nodes named side by side) or "no_match".
    reason: str = ""

    @property
    def found(self) -> bool:
        return self.entity is not None

    def candidate_names(self) -> list[str]:
        return [candidate.entity.name for candidate in self.candidates]


@dataclass(frozen=True)
class EntityRow:
    """What the graph knows a node as: its name, comma-separated aliases, and a title."""

    id: str
    name: str
    label: str
    aliases: str = ""
    title: str = ""


@dataclass(frozen=True)
class _Match:
    start: int
    end: int
    entity: Entity
    key: str
    # 0 for the node's name, 1 for an alias or title, 2 for a person's first name.
    kind: int


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def core(text: str) -> str:
    """The text with question words stripped: what the asker actually named."""
    return " ".join(token for token in tokens(text) if token not in STOPWORDS)


def same_word(a: str, b: str) -> bool:
    """Equal, or one is a stem of the other ("payments" ~ "payment", "auth" ~ "authentication")."""
    return a == b or (len(a) >= 4 and len(b) >= 4 and (a.startswith(b) or b.startswith(a)))


def load_rows(graph, labels: tuple[str, ...], scopes: list[str]) -> list[EntityRow]:
    rows = graph.ro_query(
        f"""
        MATCH (n)
        WHERE labels(n)[0] IN $labels AND n.name IS NOT NULL AND {SCOPED.format(var="n")}
        RETURN n.id, n.name, labels(n)[0], coalesce(n.aliases, ''), coalesce(n.title, '')
        """,
        {"labels": list(labels), "scopes": scopes},
    ).result_set
    return [EntityRow(id=row[0], name=row[1], label=row[2], aliases=row[3] or "", title=row[4] or "") for row in rows]


def resolve_name(text: str, labels: tuple[str, ...] = DEFAULT_LABELS, scopes: list[str] | None = None, graph=None) -> Resolution:
    """The one node ``text`` names, within the asker's scopes, or the nearest candidates."""
    if graph is None:
        _, graph = connect()
    return resolve_rows(text, load_rows(graph, labels, scopes or [ORG_SHARED]), labels)


def resolve_all(text: str, labels: tuple[str, ...] = DEFAULT_LABELS, scopes: list[str] | None = None, graph=None) -> list[Entity]:
    """Every distinct node ``text`` names, in order of appearance. For tools that take two names."""
    if graph is None:
        _, graph = connect()
    return entities_in_rows(text, load_rows(graph, labels, scopes or [ORG_SHARED]), labels)


def best_match(text: str, labels: tuple[str, ...], scopes: list[str], graph=None) -> Entity | None:
    """A lenient best guess by word overlap, for classifying ingested text. Not for answering questions."""
    if graph is None:
        _, graph = connect()
    rows = load_rows(graph, labels, scopes)
    strict = resolve_rows(text, rows, labels)
    if strict.found:
        return strict.entity
    words = {token for token in tokens(text) if len(token) > 2 and token not in STOPWORDS}
    best, best_score = None, 0
    for row in rows:
        haystack = " ".join((row.name, row.aliases, row.title)).lower()
        score = sum(2 for word in words if word in haystack)
        if score > best_score:
            best, best_score = row, score
    return Entity(best.id, best.name, best.label) if best is not None and best_score >= 2 else None


def describe_miss(resolution: Resolution, kind: str = "service, document, or ticket") -> str:
    named = core(resolution.query) or resolution.query.strip()
    lead = f"Nothing in the graph matches “{named}”"
    if resolution.reason == "ambiguous":
        lead += " as one thing"
    if not resolution.candidates:
        return f"{lead}. Name a {kind} that exists in the graph."
    nearest = ", ".join(
        candidate.entity.name if candidate.via.lower() == candidate.entity.name.lower() else f"{candidate.entity.name} (known as “{candidate.via}”)"
        for candidate in resolution.candidates
    )
    return f"{lead}. Nearest: {nearest}."


# --- matching, pure --------------------------------------------------------------------


def entities_in_rows(text: str, rows: list[EntityRow], labels: tuple[str, ...] = DEFAULT_LABELS) -> list[Entity]:
    return list(dict.fromkeys(match.entity for match in _kept_matches(text, rows, labels)))


def resolve_rows(text: str, rows: list[EntityRow], labels: tuple[str, ...] = DEFAULT_LABELS) -> Resolution:
    resolution = Resolution(query=text)
    kept = _kept_matches(text, rows, labels)
    if not kept:
        resolution.reason = "no_match"
        resolution.candidates = _fuzzy_candidates(text, rows)
        return resolution

    entities = list(dict.fromkeys(match.entity for match in kept))
    if len(entities) == 1:
        resolution.entity = entities[0]
        return resolution

    # Two different nodes named side by side is one unknown thing, not two answers.
    for previous, current in zip(kept, kept[1:]):
        if previous.end == current.start and previous.entity != current.entity:
            resolution.reason = "ambiguous"
            resolution.candidates = _candidates_from(kept)[:MAX_CANDIDATES]
            return resolution

    best = min(kept, key=lambda match: (-(match.end - match.start), match.kind, match.start))
    resolution.entity = best.entity
    resolution.candidates = [candidate for candidate in _candidates_from(kept) if candidate.entity != best.entity][:MAX_CANDIDATES]
    return resolution


def _keys(rows: list[EntityRow]) -> list[tuple[Entity, str, tuple[str, ...], int]]:
    first_names: dict[str, int] = {}
    for row in rows:
        if row.label == "Person":
            words = tokens(row.name)
            if words:
                first_names[words[0]] = first_names.get(words[0], 0) + 1
    keys = []
    for row in rows:
        entity = Entity(row.id, row.name, row.label)
        seen: set[tuple[str, ...]] = set()
        for text, kind in ((row.name, 0), *((alias, 1) for alias in row.aliases.split(",")), (row.title, 1)):
            words = tuple(tokens(text))
            if words and words not in seen:
                seen.add(words)
                keys.append((entity, text.strip(), words, kind))
        if row.label == "Person":
            words = tokens(row.name)
            if words and len(words[0]) >= 3 and first_names.get(words[0]) == 1 and (words[0],) not in seen:
                keys.append((entity, words[0].capitalize(), (words[0],), 2))
    return keys


def _kept_matches(text: str, rows: list[EntityRow], labels: tuple[str, ...]) -> list[_Match]:
    words = tokens(text)
    rank = {label: index for index, label in enumerate(labels)}
    found: list[_Match] = []
    for entity, key, key_words, kind in _keys(rows):
        size = len(key_words)
        for start in range(len(words) - size + 1):
            if all(same_word(words[start + offset], key_words[offset]) for offset in range(size)):
                found.append(_Match(start, start + size, entity, key, kind))
    # Longest phrase wins; among equals, the caller's label order, then a name over an alias.
    found.sort(key=lambda match: (-(match.end - match.start), rank.get(match.entity.label, len(rank)), match.kind, match.start))
    kept: list[_Match] = []
    for match in found:
        if all(match.end <= other.start or match.start >= other.end for other in kept):
            kept.append(match)
    kept.sort(key=lambda match: match.start)
    return kept


def _candidates_from(matches: list[_Match]) -> list[Candidate]:
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for match in matches:
        if match.entity.id not in seen:
            seen.add(match.entity.id)
            candidates.append(Candidate(match.entity, match.key))
    return candidates


def _fuzzy_candidates(text: str, rows: list[EntityRow]) -> list[Candidate]:
    named = core(text)
    if not named:
        return []
    named_words = named.split()
    scored: list[tuple[float, Candidate]] = []
    for entity, key, key_words, _kind in _keys(rows):
        score = SequenceMatcher(None, named, " ".join(key_words)).ratio()
        if any(same_word(word, key_word) for word in named_words for key_word in key_words):
            score = max(score, FUZZY_FLOOR)
        if score >= FUZZY_FLOOR:
            scored.append((score, Candidate(entity, key)))
    scored.sort(key=lambda item: -item[0])
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for _score, candidate in scored:
        if candidate.entity.id not in seen:
            seen.add(candidate.entity.id)
            candidates.append(candidate)
        if len(candidates) == MAX_CANDIDATES:
            break
    return candidates
