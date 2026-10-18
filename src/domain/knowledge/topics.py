"""Who knows about a topic, not a named thing: ``topic_experts``.

The resolver answers for things the graph names. "who knows about refunds" names nothing,
so ``who_knows`` used to refuse it. Full-text indexes over what people wrote (documents,
tickets, pull requests, decisions) and said (memories) find where the topic comes up; the
edges from each hit to the people who wrote, worked on, reviewed, proposed or said it rank
who to ask, and each hit's ``DESCRIBES``, ``ABOUT``, ``TOUCHES`` or ``MENTIONS`` edge names
the thing it is about. Every answer cites those edges.

Names and aliases of owned things are not indexed: they stay with the resolver, so "who
knows the payments gateway" is still a refusal. The words searched are the question's,
passed as ``$q`` and matched all together (a full-text AND), never spliced into a query.
"""

import logging
import re
from dataclasses import dataclass, field

from src.db.falkordb_client import connect
from src.domain.knowledge import resolve
from src.domain.knowledge.dto import MemoryLine, PathStep, ToolResult, day_from
from src.domain.knowledge.resolve import SCOPED, join_names

log = logging.getLogger(__name__)

TOOL = "topic_experts"

# One full-text index per label, over the fields people write in. Memory.text has its own,
# created by memory_service.ensure_indexes.
INDEXED: dict[str, tuple[str, ...]] = {
    "Document": ("name", "summary"),
    "Ticket": ("title", "summary"),
    "PullRequest": ("title", "summary"),
    "Decision": ("name", "summary"),
}
# Where each label's hit points: the owned thing it is about.
_ABOUT = {"Document": "DESCRIBES", "Ticket": "ABOUT", "PullRequest": "TOUCHES", "Decision": "ABOUT"}

# Hits per label, hits named in the answer, and people named.
MAX_HITS = 5
MAX_LISTED = 4
MAX_PEOPLE = 3

# How much each edge to a hit says about knowing the topic. Writing or proposing it is the
# strongest; saying it in chat once is the weakest.
WEIGHTS = {"AUTHORED": 3, "PROPOSED": 3, "WORKED_ON": 2, "REVIEWED": 2, "APPROVED": 2, "ASSIGNED": 2, "OPENED": 1, "SAID": 1}
_VERBS = {
    "AUTHORED": "wrote",
    "PROPOSED": "proposed",
    "WORKED_ON": "worked on",
    "REVIEWED": "reviewed",
    "APPROVED": "approved",
    "ASSIGNED": "is assigned",
    "OPENED": "opened",
    "SAID": "said it in chat",
}

# Asking about a topic, not a thing: "who knows about refunds", "who can help with webhooks",
# "who is the expert on session tokens". ``who_knows`` checks this before it refuses.
TOPIC_QUESTION = re.compile(
    r"\babout\b|\bhelp(?:s|ing)?(?: me| us)? with\b|\bexperts? (?:on|in|at)\b|\bexpertise (?:on|in)\b|\bfamiliar with\b",
    re.IGNORECASE,
)
# Resolver question words that can be the topic itself ("join tables", "moving off postgres",
# "the refund policy"): kept in the search.
_KEPT = frozenset(
    {
        "join", "joined", "move", "moved", "off", "new", "policy", "policies", "rules", "ticket", "service", "services",
        "break", "breaks", "down", "path", "depth", "bus", "factor", "company", "onboarding", "brief", "speed",
    }
)
# Words around a topic, on top of the resolver's question words.
_TOPIC_WORDS = frozenset(
    {
        "knowledge", "knowledgeable", "help", "helps", "helping", "us", "expert", "experts", "expertise", "ask",
        "talk", "could", "someone", "somebody", "person", "people", "best", "most", "topic", "go", "familiar",
        "understands", "understand", "there", "be", "any", "anything", "something", "stuff", "things", "thing",
    }
)


@dataclass
class Hit:
    """One node the topic comes up in, with the things it is about and the people on it."""

    id: str
    label: str
    name: str
    score: float
    about: list[tuple[str, str, str]] = field(default_factory=list)  # (service id, name, rel)
    people: list[tuple[str, str, str, str]] = field(default_factory=list)  # (id, name, label, rel)
    replaced_by: str | None = None
    memory: MemoryLine | None = None

    @property
    def mention(self) -> str:
        if self.label == "Document":
            return f"the {self.name}"
        if self.label == "Decision":
            return f"*{self.name}*" + (f" (since replaced by *{self.replaced_by}*)" if self.replaced_by else "")
        return self.name


