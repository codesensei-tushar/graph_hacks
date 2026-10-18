# Jira

The Jira connector reads the projects you allow from one Jira Cloud site and writes their
issues into the company graph as tickets. "Who knows billing?" then names the people Jira
has billing tickets assigned to, "who should know about PAY-12?" finds the ticket by its key,
and a pull request that says "fixes PAY-12" links to it. The framework under it (sync,
scopes, the graph writer) is described in [CONNECTORS.md](CONNECTORS.md).

The connector only reads. It polls Jira; webhooks are not used in v1.

## Configure

Create an API token at <https://id.atlassian.com/manage-profile/security/api-tokens> while signed
in as the account the brain should read as. The brain sees what that account can browse, so a
dedicated account that can browse only the allowed projects is the safest choice.

Set these where the app reads its environment (`.env` locally, Secret Manager in production;
don't paste the token into chats or tickets):

| Variable | Value |
| --- | --- |
| `CONNECTOR_JIRA_BASE_URL` | The site's origin, `https://<site>.atlassian.net`. https only, with no path. |
| `CONNECTOR_JIRA_EMAIL` | The email of the account the token belongs to |
| `CONNECTOR_JIRA_API_TOKEN` | The classic API token, sent as HTTP Basic auth with the email |
| `CONNECTOR_JIRA_PROJECTS` | The allow-list: comma-separated project keys, e.g. `PAY,PLAT`. Nothing else is read. |
| `CONNECTOR_JIRA_PEOPLE` | Optional: `accountId=email` pairs for people whose email Jira hides (see Identity below) |
| `CONNECTOR_JIRA_INSTALL_ID` | Optional: the install's name in node ids, `default` unless set |

The first four are required; with any of them missing, the connector stays off and the log
names the missing variables (never their values). On startup the app loads the install and
schedules its sync.

## What is ingested

| Jira | Graph |
| --- | --- |
| An allowed project | `(:Container {name, url, kind: 'project'})` |
| An issue in it, updated in the last 90 days | `(:Ticket {source: 'jira', name: 'PAY-12', title, summary, url, state, status, issue_type, priority, parent_key, project_key, labels, components, source_updated_at, synced_at, scope})-[:IN_CONTAINER]->(:Container)` |
| The reporter and the assignee | `(:ExternalUser {name})-[:OPENED]->(:Ticket)` and `-[:ASSIGNED]->` |
| An account with an email that exactly one Person has | `(:ExternalUser)-[:IS]->(:Person)` |
| The issue's components | `(:Ticket)-[:ABOUT]->(:Service)`: a component named after a service, or after one of its aliases. An issue with no component is matched on its title, the way GitHub issues are. |
| A "Blocks" link | `(:Ticket)-[:BLOCKS]->(:Ticket)`, from the blocking issue. A path edge only: it never becomes `DEPENDS_ON`, so it never changes a blast radius. |
| `PAY-12` in a pull request's title, branch or description | `(:PullRequest)-[:REFERENCES]->(:Ticket)`; "fixes PAY-12", "closes" or "resolves" writes `CLOSES` instead |

`state` is `closed` when the status is in Jira's Done category and `open` otherwise; the
status's own name goes in `status`. The summary is the description as plain text, at most
2000 characters. Comments, attachments, work logs, history and custom fields are not read.

A key in a pull request counts only when its project is one the connector syncs, so a Linear
id or a version string that looks like a key (`ENG-12`, `UTF-8`) links to nothing. A pull request
that names a ticket before it is synced links to it once it is. Keys must be upper case in a
title or description; any case works in a branch name (`pay-12-retry-fix`).

Node ids belong to the install: `jira:<install>:<issue id>` for a ticket,
`jira:<install>:user:<accountId>` for an account and `jira:<install>:container:<project id>` for
a project. Issue ids and account ids never change, so a renamed key or a moved issue updates
the same node.

## Permissions

Everything the connector writes is `org_internal`:

| Where the question is asked | Sees Jira data |
| --- | --- |
| Slack: any channel, private channel or DM | Yes |
| The public ask page, the team pages, MCP | No |

The brain does not copy Jira's project permissions. **Allow only projects that everyone in the
Slack workspace may see.** Within them:

- **An issue with a security level is never written.** If one gains a level after it was synced,
  the next poll deletes it from the graph.
- **A project removed from `CONNECTOR_JIRA_PROJECTS`**, or one the token's account can no longer
  browse, is deleted from the graph with all its tickets on the next container refresh (hourly,
  or at once with a sync-now).
- An account's email is used only to link it to a Person. It is never shown in an answer.

## Identity

A Jira account links to a Person only when its email matches exactly one Person's email,
case-insensitively. Never by name: two people can share a name, and a name is anyone's to
type. An account that links counts as that Person everywhere (who knows, who should know,
if they leave, ownership). One that doesn't still counts, named with its tool ("Robin
Contractor (Jira)", `ExternalUser` in the path), so it is never folded into a Person who happens
to share its name.

Jira often hides emails: the API returns `emailAddress` only when the person's profile
visibility allows it. For those people an admin can assert the email in `CONNECTOR_JIRA_PEOPLE`:

```
CONNECTOR_JIRA_PEOPLE=712020:0b1c7f2e-1111-4aaa-9bbb-000000000001=maya@example.com,5b10ac8d82e05b22cc7d4ef5=lena@example.com
```

A mapped email is used only when Jira hides the real one, and it links by the same rule: a
unique email match, or nothing.

**Finding an accountId.** In Jira, open the person's profile (click their avatar, then
*View profile*). The URL ends in `/jira/people/<accountId>`. A site admin can also find it at
*admin.atlassian.com → Directory → Users →* the user (the URL holds the id), or read it from the
API as `accountId`:
`GET https://<site>.atlassian.net/rest/api/3/user/search?query=<name>`.

