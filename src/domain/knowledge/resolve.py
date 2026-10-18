"""Resolve the names a question uses to nodes in the graph, before any tool runs.

The agent never guesses. A name resolves only when the graph has a node whose name,
alias, or title appears in the text as a whole phrase. When the words name two
different nodes side by side ("payments gateway" when the graph has a Billing Service
known as "payments" and an API Gateway known as "gateway"), nothing resolves and both
come back as candidates, so the reply can say what the graph does know. Nor does a
name resolve when it is only the end of what was named: in "if the claude api goes
down", where the graph knows API Gateway as "api" and nothing as "claude", "api" is not
the thing asked about, so it comes back as the nearest candidate. The same holds for the
start of what was named: "the search cache" is not Search Service. A word that only says
which version ("the current billing service") or what kind ("the billing system") does not
count. Ingestion code that only needs a best guess for classification uses ``best_match`` instead.
"""

import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from src.db import falkordb_client
from src.db.falkordb_client import DarkGraph, connect
from src.domain.knowledge.kinds import SERVICE_KINDS, kind_or_default

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
        "handles", "handle", "responsible", "stops", "stop", "stalls", "happens", "happen", "affected",
        "policy", "policies", "rules",
        "leaves", "leave", "leaving", "quit", "quits", "quitting", "resigns", "covers", "cover", "takes", "take",
        "over", "only", "one", "nobody", "bus", "factor",
        "i", "m", "im", "am", "joined", "join", "brief", "briefing", "onboard", "onboarding", "team", "teams", "get", "up", "speed",
        "connects", "bridges", "bridge", "links", "rest", "other", "others", "company", "everyone", "anyone",
        "your", "their",
        # Depth and reporting words, so a refusal quotes what was named: "and two hops out" names
        # nothing, and "what did anyone say about billing" names billing.
        "two", "three", "four", "five", "six", "out", "say", "says", "said",
    }
)
# Words that open a noun phrase. The words between one and a match describe the thing named.
DETERMINERS = frozenset({"the", "a", "an", "our", "my", "your", "their", "this"})
# Words that say which version or how much of a thing, not which thing: "the current billing
# service" is Billing Service. They sit inside a noun phrase but never make a match partial.
GENERIC_MODIFIERS = frozenset({"old", "new", "legacy", "current", "existing", "latest", "main", "whole", "entire"})
# Nouns that say what kind of thing was named, not which one ("the billing system", "the
# billing retries decision"), or what about it is asked ("a billing bug"). After a match
# they never make it partial; "cache" in "the search cache" does. The one list of them.
GENERIC_HEADS = frozenset(
    {
        *SERVICE_KINDS, "team", "decision", "doc", "document", "runbook", "ticket", "layer", "worker", "gateway",
        "api", "repo", "repository", "app", "application", "component", "integration", "tool", "issue", "rfc",
        "bug", "problem", "error", "incident", "failure", "question", "request", "fix", "update",
    }
)
# Words that start what is said about a name rather than more of the name: "if the auth
# service fails", "is the memory layer decision still current". They end the noun phrase.
PREDICATE_WORDS = frozenset(
    {
        "fails", "fail", "failed", "failing", "dies", "die", "died", "crashes", "crash", "crashed", "changes",
        "change", "changed", "changing", "still", "itself", "slow", "outage", "impact", "gone", "went", "was",
        "were", "be", "been", "now", "today", "anymore", "again", "too", "also", "really", "actually", "ever",
        "matters", "matter", "mean", "means", "need", "needs", "use", "uses", "used", "rely", "relies", "gets",
        "got", "have", "had", "last", "first", "next", "then", "there", "here", "no", "not", "never", "longer",
        "valid", "true", "right", "wrong", "ok", "okay", "broken", "working", "live", "applies", "apply",
    }
)

FUZZY_FLOOR = 0.6
MAX_CANDIDATES = 3


@dataclass(frozen=True)
class Entity:
    id: str
    name: str
    label: str
    # For Service nodes: service, system, process, ... (kinds.py). Not part of identity.
    kind: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Candidate:
    entity: Entity
    # The name, alias, or title that brought this candidate up, e.g. "payments".
    via: str
    # True when ``via`` is a title (a ticket's or document's), shown as "titled" rather than "known as".
    titled: bool = False


TITLE_CLIP = 40


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
    kind: str | None = None