def topic_terms(question: str) -> list[str]:
    """The words that name the topic: the question minus the words every question uses."""
    words = [word for word in resolve.tokens(question) if (word in _KEPT or word not in resolve.STOPWORDS) and word not in _TOPIC_WORDS]
    return list(dict.fromkeys(word for word in words if len(word) >= 2))


def full_text_query(terms: list[str]) -> str:
    """All the words together (a full-text AND). Stemmed by the index, so "webhooks" finds "webhook"."""
    return " ".join(terms)


def topic_if_asked(question: str, scopes: list[str], miss: ToolResult) -> ToolResult:
    """What ``who_knows`` returns instead of refusing, when the question asks about a topic and the topic comes up."""
    if not TOPIC_QUESTION.search(question):
        return miss
    try:
        found = topic_experts(question, scopes)
    except Exception:
        log.exception("[topics] topic search failed; keeping the refusal")
        return miss
    return found if found.found else miss


def topic_experts(question: str, scopes: list[str]) -> ToolResult:
    """Where the topic comes up in what the asker can read, and who to ask about it."""
    terms = topic_terms(question)
    if not terms:
        return ToolResult(tool=TOOL, answer="Name a topic to look for, such as refunds or webhooks.", found=False)
    topic = " ".join(terms)
    _, graph = connect()
    query = full_text_query(terms)
    hits: list[Hit] = []
    for label in INDEXED:
        hits.extend(_written(graph, label, query, scopes))
    hits.extend(_said(graph, query, scopes))
    if not hits:
        return ToolResult(
            tool=TOOL,
            answer=f"Nothing I can read mentions “{topic}”: no document, ticket, pull request, decision or chat message.",
            found=False,
        )
    hits.sort(key=lambda hit: (-hit.score, hit.label == "Memory", hit.name))
    return _answer(graph, topic, hits, scopes)


# --- finding hits ------------------------------------------------------------------------


def _written(graph, label: str, query: str, scopes: list[str]) -> list[Hit]:
    try:
        rows = graph.ro_query(
            f"""
            CALL db.idx.fulltext.queryNodes($label, $q) YIELD node, score
            WHERE {SCOPED.format(var="node")}
            RETURN node.id, coalesce(node.name, node.title, node.id), score
            ORDER BY score DESC, node.id
            LIMIT $limit
            """,
            {"label": label, "q": query, "scopes": scopes, "limit": MAX_HITS},
        ).result_set
    except Exception as error:
        # No index yet (a graph seeded before this feature, before the next start): no hits, no crash.
        log.warning("[topics] full-text search on %s failed: %s", label, error)
        return []
    hits = []
    for node_id, name, score in rows:
        hit = Hit(node_id, label, name, float(score))
        hit.about = _about(graph, label, node_id, scopes)
        hit.people = _people(graph, label, node_id)
        if label == "Decision":
            hit.replaced_by = _replaced_by(graph, node_id, scopes)
        hits.append(hit)
    return hits


def _said(graph, query: str, scopes: list[str]) -> list[Hit]:
    try:
        rows = graph.ro_query(
            """
            CALL db.idx.fulltext.queryNodes('Memory', $q) YIELD node AS m, score
            WHERE m.scope IN $scopes AND NOT coalesce(m.is_forgotten, false)
            OPTIONAL MATCH (speaker:SlackUser)-[:SAID]->(m)
            OPTIONAL MATCH (speaker)-[:IS]->(person:Person)
            RETURN m.id, m.name, m.text, m.scope, m.channel_id, m.created_at, m.ts, score,
                   coalesce(person.id, speaker.id), coalesce(person.name, speaker.name, speaker.id),
                   CASE WHEN person IS NULL THEN 'SlackUser' ELSE 'Person' END
            ORDER BY score DESC, m.created_at DESC
            LIMIT $limit
            """,
            {"q": query, "scopes": scopes, "limit": MAX_HITS},
        ).result_set
    except Exception as error:
        log.warning("[topics] full-text search on memories failed: %s", error)
        return []
    hits = []
    for memory_id, name, text, scope, channel_id, created_at, ts, score, person_id, speaker, speaker_label in rows:
        hit = Hit(memory_id, "Memory", name or (text or "")[:60], float(score))
        hit.memory = MemoryLine(text=text or "", learned_at=day_from(created_at, ts), author=speaker or "someone", scope=scope, channel_id=channel_id)
        if speaker:
            hit.people = [(person_id, speaker, speaker_label, "SAID")]
        hit.about = [
            (service_id, service, "MENTIONS")
            for service_id, service in graph.ro_query(
                f"""
                MATCH (:Memory {{id: $id}})-[:MENTIONS]->(service:Service)
                WHERE {SCOPED.format(var="service")}
                RETURN service.id, service.name
                ORDER BY service.name
                """,
                {"id": memory_id, "scopes": scopes},
            ).result_set
        ]
        hits.append(hit)
    return hits


