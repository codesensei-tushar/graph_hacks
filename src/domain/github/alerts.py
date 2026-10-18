"""A heads-up in Slack when a new pull request reaches people who weren't on it.

After a public repo's pull request is opened, reopened or marked ready for review, and has
been ingested, ``who_should_know`` runs for it with the ``org_shared`` read scope. When it
flags anyone, its reply and path go to the home channel of every installed workspace:

    Heads-up on Shorten session token TTL (https://github.com/quillfern/notebook/pull/58):
    notebook#58 changes Auth Service. 3 people who own something that depends on it ...

Once per pull request: ``github:coordination:<repo_id>:<number>`` is taken with SET NX for
seven days, and given back when nothing reached Slack, so a later event can try again.
Private repos never alert, so their details stay out of shared channels; drafts wait until
they are ready for review. Off unless ``COORDINATION_ALERTS`` is set. The webhook handler
calls this inside a try/except, after ingestion: an alert never breaks or holds up ingest.
"""

import logging

from src.core.config import settings
from src.db.falkordb_client import kv
from src.domain.github.ingest import pull_request_id
from src.domain.github.repos import repo_scope
from src.domain.knowledge import coordination
from src.domain.knowledge.resolve import ORG_SHARED
from src.domain.slack.format import format_answer
from src.domain.slack.workspace import escape_slack_text, list_connected_workspaces

log = logging.getLogger(__name__)

DEDUPE_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_TITLE_CHARS = 200


def dedupe_key(repo_id, number) -> str:
    return f"github:coordination:{repo_id}:{number}"


def alert_pull_request(repo: dict, pr: dict) -> str:
    """Post the heads-up for one ingested pull request, at most once. Returns what happened, for the log."""
    if not settings.coordination_alerts:
        return "off"
    if repo_scope(repo) != ORG_SHARED:
        return "private"
    if pr.get("draft"):
        return "draft"
    report = coordination.pull_request_report(pull_request_id(repo["id"], pr["number"]), [ORG_SHARED])
    if report is None:
        return "not_ingested"
    result, flagged = report
    if not flagged:
        return "nobody"
    homes = [workspace for workspace in list_connected_workspaces() if workspace.home_channel_id]
    if not homes:
        return "no_home_channel"
    key = dedupe_key(repo["id"], pr["number"])
    if not kv().set(key, "1", nx=True, ex=DEDUPE_TTL_SECONDS):
        return "duplicate"
    text = message(pr, result)
    posted = False
    try:
        for workspace in homes:
            if workspace.client.post_message(workspace.home_channel_id, text):
                posted = True
            else:
                log.warning("[github] coordination alert not posted team=%s channel=%s", workspace.team_id, workspace.home_channel_id)
    finally:
        if not posted:
            # Nothing reached Slack: give the key back so the next event for this pull request can try again.
            kv().delete(key)
    return "posted" if posted else "failed"


def message(pr: dict, result) -> str:
    """The heads-up line, then the reply and its path as any answer prints them.

    The title is anyone's text on a public repo, so it is escaped: "<!channel>" or a
    "<url|label>" link in a title shows as typed instead of pinging or hiding a link.
    """
    title = " ".join(str(pr.get("title") or "").split()) or f"#{pr.get('number')}"
    if len(title) > MAX_TITLE_CHARS:
        title = title[: MAX_TITLE_CHARS - 1] + "…"
    url = str(pr.get("html_url") or "")
    heads_up = f"Heads-up on {escape_slack_text(title)}" + (f" ({escape_slack_text(url)}):" if url else ":")
    return f"{heads_up}\n{format_answer(result.to_dict(), [], [ORG_SHARED], {})}"