@dataclass(frozen=True)
class _Match:
    start: int
    end: int
    entity: Entity
    key: str
    # 0 for the node's name, 1 for an alias, 2 for a person's first name, 3 for a title.
    kind: int


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def core(text: str) -> str:
    """The text with question words stripped: what the asker actually named."""
    return " ".join(token for token in tokens(text) if token not in STOPWORDS)


def same_word(a: str, b: str) -> bool:
    """Equal, or a plural/singular pair ("payments" ~ "payment", "matches" ~ "match"). Not "author" ~ "auth"."""
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    if len(short) < 4 or not long.startswith(short):
        return False
    return len(long) - len(short) <= 1 or long == short + "es"


def near_word(a: str, b: str) -> bool:
    """A likely typo: five letters or more, the same first letter, and one edit apart (two for eight or more).
    Only ever used to suggest a name, never to resolve one."""
    if a == b or min(len(a), len(b)) < 5 or a[0] != b[0] or abs(len(a) - len(b)) > 2:
        return False
    allowed = 2 if min(len(a), len(b)) >= 8 else 1
    return _edits(a, b, allowed) <= allowed


def _edits(a: str, b: str, ceiling: int) -> int:
    """Levenshtein distance, stopping once every row passes ``ceiling``."""
    previous = list(range(len(b) + 1))
    for i, char in enumerate(a, start=1):
        current = [i]
        for j, other in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char != other)))
        if min(current) > ceiling:
            return ceiling + 1
        previous = current
    return previous[-1]


class Rows(list):
    """``load_rows``'s answer: the rows, with their match keys and word indexes worked out once.

    A cached ``Rows`` is shared by every question until the graph changes, so it is read-only."""

    def __init__(self, rows=()) -> None:
        super().__init__(rows)
        self._lock = threading.Lock()
        self._keys: list | None = None
        self._first: dict[str, list[int]] = {}
        self._real: dict[str, list[int]] = {}
        self._near: dict[tuple[str, int], list[int]] = {}
        self._words: dict[str, set[str]] | None = None

    def keys(self) -> list[tuple[Entity, str, tuple[str, ...], int]]:
        self._index()
        return self._keys

    def by_first_word(self) -> dict[str, list[int]]:
        self._index()
        return self._first

    def by_real_word(self) -> dict[str, list[int]]:
        self._index()
        return self._real

    def by_first_letter_and_length(self) -> dict[tuple[str, int], list[int]]:
        """Keys by each real word's first letter and length: where a ``near_word`` typo can be."""
        self._index()
        return self._near

    def words(self) -> dict[str, set[str]]:
        """Every token of a name, alias or title, under each form ``same_word`` can meet it by."""
        with self._lock:
            if self._words is None:
                words: dict[str, set[str]] = {}
                for row in self:
                    for text in (row.name, row.aliases, row.title):
                        for token in tokens(text):
                            for form in _forms(token):
                                words.setdefault(form, set()).add(token)
                self._words = words
        return self._words

    def _index(self) -> None:
        with self._lock:
            if self._keys is not None:
                return
            keys = _build_keys(self)
            first: dict[str, list[int]] = {}
            real: dict[str, list[int]] = {}
            near: dict[tuple[str, int], list[int]] = {}
            for position, (_entity, _key, key_words, _kind) in enumerate(keys):
                for form in _forms(key_words[0]):
                    first.setdefault(form, []).append(position)
                real_words = [word for word in key_words if len(word) >= 4 and word not in STOPWORDS]
                for form in {form for word in real_words for form in _forms(word)}:
                    real.setdefault(form, []).append(position)
                for bucket in {(word[0], len(word)) for word in real_words}:
                    near.setdefault(bucket, []).append(position)
            self._keys, self._first, self._real, self._near = keys, first, real, near


def _forms(word: str) -> set[str]:
    """The word and the shorter words ``same_word`` may equate it with ("payments" ~ "payment",
    "matches" ~ "match"). Two words can be the same only if their forms meet."""
    forms = {word}
    if len(word) - 1 >= 4:
        forms.add(word[:-1])
    if word.endswith("es") and len(word) - 2 >= 4:
        forms.add(word[:-2])
    return forms


def _lookup(index: dict[str, list[int]], words) -> list[int]:
    """Positions of the keys whose indexed word may be the same as one of ``words``, in key order."""
    return sorted({position for word in words for form in _forms(word) for position in index.get(form, ())})


