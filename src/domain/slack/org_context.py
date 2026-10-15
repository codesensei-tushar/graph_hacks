"""Port of company-brain `brain/slack/org-context.ts`.

The org's Slack surface for internal work that runs without an incoming event
(scheduled jobs, rollouts): a client to read with, and the home channel when set.
"""

from dataclasses import dataclass

from src.domain.slack.workspace import SlackWorkspace, get_workspace_by_team_id, list_connected_workspaces


@dataclass(frozen=True)
class OrgSlackContext:
    workspace: SlackWorkspace
    team_id: str
    # Empty when the workspace has no home channel yet.
    channel_id: str
    installed_by: str | None


def load_org_slack_context(team_id: str | None = None) -> OrgSlackContext | None:
    workspace = get_workspace_by_team_id(team_id) if team_id else next(iter(list_connected_workspaces()), None)
    # Fail closed: never read one workspace's Slack as another.
    if workspace is None or (team_id and workspace.team_id != team_id):
        return None
    return OrgSlackContext(
        workspace=workspace,
        team_id=workspace.team_id,
        channel_id=workspace.home_channel_id or "",
        installed_by=workspace.installed_by,
    )