def _about(graph, label: str, node_id: str, scopes: list[str]) -> list[tuple[str, str, str]]:
    rel = _ABOUT[label]
    rows = graph.ro_query(
        f"""
        MATCH (hit {{id: $id}})-[edge]->(service:Service)
        WHERE $label IN labels(hit) AND type(edge) = $rel AND {SCOPED.format(var="service")}
        RETURN DISTINCT service.id, service.name
        ORDER BY service.name
        """,
        {"id": node_id, "label": label, "rel": rel, "scopes": scopes},
    ).result_set
    return [(service_id, service, rel) for service_id, service in rows]


def _people(graph, label: str, node_id: str) -> list[tuple[str, str, str, str]]:
    """People on a hit: who wrote, worked on, proposed or approved it, and GitHub people through IS."""
    rows = graph.ro_query(
        """
        MATCH (person:Person)-[rel:AUTHORED|WORKED_ON|PROPOSED|APPROVED]->(hit {id: $id})
        WHERE $label IN labels(hit)
        RETURN person.id AS id, person.name AS name, 'Person' AS label, type(rel) AS rel
        UNION
        MATCH (gh:GitHubUser)-[rel:AUTHORED|REVIEWED|OPENED|ASSIGNED]->(hit {id: $id})
        WHERE $label IN labels(hit)
        OPTIONAL MATCH (gh)-[:IS]->(person:Person)
        RETURN coalesce(person.id, gh.id) AS id, coalesce(person.name, '@' + gh.login) AS name,
               CASE WHEN person IS NULL THEN 'GitHubUser' ELSE 'Person' END AS label, type(rel) AS rel
        """,
        {"id": node_id, "label": label},
    ).result_set
    return sorted((who_id, who, who_label, rel) for who_id, who, who_label, rel in rows)


def _replaced_by(graph, decision_id: str, scopes: list[str]) -> str | None:
    # Imported here: decisions registers a catalogue tool, and the catalogue imports this module.
    from src.domain.knowledge import decisions

    current = decisions.current_decision(graph, decision_id, scopes)
    return current.name if current is not None and current.id != decision_id else None


# --- the answer ---------------------------------------------------------------------------


@dataclass
class _Asked:
    name: str
    score: int = 0
    reasons: dict[str, list[str]] = field(default_factory=dict)