# Rows by (graph, label, scope) and assembled Rows by (graph, labels, scopes), each with the graph
# generation it was read at. A user's own channel scopes add small entries beside the shared
# org_shared and org_internal ones. Both are LRUs with a size cap.
ROWS_CACHE_MAX = 256
_cache_lock = threading.Lock()
_part_cache: OrderedDict = OrderedDict()
_rows_cache: OrderedDict = OrderedDict()
_LABEL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _cached(cache: OrderedDict, key, generation):
    with _cache_lock:
        hit = cache.get(key)
        if hit is None or hit[0] != generation:
            return None
        cache.move_to_end(key)
        return hit[1]


def _store(cache: OrderedDict, key, generation, value) -> None:
    with _cache_lock:
        cache[key] = (generation, value)
        cache.move_to_end(key)
        while len(cache) > ROWS_CACHE_MAX:
            cache.popitem(last=False)


def clear_rows_cache() -> None:
    with _cache_lock:
        _part_cache.clear()
        _rows_cache.clear()


def load_rows(graph, labels: tuple[str, ...], scopes: list[str]) -> Rows:
    """Every visible node of these labels as rows to match names against, in node order.

    Kept per graph generation (``falkordb_client.generation``): read again only after a write
    changed the graph. A dark graph's empty rows are never kept."""
    labels = tuple(labels)
    wanted = tuple(sorted(set(scopes)))
    name = None if isinstance(graph, DarkGraph) else getattr(graph, "name", None)
    generation = falkordb_client.generation(name) if isinstance(name, str) else None
    if generation is None:
        return Rows(row for _node, row in sorted(_read_rows(graph, {label: wanted for label in labels})["all"], key=lambda item: item[0]))
    rows = _cached(_rows_cache, (name, labels, wanted), generation)
    if rows is not None:
        return rows
    parts = {(label, scope): _cached(_part_cache, (name, label, scope), generation) for label in labels for scope in wanted}
    missing: dict[str, tuple[str, ...]] = {}
    for (label, scope), part in parts.items():
        if part is None:
            missing[label] = (*missing.get(label, ()), scope)
    if missing:
        read = _read_rows(graph, missing)
        for label, label_scopes in missing.items():
            for scope in label_scopes:
                parts[(label, scope)] = read.get((label, scope), [])
                _store(_part_cache, (name, label, scope), generation, parts[(label, scope)])
    rows = Rows(row for _node, row in sorted((item for part in parts.values() for item in part), key=lambda item: item[0]))
    _store(_rows_cache, (name, labels, wanted), generation, rows)
    return rows


def _read_rows(graph, wanted: dict[str, tuple[str, ...]]) -> dict:
    """One label scan per label (UNION ALL), never an all-node scan. Each row comes back as
    ``(node order, EntityRow)``, by ``(label, scope)`` and all together under "all"."""
    # A synced item (Jira, HubSpot; it has ``external_id``) is matched by its handle only, never its
    # title: hundreds of free-text titles would match every question and make every word "known".
    parts, params = [], {}
    for index, (label, scopes) in enumerate(wanted.items()):
        if not _LABEL.fullmatch(label):
            raise ValueError(f"bad label {label!r}")
        params[f"scopes{index}"] = list(scopes)
        parts.append(
            f"""
            MATCH (n:{label})
            WHERE labels(n)[0] = '{label}' AND n.name IS NOT NULL AND coalesce(n.scope, '{ORG_SHARED}') IN $scopes{index}
            RETURN ID(n) AS node, coalesce(n.scope, '{ORG_SHARED}') AS scope, n.id AS id, n.name AS name, '{label}' AS label,
                   coalesce(n.aliases, '') AS aliases, CASE WHEN n.external_id IS NULL THEN coalesce(n.title, '') ELSE '' END AS title,
                   n.kind AS kind
            """
        )
    out: dict = {"all": []}
    if not parts:
        return out
    for node, scope, node_id, node_name, label, aliases, title, kind in graph.ro_query(" UNION ALL ".join(parts), params).result_set:
        row = EntityRow(
            id=node_id, name=node_name, label=label, aliases=aliases or "", title=title or "",
            kind=kind_or_default(kind) if label == "Service" else None,
        )
        out.setdefault((label, scope), []).append((node, row))
        out["all"].append((node, row))
    return out


def resolve_name(text: str, labels: tuple[str, ...] = DEFAULT_LABELS, scopes: list[str] | None = None, graph=None) -> Resolution:
    """The one node ``text`` names, within the asker's scopes, or the nearest candidates."""
    if graph is None:
        _, graph = connect()
    scopes = scopes or [ORG_SHARED]
    rows = load_rows(graph, labels, scopes)
    return resolve_rows(text, rows, labels, known=_graph_words(graph, scopes, rows if tuple(labels) == DEFAULT_LABELS else None))