After changing the mapping, run a full sync (below) so accounts already written are linked again.

**People and Services that arrive later.** An account is linked, and a ticket's components are
resolved to Services, only when the ticket is written. A Jira poll rewrites only issues that
changed, so it does not link them (unlike HubSpot, whose every poll re-sends every owner).
A team file import (`/ops/import`, the console) or `onboard team:` that adds people or
Services therefore queues a full sync of each of the org's connectors. The full sync rewrites
every synced ticket (each project is capped at 1000) against the People and Services that exist
by then.

## Freshness

The sync runs on the app's scheduler, one page of up to 100 issues per step:

1. **Backfill**, on the first run or after a full sync: each project's issues updated in the
   last 90 days, newest first, at most 500 per project. A project that already holds the
   framework's limit (1000 tickets) stops its backfill at once and polls from the newest issue
   it saw, so it never reads older pages it could not keep.
2. **Polls**, every five minutes after that: issues updated since the newest one already seen,
   oldest first. The cursor only moves forward, and each poll re-reads a few minutes of overlap,
   which writes nothing new.

The window is written relative to now (`updated >= -42m`) because Jira reads an absolute time in
JQL in the token owner's time zone. A 429 is retried after `Retry-After`; a long wait, a 401
or an outage backs off as [CONNECTORS.md](CONNECTORS.md) describes, and nothing is lost: the
cursor does not move until a page is written.

A deleted Jira issue stays in the graph in v1 (search does not report deletions). Removing the
project from the allow-list and adding it back rebuilds it.

## Run a sync

With the variables set, starting the app syncs on its own. To check on it or sync now (bearer
`BRAIN_ADMIN_TOKEN`):

```bash
curl -H "Authorization: Bearer $BRAIN_ADMIN_TOKEN" https://YOUR-HOST/ops/connectors
curl -X POST -H "Authorization: Bearer $BRAIN_ADMIN_TOKEN" "https://YOUR-HOST/ops/connectors/jira/sync"
curl -X POST -H "Authorization: Bearer $BRAIN_ADMIN_TOKEN" "https://YOUR-HOST/ops/connectors/jira/sync?full=true"   # backfill again
```

The status shows the account and site the token reads as, each project's item count, cursor,
whether it is backfilled, and the last error. It never shows a setting's value.

## Seed a test site

`scripts/seed_jira.py` fills a Jira site with synthetic data that lines up with the Quillfern
seed (`src/seed/quillfern.py`). It creates two company-managed projects, PAY "Quillfern Payments"
and PLAT "Quillfern Platform", with components named after Quillfern's services. Then 35 issues:
epics with children, a mix of to do, in progress and done, and six "Blocks" links, two of them
between projects.

```bash
.venv/bin/python scripts/seed_jira.py --env-file ~/.config/company-brain/connectors.env            # print the plan
.venv/bin/python scripts/seed_jira.py --env-file ~/.config/company-brain/connectors.env --apply    # create it
```

- **Dry run by default.** `--apply` writes.
- **It needs a Jira admin** to create projects.
- **It never changes what it did not create.** A project it made carries the project property
  `company-brain-seed`, and an issue it made carries the labels `brain-seed` and
  `brain-seed-<id>`. If a PAY or PLAT project already exists without the property, it stops
  before writing anything. It never deletes.
- **It is idempotent.** A second run creates only what is missing.
- **Nobody gets email about it.** Issues are assigned round-robin over the site's active people,
  last and with `notifyUsers=false`.

Then sync it with `CONNECTOR_JIRA_PROJECTS=PAY,PLAT`.

## Code

| Path | Role |
| --- | --- |
| `src/connectors/jira/__init__.py` | `JiraConnector`: the allow-list, the backfill and poll cursors, `CONNECTOR_JIRA_PEOPLE` |
| `src/connectors/jira/mapper.py` | Jira JSON to tickets, accounts and edges, as pure functions |
| `src/connectors/jira/client.py` | The REST v3 calls, on `src/connectors/http.py` |
| `src/domain/github/ingest.py` | Pull request to Jira ticket links (`link_jira_tickets`) |
| `scripts/seed_jira.py` | The synthetic test site |
| `tests/fixtures/jira/` | Synthetic API responses and `FakeJiraClient` |
