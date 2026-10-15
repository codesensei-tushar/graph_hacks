"""Port of company-brain `brain/memory/interaction-style.ts`.

How the brain should talk here, read from bucketed memories in the agent-self
container. The asker's own person-tagged style comes first, then team-wide style.
"""

from dataclasses import dataclass, field

from src.domain.memory.containers import AGENT_SELF_CONTAINER_TAG
from src.domain.memory.documents import list_brain_memories
from src.domain.memory.tags import person_brain_tag_key


@dataclass
class InteractionStyleProfile:
    voice: list[str] = field(default_factory=list)
    social: list[str] = field(default_factory=list)
    culture: list[str] = field(default_factory=list)
    do_not: list[str] = field(default_factory=list)
    self_concept: list[str] = field(default_factory=list)
    operating: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any((self.voice, self.social, self.culture, self.do_not, self.self_concept, self.operating))


BUCKET_TO_FACET = {
    "voice": "voice",
    "social": "social",
    "culture": "culture",
    "do_not": "do_not",
    "self_concept": "self_concept",
    "operating": "operating",
}

SECTIONS = [
    ("voice", "Voice"),
    ("social", "Social (occasional — keep it sparing)"),
    ("culture", "Team references"),
    ("operating", "How this team works"),
    ("do_not", "Avoid"),
    ("self_concept", "Self"),
]


def load_interaction_style_profile(asker_slack_user_id: str | None = None) -> InteractionStyleProfile | None:
    rows = list_brain_memories([AGENT_SELF_CONTAINER_TAG], limit=80, with_buckets=True)
    person_key = person_brain_tag_key(asker_slack_user_id) if (asker_slack_user_id or "").strip() else None

    def is_person_scoped(tags: list[str]) -> bool:
        return any(tag.startswith("person_") for tag in tags)

    ordered = [r for r in rows if person_key and person_key in r.tags] + [r for r in rows if not is_person_scoped(r.tags)]
    profile = InteractionStyleProfile()
    for row in ordered:
        text = (row.memory or "").strip()
        if not text:
            continue
        for bucket in row.buckets:
            facet = BUCKET_TO_FACET.get(bucket)
            if facet:
                getattr(profile, facet).append(text)
    return None if profile.is_empty() else profile


def render_interaction_style(profile: InteractionStyleProfile | None) -> str | None:
    if not profile:
        return None
    parts: list[str] = []
    for facet, title in SECTIONS:
        items = [item.strip() for item in getattr(profile, facet) if item.strip()]
        if items:
            parts += [f"{title}:", *(f"- {item}" for item in items)]
    return "\n".join(parts) if parts else None