def _graph_words(graph, scopes: list[str], rows: Rows | None = None):
    """Whether a word is in any visible node's name, alias, or title. Loaded on first use only,
    and not at all when the caller already holds every label's rows."""
    held = [rows]

    def known(word: str) -> bool:
        if held[0] is None:
            held[0] = load_rows(graph, DEFAULT_LABELS, scopes)
        words = held[0].words()
        return any(same_word(word, token) for form in _forms(word) for token in words.get(form, ()))

    return known


def resolve_all(text: str, labels: tuple[str, ...] = DEFAULT_LABELS, scopes: list[str] | None = None, graph=None) -> list[Entity]:
    """Every distinct node ``text`` names, in order of appearance. For tools that take two names."""
    if graph is None:
        _, graph = connect()
    return entities_in_rows(text, load_rows(graph, labels, scopes or [ORG_SHARED]), labels)


def best_match(text: str, labels: tuple[str, ...], scopes: list[str], graph=None) -> Entity | None:
    """A lenient best guess by word overlap, for classifying ingested text. Not for answering questions."""
    if graph is None:
        _, graph = connect()
    return best_match_rows(text, load_rows(graph, labels, scopes), labels)


def best_match_rows(text: str, rows: list[EntityRow], labels: tuple[str, ...]) -> Entity | None:
    """``best_match`` over rows already loaded, for callers that classify many texts against one load."""
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
    return Entity(best.id, best.name, best.label, best.kind) if best is not None and best_score >= 2 else None


def describe_miss(resolution: Resolution, kind: str = "service, document, or ticket") -> str:
    named = core(resolution.query)
    if not named:
        return f"I can't tell what that refers to. Name a {kind}."
    lead = f"Nothing in the graph matches “{named}”"
    if resolution.reason == "ambiguous":
        lead += " as one thing"
    if not resolution.candidates:
        return f"{lead}. Name a {kind} that exists in the graph."
    return f"{lead}. Nearest: {', '.join(_describe(candidate) for candidate in resolution.candidates)}."


def _describe(candidate: Candidate) -> str:
    if candidate.via.lower() == candidate.entity.name.lower():
        return candidate.entity.name
    if candidate.titled:
        title = candidate.via if len(candidate.via) <= TITLE_CLIP else candidate.via[: TITLE_CLIP - 1].rstrip() + "…"
        return f"{candidate.entity.name} (titled “{title}”)"
    return f"{candidate.entity.name} (known as “{candidate.via}”)"


def join_names(names: list[str]) -> str:
    """A, B and C."""
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


# --- matching, pure --------------------------------------------------------------------


def entities_in_rows(text: str, rows: list[EntityRow], labels: tuple[str, ...] = DEFAULT_LABELS) -> list[Entity]:
    return list(dict.fromkeys(match.entity for match in _kept_matches(text, rows, labels)))


def resolve_rows(text: str, rows: list[EntityRow], labels: tuple[str, ...] = DEFAULT_LABELS, known=None) -> Resolution:
    """``known(word)``, when given, says whether a word names anything in the graph; it lets a
    match that is only the end of a noun phrase ("the claude api") be refused."""
    resolution = Resolution(query=text)
    kept = _kept_matches(text, rows, labels)
    if not kept:
        resolution.reason = "no_match"
        resolution.candidates = _fuzzy_candidates(text, rows)
        return resolution

    entities = list(dict.fromkeys(match.entity for match in kept))
    if len(entities) > 1:
        # Two different nodes named side by side is one unknown thing, not two answers.
        for previous, current in zip(kept, kept[1:]):
            if previous.end == current.start and previous.entity != current.entity:
                resolution.reason = "ambiguous"
                resolution.candidates = _candidates_from(kept)[:MAX_CANDIDATES]
                return resolution

    best = min(kept, key=lambda match: (-(match.end - match.start), match.kind, match.start))
    words = tokens(text)
    start = _phrase_start(words, best)
    modifiers = words[start : best.start] if start is not None else []
    trailing = _trailing(words, best) if start is not None else []
    named = [word for word in modifiers if word not in GENERIC_MODIFIERS]
    described = [word for word in trailing if word not in GENERIC_MODIFIERS and not any(same_word(word, head) for head in GENERIC_HEADS)]
    if known is not None and any(group and not any(known(word) for word in group) for group in (named, described)):
        # "the claude api", "the search cache": the graph knows "api" and "search", and nothing
        # called "claude" or "cache". Name what was asked, offer the match.
        resolution.query = " ".join(words[best.start - len(modifiers) : best.end + len(trailing)])
        resolution.reason = "partial"
        resolution.candidates = [Candidate(best.entity, best.key, titled=best.kind == 3)]
        return resolution
    resolution.entity = best.entity
    resolution.candidates = [candidate for candidate in _candidates_from(kept) if candidate.entity != best.entity][:MAX_CANDIDATES]
    return resolution


