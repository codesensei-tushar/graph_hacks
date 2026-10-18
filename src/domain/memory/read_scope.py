"""The containers a turn may READ. Port of company-brain ``brain/memory/read-scope.ts``.

Always a superset of the single write container: org_shared, org_internal (what connected
tools such as Jira write; every Slack asker reads it, the public web page and MCP never do)
+ the current scope's own container, plus, for a personal DM, every private channel the
asker belongs to, every private GitHub repo their linked GitHub account collaborates on,
and every restricted connector project their linked account may read (their full access
surface). Public and private channels only ever read org_shared, org_internal and their
own container.

``org_internal`` is for full members of the org's workspaces: a turn whose audience is not
all full members (a guest asking, a channel with guests, a Slack Connect channel:
``scope.internal_audience`` False) reads neither it nor any restricted connector scope.
"""

from src.connectors.graph import readable_container_scopes_for_slack_user
from src.domain.github.repos import readable_github_scopes_for_slack_user
from src.domain.memory.writeback import (
    ORG_INTERNAL_CONTAINER_TAG,
    SHARED_TEAM_BRAIN_CONTAINER_TAG,
    SlackMemoryScope,
    slack_memory_container_tag,
)
from src.domain.slack.channel_membership import readable_slack_channel_container_tags_for_user


def resolve_brain_read_container_tags(
    scope: SlackMemoryScope | None,
    override: list[str] | None = None,
) -> list[str]:
    # An explicit empty surface means read nothing, not fall back to the derived set.
    if override is not None:
        return list(dict.fromkeys(override))
    internal = scope is None or scope.internal_audience is not False
    tags = [SHARED_TEAM_BRAIN_CONTAINER_TAG] + ([ORG_INTERNAL_CONTAINER_TAG] if internal else [])
    scope_tag = slack_memory_container_tag(scope)
    if scope_tag and scope_tag not in tags:
        tags.append(scope_tag)
    if scope and scope.kind == "dm" and scope.slack_user_id:
        for tag in readable_slack_channel_container_tags_for_user(scope.slack_user_id):
            if tag not in tags:
                tags.append(tag)
        for tag in readable_github_scopes_for_slack_user(scope.slack_user_id):
            if tag not in tags:
                tags.append(tag)
        for tag in readable_container_scopes_for_slack_user(scope.slack_user_id) if internal else []:
            if tag not in tags:
                tags.append(tag)
    return tags
