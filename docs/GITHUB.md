# GitHub

A GitHub App feeds pull requests, reviews, and issues into the same FalkorDB graph the Slack bot answers from. "Who knows the memory layer?" then includes who wrote and reviewed the pull requests that changed `src/domain/memory/`, and issues show up as tickets.

The app is read-only. It stores titles, descriptions, people, file paths, and states. It never stores code or diffs.

## Create the app

One app per environment, like Slack: a dev app on your ngrok URL and a production app on `https://brain.noobsurd.space`.

**GitHub → Settings → Developer settings → GitHub Apps → New GitHub App**

| Field | Value |
| --- | --- |
| GitHub App name | `company-brain-dev` (names are global, so pick a free one) |
| Homepage URL | The repo URL |
| Callback URL | `https://YOUR-HOST/github/oauth/callback` |
| Request user authorization (OAuth) during installation | Off |
| Webhook → Active | On |
| Webhook URL | `https://YOUR-HOST/github/webhook` |
| Webhook secret | `openssl rand -hex 32` |
| Where can this GitHub App be installed? | Only on this account |

**Repository permissions**, all read-only, everything else "No access":

| Permission | Why |
| --- | --- |
| Metadata: Read | Required. Also lists collaborators, which decides who can read a private repo's data. |
| Pull requests: Read | Pull requests, reviews, changed files |
| Issues: Read | Issues, written as tickets |
| Contents: Read | Lets the backfill read pull request details. No file contents are stored. |

**Subscribe to events:** Pull request, Pull request review, Issues. Install and repository-selection events arrive without subscribing.

Then:

1. Note the **App ID** and the **Client ID** at the top of the app page.
2. **Generate a new client secret** and copy it.
3. **Private keys → Generate a private key.** Keep the downloaded `.pem` outside the repo, for example `~/.config/company-brain/github-app.pem`.
4. **Install App** → your account → **Only select repositories** → pick the repos.

## Configure

Locally, in `.env` (edit the file; don't paste these into chats or tickets):

```
GITHUB_APP_ID=123456
GITHUB_WEBHOOK_SECRET=<the openssl value>
GITHUB_PRIVATE_KEY_PATH=/home/you/.config/company-brain/github-app.pem
GITHUB_CLIENT_ID=Iv23...
GITHUB_CLIENT_SECRET=...
PUBLIC_URL=https://YOUR-HOST
```

`PUBLIC_URL` builds the sign-in callback, so it must match the app's Callback URL. Restart the app afterwards.

In production, add the same names as Secret Manager secrets (see [DEPLOY.md](DEPLOY.md)). `deploy.sh` refuses values with newlines, so store the key as `GITHUB_PRIVATE_KEY` on one line; the app turns `\n` back into newlines:

```bash
awk '{printf "%s\\n", $0}' github-app.pem | gcloud secrets create GITHUB_PRIVATE_KEY --data-file=- --project=graphhacks-brain-8490
```

## What happens on install

The `installation` webhook starts a backfill of every selected repo: the collaborator list, the 50 most recently updated issues, and the 50 most recently updated pull requests with their changed files and reviews. After that, webhooks keep the graph current. Collaborators are refreshed every hour.

Uninstalling the app, or removing a repo from it, deletes that repo's nodes.

Check deliveries under the app's **Advanced → Recent Deliveries**. A green tick means the app answered 200; the outcome is in the app log as `[github] event=... outcome=...`.

## Heads-up on a new pull request

With `COORDINATION_ALERTS=true`, a public repo's pull request that is opened, reopened or marked ready for review is ingested first, then `who_should_know` runs for it with the public read scope. If it flags anyone, the bot posts to each installed workspace's home channel: "Heads-up on <title> (<url>):", then the reply and its path. A pull request posts once (`github:coordination:<repo_id>:<number>`, SET NX for seven days; the key is given back if nothing reached Slack). Private repos never post, drafts wait until they are ready for review, and the title is escaped, so it cannot ping a channel. A failure is logged as `[github] coordination alert failed` and never affects ingestion; each run logs its outcome (`posted`, `nobody`, `duplicate`, `private`, ...).

## Graph

```
(:Repo {id, full_name, private, installation_id, scope})
(:GitHubUser {id, login})-[:CAN_READ]->(:Repo)                    collaborators
(:GitHubUser)-[:IS]->(:Person)                                     linked account
(:GitHubUser)-[:AUTHORED]->(:PullRequest)-[:IN_REPO]->(:Repo)
(:GitHubUser)-[:REVIEWED {state}]->(:PullRequest)
(:PullRequest)-[:TOUCHES]->(:Service)                              changed files under Service.paths
(:PullRequest)-[:CLOSES]->(:Ticket)                                "closes #12" in the description
(:GitHubUser)-[:OPENED|ASSIGNED]->(:Ticket {source: 'github'})-[:ABOUT]->(:Service)
```

A pull request links to a service when it changes a file under one of that service's `paths` (set in the seed, for example `src/domain/memory` for the Memory layer). An issue links to the service its title and body match best.

## Permissions

| Repo | Scope | Who can read it |
| --- | --- | --- |
| Public | `org_shared` | Everyone, from any channel |
| Private | `github_repo_<id>` | In a DM only, people whose linked GitHub account is a collaborator |

Private repo data never shows in a channel, because not everyone in a channel has repo access. If an account isn't linked, or the collaborator list can't be read, the answer is no.

### Linking your GitHub account

DM the bot `link github`. It replies with a GitHub sign-in link that expires in 15 minutes and works once. Signing in proves which GitHub account is yours; the app reads your login and drops the user token right away. A typed username is never trusted, since anyone could claim a collaborator's name.

A seeded `Person` with a `github` login (like `tushar` in `src/seed/company_brain.py`) is linked to that GitHub user automatically. That only links the Person; your Slack account still has to map to that Person for a DM to use it.

## Code

| Path | Role |
| --- | --- |
| `src/api/v1/github.py` | `POST /github/webhook` (signature, dedupe on `X-GitHub-Delivery`, background work) and `GET /github/oauth/callback` |
| `src/domain/github/webhooks.py` | What each event does |
| `src/domain/github/ingest.py` | Pull requests, reviews, and issues into the graph |
| `src/domain/github/backfill.py` | Install backfill and the hourly collaborator refresh |
| `src/domain/github/repos.py` | Repo scopes and who may read them |
| `src/domain/github/account_link.py` | `link github` |
| `src/integrations/github/` | Signature check and the read-only REST client |