def _phrase_start(words: list[str], match: _Match) -> int | None:
    """Where the noun phrase holding the match starts, just after its determiner, or None.

    The words from there to the match describe the thing ("claude" in "the claude api").
    Only a determiner opens the phrase: in "who maintains billing" or "why did we cut vector
    recall" the word before the match is a verb, not part of the name. A generic word such as
    "new" is walked past even though it is a stopword, so "the new claude api" still reaches
    "claude".
    """
    start = match.start
    while start > 0 and (words[start - 1] not in STOPWORDS or words[start - 1] in GENERIC_MODIFIERS):
        start -= 1
    if start == 0 or words[start - 1] not in DETERMINERS:
        return None
    return start


def _trailing(words: list[str], match: _Match) -> list[str]:
    """The words after the match up to a stopword, a predicate or a number ("cache" in "the search cache goes down")."""
    end = match.end
    while end < len(words) and words[end] not in STOPWORDS and words[end] not in PREDICATE_WORDS and not words[end].isdigit():
        end += 1
    return words[match.end : end]


def _keys(rows: list[EntityRow]) -> list[tuple[Entity, str, tuple[str, ...], int]]:
    return rows.keys() if isinstance(rows, Rows) else _build_keys(rows)


def _build_keys(rows: list[EntityRow]) -> list[tuple[Entity, str, tuple[str, ...], int]]:
    first_names: dict[str, int] = {}
    for row in rows:
        if row.label == "Person":
            words = tokens(row.name)
            if words:
                first_names[words[0]] = first_names.get(words[0], 0) + 1
    keys = []
    for row in rows:
        entity = Entity(row.id, row.name, row.label, row.kind)
        seen: set[tuple[str, ...]] = set()
        for text, kind in ((row.name, 0), *((alias, 1) for alias in row.aliases.split(",")), (row.title, 3)):
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
    held = rows if isinstance(rows, Rows) else Rows(rows)
    keys = held.keys()
    # Only a key whose first word is the same as one of the question's can match.
    for position in _lookup(held.by_first_word(), set(words)):
        entity, key, key_words, kind = keys[position]
        size = len(key_words)
        for start in range(len(words) - size + 1):
            if all(same_word(words[start + offset], key_words[offset]) for offset in range(size)):
                found.append(_Match(start, start + size, entity, key, kind))
    # Longest phrase wins; among equals, the caller's label order, then a name over an alias.
    found.sort(key=lambda match: (-(match.end - match.start), rank.get(match.entity.label, len(rank)), min(match.kind, 1), match.start))
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
            candidates.append(Candidate(match.entity, match.key, titled=match.kind == 3))
    return candidates


def _fuzzy_candidates(text: str, rows: list[EntityRow]) -> list[Candidate]:
    """Near misses: a key that shares a real word (four letters or more, not a stopword) with what was named."""
    named = core(text)
    if not named:
        return []
    named_words = [word for word in named.split() if len(word) >= 4]
    if not named_words:
        return []
    scored: list[tuple[float, Candidate]] = []
    held = rows if isinstance(rows, Rows) else Rows(rows)
    keys = held.keys()
    # Only keys sharing a word with what was named (same_word), or holding one of the same first
    # letter and nearly the same length (near_word), can qualify.
    near = held.by_first_letter_and_length()
    possible = set(_lookup(held.by_real_word(), named_words))
    possible.update(position for word in named_words for size in range(len(word) - 2, len(word) + 3) for position in near.get((word[0], size), ()))
    for position in sorted(possible):
        entity, key, key_words, kind = keys[position]
        real_words = [word for word in key_words if len(word) >= 4 and word not in STOPWORDS]
        # A shared word, or a misspelt one: "biling" is one edit from "billing", "invoce" from "invoice".
        if not any(same_word(word, key_word) or near_word(word, key_word) for word in named_words for key_word in real_words):
            continue
        score = max(SequenceMatcher(None, named, " ".join(key_words)).ratio(), FUZZY_FLOOR)
        scored.append((score, Candidate(entity, key, titled=kind == 3)))
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
