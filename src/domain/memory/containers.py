"""Memory containers and their settings.

Port of company-brain `compat/lib/spaces/provisioning.ts` and `memory/container-tags.ts`.
A container is a FalkorDB ``Scope`` node (``id`` = container tag). supermemory created a
container on first write; here the first write MERGEs the Scope node, so there is
still nothing to provision. Container settings (display name, entity context,
profile buckets) live as properties on that node.
"""

import json
from dataclasses import dataclass, field

from src.db.falkordb_client import connect

SHARED_TEAM_BRAIN_CONTAINER_TAG = "org_shared"
AGENT_SELF_CONTAINER_TAG = "agent_self"
# What connected tools (Jira, HubSpot) write: every Slack asker reads it, the public web page,
# the team pages and MCP never do. Nothing is ever written to it from Slack.
ORG_INTERNAL_CONTAINER_TAG = "org_internal"
# A restricted connector container's own scope: ``connector_<hash of the container id>``.
CONNECTOR_SCOPE_PREFIX = "connector_"


def private_container_tag_for(user_id: str) -> str:
    return f"user_{user_id}"


def private_slack_channel_container_tag(channel_id: str) -> str:
    return f"slack_channel_{channel_id}"


def build_org_entity_context(org_name: str, domain: str | None = None, about: str | None = None) -> str:
    header = f"Organization: {org_name}{f' ({domain})' if domain else ''}. This is the shared company brain for everyone in this org."
    about_line = f"About: {about.strip()}" if about and about.strip() else ""
    return "\n".join(
        line
        for line in (
            header,
            about_line,
            "Scope every memory to this organization — its people, teams, projects, customers, decisions, and product/domain terms.",
            "A full profile isn't provided yet: infer the org's products, structure, and vocabulary from ingested content, and treat recurring names (people, repos, products, customers, projects) as this org's entities.",
        )
        if line
    )


@dataclass
class ProfileBucketDef:
    key: str
    description: str


@dataclass
class ContainerTagSettings:
    container_tag: str
    name: str | None = None
    entity_context: str | None = None
    profile_buckets: list[ProfileBucketDef] = field(default_factory=list)


def get_container_tag_settings(container_tag: str) -> ContainerTagSettings | None:
    """A container's settings, or None before anything has created it."""
    _, graph = connect()
    rows = graph.query(
        "MATCH (s:Scope {id: $id}) RETURN s.name, s.entity_context, s.profile_buckets",
        {"id": container_tag},
    ).result_set
    if not rows:
        return None
    name, entity_context, buckets_json = rows[0]
    buckets = [ProfileBucketDef(**bucket) for bucket in json.loads(buckets_json)] if buckets_json else []
    return ContainerTagSettings(container_tag=container_tag, name=name, entity_context=entity_context, profile_buckets=buckets)


def update_container_tag_settings(
    container_tag: str,
    name: str | None = None,
    entity_context: str | None = None,
    profile_buckets: list[ProfileBucketDef] | None = None,
) -> None:
    """Set a container's display name, entity context and profile buckets. Creates it if missing."""
    assignments = []
    params: dict = {"id": container_tag}
    if name is not None:
        assignments.append("s.name = $name")
        params["name"] = name
    if entity_context is not None:
        assignments.append("s.entity_context = $entity_context")
        params["entity_context"] = entity_context
    if profile_buckets is not None:
        assignments.append("s.profile_buckets = $buckets")
        params["buckets"] = json.dumps([{"key": b.key, "description": b.description} for b in profile_buckets])
    set_clause = f" SET {', '.join(assignments)}" if assignments else ""
    _, graph = connect()
    graph.query(f"MERGE (s:Scope {{id: $id}}){set_clause}", params)
