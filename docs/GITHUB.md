# GitHub

A GitHub App feeds pull requests, reviews, and issues into the same FalkorDB graph the Slack bot answers from. "Who knows the memory layer?" then includes who wrote and reviewed the pull requests that changed `src/domain/memory/`, and issues show up as tickets.

The app is read-only. It stores titles, descriptions, people, file paths, and states. It never stores code or diffs.

## Register the app (operator page, no terminal)

One app per deployment. The operator registers it from a browser with GitHub's [App Manifest flow](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest):

1. Open `https://YOUR-HOST/ops/github/app` and enter `BRAIN_ADMIN_TOKEN` (kept in the tab's memory only).
2. Pick a name (GitHub App names are unique across GitHub; the default is "Company Brain") and, optionally, the GitHub organization that should own the app. Empty means your personal account.
3. **Register on GitHub** posts the manifest to `https://github.com/settings/apps/new` (or `https://github.com/organizations/<org>/settings/apps/new`) with a single-use `state`. Click **Create GitHub App** there.
4. GitHub redirects to `/ops/github/app/created?code=...&state=...`. The server takes the state once, trades the code at [`POST /app-manifests/{code}/conversions`](https://docs.github.com/en/rest/apps/apps#create-a-github-app-from-a-manifest) for the app id, slug, private key, webhook secret, client id and client secret, and stores them in the control graph (`(:GitHubApp {id: 'default'})`). The secrets are encrypted with MultiFernet (`TOKEN_ENCRYPTION_KEYS`, `allow_plaintext=False`). Without a key, the page refuses to start. The whole flow must finish within an hour (GitHub's limit).

The manifest it posts:

| Field | Value |
| --- | --- |
| `url` | `PUBLIC_URL` |
| `hook_attributes` | `{url: PUBLIC_URL/github/webhook, active: true}` |
| `redirect_url` | `PUBLIC_URL/ops/github/app/created` |
| `setup_url` | `PUBLIC_URL/console/oauth/github/setup`, with `setup_on_update: true` |
| `callback_urls` | `PUBLIC_URL/console/oauth/github/verify` (the install check) and `PUBLIC_URL/github/oauth/callback` (`link github`) |
| `public` | `true`, so other companies can install it on their own accounts |
| `default_permissions` | `metadata`, `contents`, `pull_requests`, `issues`, `members`: all `read` |
| `default_events` | `pull_request`, `pull_request_review`, `issues`, `repository`, `member` |

`installation` and `installation_repositories` events reach every GitHub App, and GitHub refuses a subscription to them, so they are not listed. `members: read` is an organization permission. It is needed for the `member` event and does nothing on a personal account.

A second registration is refused while one is stored, because it would strand the first app's installations. `GET /ops/github/app/status` (admin token) shows the app: its source, id, slug, owner and install link. It never shows a secret. `POST /ops/tokens/rewrap` re-encrypts the stored app with the other credentials.

## Or: configure the app by environment (as before)

Environment variables still work, and **they win** over a registered app whenever `GITHUB_APP_ID`, `GITHUB_WEBHOOK_SECRET` and a private key are set. Create the app by hand:

**GitHub → Settings → Developer settings → GitHub Apps → New GitHub App**

| Field | Value |
| --- | --- |
| GitHub App name | `company-brain-dev` (names are global, so pick a free one) |
| Homepage URL | The repo URL |
| Callback URL | `https://YOUR-HOST/github/oauth/callback` (and `https://YOUR-HOST/console/oauth/github/verify` to connect from the console) |
| Setup URL | `https://YOUR-HOST/console/oauth/github/setup`, with "Redirect on update" on (only for the console) |
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

## Public repos by URL (no installation)

A public repo can be read with no installation of the app at all, because GitHub answers public data to anyone. The operator adds one to any org. An org's owner or admin can add one to their own org from the console.

- **Operator:** open `/ops/github/app`, enter the admin token, and under **Public repos** give the org (`quillfern` is the legacy org), the address (`https://github.com/<owner>/<repo>`) and how many pull requests to read (**0 = all of them**). Or use the API:

  ```bash
  curl -X POST "https://YOUR-HOST/ops/github/repos?org=quillfern" -H "Authorization: Bearer $BRAIN_ADMIN_TOKEN" \
       -H 'Content-Type: application/json' -d '{"url": "https://github.com/codesensei-tushar/graph_hacks", "backfill_limit": 0}'
  curl "https://YOUR-HOST/ops/github/repos?org=quillfern" -H "Authorization: Bearer $BRAIN_ADMIN_TOKEN"   # progress
  ```

  `POST /ops/github/repos/<repo id>/sync` (optionally `{"backfill_limit": N}`) reads it again from the start, for example after the seed's `Service.paths` changed so `TOUCHES` is recomputed. `DELETE /ops/github/repos/<repo id>` removes it.
- **Console:** the same calls under `/console/api/github/repos` act on the session's org. Members list; owners and admins add, sync and remove.

Its pull requests, reviews and issues are `org_shared`: everyone in the org reads them. For the legacy org that includes the public ask page and MCP. No collaborators are read and no webhooks arrive. After the backfill, the repo is polled every 15 minutes for issues and pull requests updated since the last poll. If a repo turns private, or its name now points at another repo, reading stops and the status says so. A repo the org already reads through an installation is refused, and so is a private one (GitHub answers 404 for it).

**The backfill** reads one page per scheduler job: 100 issues, or 10 pull requests with their changed files and reviews. A restart resumes it, and the shared scheduler thread is never held for long. Pull requests come newest first, `backfill_limit` deep (default 50; 0 = all). Every write is an upsert, so a page read twice changes nothing. A rate limit (`Retry-After`, or `X-RateLimit-Remaining: 0` until `X-RateLimit-Reset`) puts the page off until then, up to an hour. Any other failure backs off 1, 5, 15, then 60 minutes. Progress is on the `Repo` node (`bf_phase`, `bf_page`, `bf_pulls`, `bf_issues`, `bf_error`) and in the ops listing.

**Rate limits.** Public reads use, in order:

1. `GITHUB_PUBLIC_TOKEN`, if set: a token used only for this. A fine-grained token with public read access is enough. 5,000 requests an hour.
2. An installation token of this app's installation **on its owner's own account**. Another company's installation is never used, because its rate limit is theirs.
3. The app's client id and secret as basic auth. GitHub [documents this for OAuth apps](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api) at 5,000 an hour; a refusal (401) moves on to the next option.
4. Nothing: 60 requests an hour. The backfill still finishes, because it waits out each limit, but a repo of 220 pull requests takes about 8 hours this way. Each pull request costs 3 requests.

**The demo repo (`codesensei-tushar/graph_hacks`, public, about 220 pull requests).** Add it to the legacy org with depth 0 as above. No GitHub install is needed. For the full backfill in minutes rather than hours, either set `GITHUB_PUBLIC_TOKEN`, or register the app (above) and install it once on the app owner's own account (any repo will do, even one).

If the repo's live events (webhooks rather than the 15-minute poll) are wanted, or the repo were private, the app has to be installed on the account that owns it. `codesensei-tushar` is a user account, so only that user can install it: one click on the app's install link (`/ops/github/app` shows it), choosing the repo. The alternative would be moving the repo into a GitHub organization the operator administers; that is not planned.

## Configure

Locally, in `.env` (edit the file; don't paste these into chats or tickets):

```
GITHUB_APP_ID=123456
GITHUB_WEBHOOK_SECRET=<the openssl value>
GITHUB_PRIVATE_KEY_PATH=/home/you/.config/company-brain/github-app.pem
GITHUB_CLIENT_ID=Iv23...
GITHUB_CLIENT_SECRET=...
GITHUB_APP_SLUG=company-brain-dev   # optional: the console's Connect button links to github.com/apps/<slug>
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

With `COORDINATION_ALERTS=true`, a public repo's pull request that is opened, reopened or marked ready for review is ingested first, then `who_should_know` runs for it with the public read scope. Its author, its reviewers and anyone asked to review it (`requested_reviewers`, including CODEOWNERS) count as in the loop. If it flags anyone, the bot posts to each installed workspace's home channel: "Heads-up on <title> (<url>):", then the reply and its path.

- **At most once per pull request every seven days:** `github:coordination:<repo_id>:<number>`, SET NX with a seven-day expiry. If nothing reached Slack the key is given back and the post is retried on the scheduler after one, five and thirty minutes.
- **Only for people the company knows:** the author is linked to a person in the graph, or GitHub marks them as an owner, member or collaborator. A stranger opening pull requests on a public repo cannot make the bot post.
- **Never for private repos**, so their details stay out of shared channels. Drafts wait until they are ready for review.
- **Titles are anyone's text:** they are escaped, and markdown links are split, so a title cannot ping a channel or hide a link.

A failure is logged as `[github] coordination alert failed` and never affects ingestion; each run logs its outcome (`posted to 1 workspace for ...`, `nobody`, `duplicate`, `private`, `outside_author`, `failed`, ...).

## Graph

```
(:Repo {id, full_name, private, installation_id, source, scope, bf_*})   source: installation | public
(:GitHubUser {id, login})-[:CAN_READ]->(:Repo)                    collaborators
(:GitHubUser)-[:IS]->(:Person)                                     linked account
(:GitHubUser)-[:AUTHORED]->(:PullRequest {created_at, merged_at, closed_at, updated_at})-[:IN_REPO]->(:Repo)
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
| `src/api/v1/github_app.py`, `src/static/ops/github-app.*` | The operator page: register the app (manifest flow) |
| `src/domain/github/app_registration.py` | The manifest, its state, the code conversion |
| `src/integrations/github/app_config.py` | Which app runs: env, else the stored one (encrypted in the control graph) |
| `src/api/v1/github_repos.py`, `src/domain/github/public_repos.py` | Public repos by URL (ops and console) |
| `src/api/v1/github.py` | `POST /github/webhook` (signature, dedupe on `X-GitHub-Delivery`, background work) and `GET /github/oauth/callback` |
| `src/domain/github/webhooks.py` | What each event does |
| `src/domain/github/ingest.py` | Pull requests, reviews, and issues into the graph |
| `src/domain/github/backfill.py` | The stepped backfill (a page per job), the public-repo poll, batched removal, the hourly collaborator refresh |
| `src/domain/github/repos.py` | Repo scopes and who may read them |
| `src/domain/github/account_link.py` | `link github` |
| `src/integrations/github/` | Signature check and the read-only REST client |