def _answer(graph, topic: str, hits: list[Hit], scopes: list[str]) -> ToolResult:
    result = ToolResult(tool=TOOL, answer="")
    written = [hit for hit in hits if hit.label != "Memory"]
    said = [hit for hit in hits if hit.label == "Memory"]
    result.memories = [hit.memory for hit in said if hit.memory is not None]

    asked: dict[str, _Asked] = {}
    for hit in hits:
        for who_id, who, who_label, rel in hit.people:
            person = asked.setdefault(who, _Asked(who))
            person.score += WEIGHTS.get(rel, 1)
            what = "" if rel == "SAID" else hit.mention if hit.label != "Decision" else f"*{hit.name}*"
            objects = person.reasons.setdefault(_VERBS.get(rel, rel.lower()), [])
            if what not in objects:
                objects.append(what)
            result.add(PathStep(who, rel, hit.name, from_id=who_id, to_id=hit.id, from_label=who_label, to_label=hit.label))
        for service_id, service, rel in hit.about:
            result.add(PathStep(hit.name, rel, service, from_id=hit.id, to_id=service_id, from_label=hit.label, to_label="Service"))

    services = list(dict.fromkeys(service for hit in hits for _, service, _ in hit.about))
    places = [hit.mention for hit in written]
    if said:
        places.append(f"{len(said)} chat message{'s' if len(said) != 1 else ''}")
    listed, rest = places[:MAX_LISTED], places[MAX_LISTED:]
    if rest:
        listed.append(f"{len(rest)} more")
    parts = [f"“{topic}” comes up in {join_names(listed)}" + (f", about {join_names(services)}." if services else ".")]

    ranked = sorted(asked.values(), key=lambda person: (-person.score, person.name))[:MAX_PEOPLE]
    if ranked:
        first, *others = [f"{person.name} ({_reasons(person)})" for person in ranked]
        parts.append(f"Ask first: {first}" + (f", then {join_names(others)}" if others else "") + ".")
    else:
        owners = _owners(graph, [service_id for hit in hits for service_id, _, _ in hit.about], scopes, result)
        if owners:
            parts.append(f"Nobody is linked to those directly; ask the owner{'s' if len(owners) != 1 else ''}: {join_names(owners)}.")
        else:
            parts.append("Nobody in the graph is linked to those yet.")
    result.answer = " ".join(parts)
    return result


def _reasons(person: _Asked) -> str:
    """One clause per verb, the weightiest first: "wrote the Billing runbook, said it in chat"."""
    weight = {verb: WEIGHTS.get(rel, 1) for rel, verb in _VERBS.items()}
    clauses = []
    for verb, objects in sorted(person.reasons.items(), key=lambda item: -weight.get(item[0], 1)):
        named = [what for what in objects if what]
        clauses.append(f"{verb} {join_names(named)}" if named else verb)
    return ", ".join(clauses)


def _owners(graph, service_ids: list[str], scopes: list[str], result: ToolResult) -> list[str]:
    if not service_ids:
        return []
    rows = graph.ro_query(
        f"""
        MATCH (person:Person)-[:OWNS]->(service:Service)
        WHERE service.id IN $ids AND {SCOPED.format(var="service")}
        RETURN person.id, person.name, service.id, service.name
        ORDER BY person.name, service.name
        """,
        {"ids": sorted(set(service_ids)), "scopes": scopes},
    ).result_set
    owners = []
    for person_id, person, service_id, service in rows:
        result.add(PathStep(person, "OWNS", service, from_id=person_id, to_id=service_id, from_label="Person", to_label="Service"))
        owner = f"{person} ({service})"
        if owner not in owners:
            owners.append(owner)
    return owners


# --- indexes -----------------------------------------------------------------------------


def ensure_topic_indexes(graph=None) -> list[str]:
    """Create the full-text indexes ``topic_experts`` reads. Run on every start and after a seed.

    Idempotent: an index that already covers its fields is left alone, so a second run
    creates nothing. Failure-safe: a failure is logged and the rest carry on; the tool then
    finds nothing for that label and ``who_knows`` keeps its refusal. Returns what it created.
    """
    if graph is None:
        _, graph = connect()
    try:
        present = indexed_fields(graph)
    except Exception:
        log.exception("[topics] could not list indexes; topic search may miss until the next start")
        return []
    created = []
    for label, fields in INDEXED.items():
        missing = [name for name in fields if name not in present.get(label, set())]
        if not missing:
            continue
        for name in missing:
            # One field per call: a later call adds the field to the label's index. Label and
            # field are the constants above, passed as parameters all the same.
            try:
                graph.query("CALL db.idx.fulltext.createNodeIndex($label, $field)", {"label": label, "field": name})
                created.append(f"{label}.{name}")
            except Exception as error:
                if "already indexed" not in str(error).lower():
                    log.warning("[topics] full-text index on %s.%s not created: %s", label, name, error)
    if created:
        log.info("[topics] full-text indexes created: %s", "; ".join(created))
    return created


def indexed_fields(graph) -> dict[str, set[str]]:
    """Fields under a full-text index, by label."""
    rows = graph.ro_query("CALL db.indexes() YIELD label, types RETURN label, types").result_set
    present: dict[str, set[str]] = {}
    for label, types in rows:
        for name, kinds in (types or {}).items():
            if "FULLTEXT" in (kinds or []):
                present.setdefault(label, set()).add(name)
    return present
