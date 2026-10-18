"""A heads-up in Slack when a new pull request reaches people who weren't on it.

After a public repo's pull request is opened, reopened or marked ready for review, and has
been ingested, ``who_should_know`` runs for it with the ``org_shared`` read scope. When it
flags anyone, its reply and path go to the home channel of every installed workspace:

    Heads-up on Shorten session token TTL (https://github.com/quillfern/notebook/pull/58):
    notebook#58 changes Auth Service. 3 people who own something that depends on it ...

At most once per pull request every seven days: ``github:coordination:<repo_id>:<number>``
is taken with SET NX and a seven-day expiry. When nothing reached Slack the key is given
back and the post is retried on the scheduler, after one, five and thirty minutes.

Only for people the company knows: the author is linked to a person in the graph, or
GitHub marks them as an owner, member or collaborator of the repo, so a stranger opening
pull requests on a public repo cannot make the bot post. Private repos never alert, so
their details stay out of shared channels; drafts wait until they are ready for review.
Off unless ``COORDINATION_ALERTS`` is set. The webhook handler calls this inside a
try/except after ingestion: an alert never breaks or holds up ingest.
"""

import logging

from src.core import scheduler
from src.core.config import settings
from src.db.falkordb_client import connect, kv
from src.domain.github.ingest import pull_request_id
from src.domain.github.repos import repo_scope
from src.domain.knowledge import coordination
from src.domain.knowledge.resolve import ORG_SHARED
from src.domain.slack.format import format_answer
from src.domain.slack.workspace import escape_slack_text, list_connected_workspaces

log = logging.getLogger(__name__)

DEDUPE_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_TITLE_CHARS = 200
RETRY_JOB = "runCoordinationAlertRetry"
# Seconds before each retry after nothing reached Slack; then it gives up and says so in the log.
RETRY_DELAYS = (60, 5 * 60, 30 * 60)
# GitHub's author_association values for people the repo already trusts.
_TRUSTED = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
# What a retry needs from the webhook payload, so the scheduled job stays small.
_REPO_FIELDS = ("id", "full_name", "private")
_PR_FIELDS = ("number", "title", "html_url", "draft", "author_association", "user")


def dedupe_key(repo_id, number) -> str:
    return f"github:coordination:{repo_id}:{number}"


def retry_job_id(repo_id, number) -> str:
    return f"github:coordination-retry:{repo_id}:{number}"


def alert_pull_request(repo: dict, pr: dict, attempt: int = 0) -> str:
    """Post the heads-up for one ingested pull request, at most once. Returns what happened, for the log.

    The cheap reasons not to post come first; the report, which reads the graph, comes last.
    """
    if not settings.coordination_alerts:
        return "off"
    if repo_scope(repo) != ORG_SHARED:
        return "private"
    if pr.get("draft"):
        return "draft"
    if not _known_author(pr):
        return "outside_author"
    homes = [workspace for workspace in list_connected_workspaces() if workspace.home_channel_id]
    if not homes:
        return "no_home_channel"
    key = dedupe_key(repo["id"], pr["number"])
    if kv().exists(key):
        return "duplicate"
    report = coordination.pull_request_report(pull_request_id(repo["id"], pr["number"]), [ORG_SHARED])
    if report is None:
        return "not_ingested"
    result, flagged = report
    if not flagged:
        return "nobody"
    # Built before the key is taken: a message that cannot be built must not hold the key for a week.
    text = message(pr, result)
    if not kv().set(key, "1", nx=True, ex=DEDUPE_TTL_SECONDS):
        return "duplicate"
    posted = [workspace.team_id for workspace in homes if _post(workspace, text)]
    if not posted:
        # Nothing reached Slack: give the key back, and try again later.
        kv().delete(key)
        _schedule_retry(repo, pr, attempt)
        return "failed"
    return f"posted to {len(posted)} workspace{'s' if len(posted) != 1 else ''} for {', '.join(flagged)}"


def retry_alert(payload: dict) -> None:
    """The scheduled retry: the same checks again, so a pull request that changed since is judged as it is now."""
    try:
        outcome = alert_pull_request(payload["repo"], payload["pr"], payload.get("attempt", 1))
        log.info("[github] coordination alert retry repo=%s pr=%s outcome=%s", payload["repo"].get("full_name"), payload["pr"].get("number"), outcome)
    except Exception:
        log.exception("[github] coordination alert retry failed repo=%s pr=%s", payload["repo"].get("full_name"), payload["pr"].get("number"))


def message(pr: dict, result) -> str:
    """The heads-up line, then the reply and its path as any answer prints them.

    The title is anyone's text on a public repo. It is escaped, so "<!channel>" or a
    "<url|label>" link shows as typed instead of pinging or hiding a link, and "](" is
    split, so a markdown link "[label](url)" is not turned into a hidden Slack link either.
    """
    title = " ".join(str(pr.get("title") or "").split()) or f"#{pr.get('number')}"
    if len(title) > MAX_TITLE_CHARS:
        title = title[: MAX_TITLE_CHARS - 1] + "…"
    title = escape_slack_text(title).replace("](", "] (")
    url = str(pr.get("html_url") or "")
    heads_up = f"Heads-up on {title}" + (f" ({escape_slack_text(url)}):" if url else ":")
    return f"{heads_up}\n{format_answer(result.to_dict(), [], [ORG_SHARED], {})}"


def _known_author(pr: dict) -> bool:
    if str(pr.get("author_association") or "").upper() in _TRUSTED:
        return True
    author = (pr.get("user") or {}).get("id")
    if author is None:
        return False
    _, graph = connect()
    rows = graph.ro_query("MATCH (:GitHubUser {id: $id})-[:IS]->(p:Person) RETURN count(p)", {"id": str(author)}).result_set
    return bool(rows and rows[0][0])


def _post(workspace, text: str) -> bool:
    """One workspace's post. A failure is logged and never stops the next workspace."""
    try:
        if workspace.client.post_message(workspace.home_channel_id, text):
            return True
        log.warning("[github] coordination alert not posted team=%s channel=%s", workspace.team_id, workspace.home_channel_id)
    except Exception:
        log.exception("[github] coordination alert post failed team=%s channel=%s", workspace.team_id, workspace.home_channel_id)
    return False


def _schedule_retry(repo: dict, pr: dict, attempt: int) -> None:
    if attempt >= len(RETRY_DELAYS):
        log.warning("[github] coordination alert gave up repo=%s pr=%s after %d tries", repo.get("full_name"), pr.get("number"), attempt + 1)
        return
    payload = {
        "repo": {field: repo.get(field) for field in _REPO_FIELDS},
        "pr": {field: pr.get(field) for field in _PR_FIELDS},
        "attempt": attempt + 1,
    }
    scheduler.schedule(RETRY_DELAYS[attempt], RETRY_JOB, payload, job_id=retry_job_id(repo["id"], pr["number"]))


scheduler.register(RETRY_JOB, retry_alert)
