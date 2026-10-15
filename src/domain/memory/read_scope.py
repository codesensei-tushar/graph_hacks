"""The containers a turn may READ. Port of company-brain ``brain/memory/read-scope.ts``.

Always a superset of the single write container: org_shared + the current scope's
own container, plus, for a personal DM, every private channel the asker belongs to
and every private GitHub repo their linked GitHub account collaborates on (their full
access surface). Public and private channels only ever read org_shared and their own
container.
"""

from src.domain.github.repos import readable_github_scopes_for_slack_user
from src.domain.memory.writeback import (
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
    tags = [SHARED_TEAM_BRAIN_CONTAINER_TAG]
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
    return tags
