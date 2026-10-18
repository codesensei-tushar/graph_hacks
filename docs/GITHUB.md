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

## Connect GitHub (each company, from the console)

Each company installs the app on its own GitHub account. One GitHub account feeds one org.

1. An owner or admin clicks **Connect GitHub** in the console. `POST /console/api/github/connect` answers `https://github.com/apps/<slug>/installations/new?state=<state>`. The state is 32 random bytes, kept in kv for 15 minutes, bound to the browser session (`Session.id_hash`) and the org, and usable once. GitHub [carries `state`](https://docs.github.com/en/apps/sharing-github-apps/sharing-your-github-app) through the install.
2. GitHub sends the person to the **setup URL**, `/console/oauth/github/setup?installation_id=…&setup_action=install&state=…`. GitHub [warns that `installation_id` can be spoofed](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/about-the-setup-url). So the route takes the state once and checks that the browser still carries the same session, still an owner or admin of the same org. It then sends the person through a GitHub sign-in.
3. **Verify:** `/console/oauth/github/verify` trades the sign-in code for a user token. `GET /user/installations` must list the installation, which proves the person in the browser can reach it on GitHub. The token is dropped at once. `GET /app/installations/{id}` (the app's JWT) names its account. The account `ExternalAccount github:<account id>` is claimed under a lock. The binding `(:GitHubInstallation {id, org, account_*, status, backfill_limit})-[:OF]->(:Org)` is written to the control graph, and the backfill is queued in the org.

Refusals are fixed redirects to `/console/github?error=`:

| Code | Meaning |
| --- | --- |
| `github_state` | The state is unknown, expired or spent, or the session changed |
| `github_denied` | The GitHub sign-in was declined |
| `github_not_yours` | The person who signed in cannot reach that installation |
| `account_claimed` | Another org reads that GitHub account |
| `github_failed` | Anything else |

Nothing GitHub says is echoed.

The connection page (`GET /console/api/github`) lists each installation, with its repos and their status and counts. **Sync now** is `POST /console/api/github/installations/{id}/sync`, optionally with `{"backfill": "all" | N}`. **Disconnect** is `DELETE /console/api/github/installations/{id}`: the binding goes `removing`, every repo it brought is deleted 500 nodes per job (GitHub accounts left with nothing go too), the account claim is released, and a `disconnected` tombstone keeps its webhooks dropped. The answer carries `uninstall_url`, the GitHub page where the company removes the app itself. Uninstalling on GitHub (`installation deleted`) does all of this and forgets the installation.

### The operator: any org, the legacy org included

With the admin token:

| Route | What it does |
| --- | --- |
| `GET /ops/github/installations` | Every installation of the app (asked of GitHub with the JWT), and the org each feeds |
| `POST /ops/github/installations/{id}/bind?org=<id>` | `{"backfill": "all"}`. Binds it to the org, default the legacy org, and starts the backfill. The admin token is the trust; the JWT proves the installation is this app's. |
| `POST /ops/github/installations/{id}/sync` | Sync now |
| `DELETE /ops/github/installations/{id}` | Disconnect |
| `POST /ops/github/connect?org=<id>` | An install link for that org. Whoever installs from it still has to pass the GitHub sign-in check. |

**The demo repo, `codesensei-tushar/graph_hacks`:** it is public, so it needs no installation (see "Public repos by name" below). Live webhooks instead of the 10-minute poll would need the app installed on the `codesensei-tushar` account. That is a user account, so only that user can install it: open the install link from `/ops/github/app`, choose the repo, then bind the installation to the legacy org with the bind route. The other way would be moving the repo into a GitHub organization the operator administers; that is not planned.

## Webhooks, per org

`POST /github/webhook` verifies the signature with the app's webhook secret (env, or the registered app's), dedupes on `X-GitHub-Delivery`, and routes by `installation.id` (`installations.route`). The work runs in a background task inside that org's context, so it writes that org's graph only.

| Installation | Goes to |
| --- | --- |
| Bound and active | Its org |
| Being removed, or disconnected | Dropped |
| Unknown, with a **registered** app | Dropped, until someone connects it |
| Unknown, with the app set **by env** (the legacy deployment) | The legacy org, bound on first sight, exactly as before there were orgs |

The last row has one exception: while a console Connect is in flight, a brand-new installation's `installation created` is looked at again 15 minutes later. That way a company's install cannot land in the legacy graph before its own callback binds it.

What each event does:
- `installation created`: queues the backfill.
- `installation deleted`: removes the installation's data and forgets it.
- `suspend` / `unsuspend`: marks the binding.
- `installation_repositories added`: backfills those repos. `removed`: removes their data.
- `repository deleted`: removes its data. Any other `repository` action upserts the repo; a visibility change moves its data between `org_shared` and `github_repo_<id>`.
- `member`: reads the collaborators again.
- Pull requests, reviews and issues: ingested as before.

A failed route lookup answers 503, so the delivery can be redelivered from **Advanced → Recent Deliveries**.

**The backfill** is the same stepped one public repos use: a page per scheduler job, rate limits waited out, `backfill_limit` deep (default 50; 0 = all). It is idempotent, so it is safe to run at least once. It lists the installation's repos, drops repos it no longer sees, and reads each repo's collaborators first. Collaborators are refreshed every hour in every org with an installation. Private repos keep `github_repo_<id>`, readable in a DM by linked collaborators. `link github` remembers the org the DM came from and links the person in that org's graph.

## Public repos by name (no app, no installation)

A public repo needs no GitHub App and no installation, because GitHub answers public data to anyone. The operator adds one to any org, the legacy org by default:

```bash
curl -X POST "https://YOUR-HOST/ops/github/public-repos" -H "Authorization: Bearer $BRAIN_ADMIN_TOKEN" \
     -H 'Content-Type: application/json' -d '{"repo": "codesensei-tushar/graph_hacks", "backfill": "all"}'
curl "https://YOUR-HOST/ops/github/public-repos" -H "Authorization: Bearer $BRAIN_ADMIN_TOKEN"     # progress
```

- `repo` is `owner/name` or its GitHub URL. `backfill` is `"all"` or a number of pull requests, newest first (default 50). Add `?org=<org id>` for an org other than the legacy one.
- `POST /ops/github/public-repos/<repo id>/sync` (optionally `{"backfill": ...}`) reads it again from the start. Use it, for example, after the seed's `Service.paths` change, so `TOUCHES` is recomputed. `DELETE /ops/github/public-repos/<repo id>` stops reading it and deletes what it brought in, 500 nodes per job.
- In the console, `/console/api/github/public-repos` makes the same calls on the session's org. Members can list; owners and admins can add, sync and remove.

What lands in the graph is what the app path writes, through the same `ingest` functions:
- pull requests with their author, reviews, requested reviewers, changed files (`TOUCHES` from `Service.paths`), `created_at`, `merged_at`, `closed_at` and the description summary
- issues as tickets
- `closes #N` and Jira key links

All of it is `org_shared`, so everyone in the org reads it. For the legacy org that includes the public ask page and MCP. No collaborators are read and no webhooks arrive. After the backfill, the repo is polled every 10 minutes for issues and pull requests updated since the last poll. If the repo turns private, or its name now points at another repo, reading stops and the status says so. A private repo is refused, because GitHub answers 404 for it.

**The backfill** reads one page per scheduler job: 100 issues, or 10 pull requests with their files and reviews. Each job is short, and a restart resumes where it stopped. Every write is an upsert, so a page read twice changes nothing. A rate limit (`Retry-After`, or `X-RateLimit-Remaining: 0` until `X-RateLimit-Reset`) puts the same page off until then, at most an hour. Any other failure backs off 1, 5, 15, then 60 minutes. Progress is on the `Repo` node (`bf_phase`, `bf_page`, `bf_pulls`, `bf_issues`, `bf_error`) and in the listing.

**Rate limit.** Set `GITHUB_PUBLIC_REPO_TOKEN` (an optional secret; `deploy.sh` passes it through). A fine-grained personal access token with read access to public repositories is enough, and it gets 5,000 requests an hour. Without it, reads are unauthenticated: 60 an hour ([GitHub's limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)). That still finishes, because every limit is waited out, but a repo of 220 pull requests then takes about 8 hours (3 requests per pull request). If GitHub refuses the token, it is dropped and reads carry on unauthenticated.

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
| `src/api/v1/console_github.py` | Connect GitHub: the console API, the setup and verify callbacks |
| `src/api/v1/github_installations.py` | The operator's installations: list, bind (any org), sync, disconnect |
| `src/domain/github/installations.py` | Installation -> org bindings in the control graph, and webhook routing |
| `src/domain/github/connections.py` | An installation's backfill, sync and disconnect in its org |
| `src/api/v1/github.py` | `POST /github/webhook` (signature, dedupe on `X-GitHub-Delivery`, background work) and `GET /github/oauth/callback` |
| `src/domain/github/webhooks.py` | What each event does |
| `src/domain/github/ingest.py` | Pull requests, reviews, and issues into the graph |
| `src/domain/github/backfill.py` | Install backfill, the stepped backfill (a page per job), the public-repo poll, batched removal, the hourly collaborator refresh |
| `src/domain/github/public_repos.py`, `src/api/v1/github_repos.py` | Public repos by name (ops and console) |
| `src/domain/github/repos.py` | Repo scopes and who may read them |
| `src/domain/github/account_link.py` | `link github` |
| `src/integrations/github/` | Signature check and the read-only REST client |
