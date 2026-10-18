# Console

The console (`/console`) lets a company sign in with Slack, create its own org with an invite
code, add the Slack bot to its workspace, and connect Jira and HubSpot with the vendor's OAuth
instead of pasting tokens. Each org reads and writes its own FalkorDB graph. Quillfern stays the
legacy org on graph `company`: the public site, the public ask page and MCP serve it as before.

This page is the operator's guide: turning the console on, the secrets, the vendor apps, key
rotation, and what to check when something fails. The connector framework itself is in
[CONNECTORS.md](CONNECTORS.md), and the per-vendor data in [JIRA.md](JIRA.md) and [HUBSPOT.md](HUBSPOT.md).

## Turn it on

Everything sits behind `CONSOLE_ENABLED`, which is off by default. While it is off, `/console`,
`/console/api/*`, `/console/oauth/*` and `/auth/*` answer 404, and production behaves as before.

1. **Encryption key first.** Connector tokens are never stored unencrypted: without a key, connecting a
   tool fails. Generate a Fernet key and store it:

   ```bash
   python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' \
     | gcloud secrets create TOKEN_ENCRYPTION_KEYS --data-file=- --configuration=graphhacks
   ```

   `TOKEN_ENCRYPTION_KEYS` is a comma-separated list: the first key encrypts and every key decrypts.
   The older single `TOKEN_ENCRYPTION_KEY` is still read, as the last key.
2. **The Slack sign-in and the vendor apps:** register them (below) and store their client ids and
   secrets.
3. **The admin token.** `BRAIN_ADMIN_TOKEN` must exist; it mints invite codes.
4. **The switch:**

   ```bash
   printf true | gcloud secrets create CONSOLE_ENABLED --data-file=- --configuration=graphhacks
   gh workflow run deploy.yml
   ```

5. **Invite a company.** Mint an invite code and send it:

   ```bash
   curl -s -X POST -H "Authorization: Bearer $BRAIN_ADMIN_TOKEN" https://brain.noobsurd.space/ops/invites
   ```

To turn the console off again, add a version of `CONSOLE_ENABLED` that is not `true` (or delete
the secret) and redeploy. Orgs, installs and their graphs are kept.

## Secrets

Each is a Secret Manager secret of the same name. `deploy.sh` writes each one that exists into
`/opt/brain/.env` (its `OPTIONAL_SECRETS` list). Create each value at a silent prompt, so it stays
out of shell history:

```bash
read -rs V && printf '%s' "$V" | gcloud secrets create JIRA_OAUTH_CLIENT_ID --data-file=- --configuration=graphhacks; unset V
```

| Secret | What it is |
| --- | --- |
| `CONSOLE_ENABLED` | `true` turns the console on |
| `TOKEN_ENCRYPTION_KEYS` | Fernet keys, comma-separated. The first one encrypts. |
| `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET` | The Slack app: sign in with Slack (OpenID Connect) and "Add to Slack" |
| `JIRA_OAUTH_CLIENT_ID`, `JIRA_OAUTH_CLIENT_SECRET` | The Atlassian OAuth 2.0 (3LO) app |
| `HUBSPOT_OAUTH_CLIENT_ID`, `HUBSPOT_OAUTH_CLIENT_SECRET` | The HubSpot developer-platform app |

The vendor app secrets are deliberately **not** named `CONNECTOR_*`. `registry.load` reads every
`CONNECTOR_<SOURCE>_<FIELD>` as a field of the legacy org's env install, so an OAuth client secret
named that way would turn into one. `PUBLIC_URL` (`https://brain.noobsurd.space`) must be set; the
callbacks are built from it.

## The vendor apps

All three are published as **Refactron**, with the privacy page at
`https://brain.noobsurd.space/privacy`.

### Slack sign-in

| Setting | Value |
| --- | --- |
| Redirect URL | `https://brain.noobsurd.space/auth/slack/callback` |
| User scopes (OpenID Connect) | `openid email profile` |

### Jira (Atlassian OAuth 2.0, 3LO)

In the [Atlassian developer console](https://developer.atlassian.com/console/myapps/), create an
OAuth 2.0 integration:

| Setting | Value |
| --- | --- |
| Authorization, OAuth 2.0 (3LO), Callback URL | `https://brain.noobsurd.space/console/oauth/jira/callback` |
| Permissions, Jira API (classic scopes) | `read:jira-work`, `read:jira-user`. `offline_access` is requested on the authorize URL. |
| Distribution | Turn **Enable sharing** on, or only the app's owner can consent |

Then copy its client id and secret into `JIRA_OAUTH_CLIENT_ID` and `JIRA_OAUTH_CLIENT_SECRET`.

How it is used:

- **Authorize.** The browser goes to `https://auth.atlassian.com/authorize` with `audience=api.atlassian.com`,
  `prompt=consent` and the scopes above, `offline_access` included so Atlassian issues a refresh token.
- **Token exchange.** The code is exchanged at `POST https://auth.atlassian.com/oauth/token`, which takes a
  JSON body. Access tokens last an hour.
- **Refresh tokens rotate.** Each refresh returns a new one and disables the old one, after a
  10-minute reuse interval. One that goes unused for 90 days expires.
- **Sites.** `GET https://api.atlassian.com/oauth/token/accessible-resources` lists the sites the consent
  covers. Only resources whose scopes include `read:jira-work` and whose `url` matches
  `^https://[a-z0-9][a-z0-9-]{0,62}\.atlassian\.net$` are offered. With several, the admin picks one
  (`/console/connect/jira/site`).
- **API calls.** Every call goes to `https://api.atlassian.com/ex/jira/<cloudId>/rest/api/3/...` with the
  bearer token. The site's own URL is used only to build links in answers.

### HubSpot (developer platform)

Create an app with the HubSpot CLI (`hs project create`). In its `app-hsmeta.json`:

| Setting | Value |
| --- | --- |
| `auth.type` | `oauth` |
| `auth.redirectUrls` | `["https://brain.noobsurd.space/console/oauth/hubspot/callback"]` |
| `auth.requiredScopes` | `crm.objects.companies.read`, `crm.objects.deals.read`, `crm.objects.owners.read`, `oauth` |
| `distribution` | `private` (allow-listed accounts) during the beta, or `marketplace` |

Copy the client id and secret from the app's **Auth** tab into `HUBSPOT_OAUTH_CLIENT_ID` and
`HUBSPOT_OAUTH_CLIENT_SECRET`.

How it is used:

- **Authorize.** The browser goes to `https://app.hubspot.com/oauth/authorize`.
- **Tokens.** The code is exchanged at `POST https://api.hubapi.com/oauth/2026-09/token`, form-encoded. The
  same endpoint refreshes. Access tokens last 30 minutes, and the refresh token is kept: HubSpot does
  not rotate it, and if it ever does, the new one is saved.
- **The portal.** It comes from the token's metadata (`POST /oauth/2026-09/token/introspect`, `hub_id`) and must
  match `hub_id` in the token response and `portalId` from `account-info`. On every credentials
  check, the portal the CRM answers for (the connector's `_portal()`) must still be that `hub_id`,
  or nothing is read.
- **Not the v1 endpoints.** `/oauth/v1/*` is deprecated on 16 February 2027 and is not used.

## Connecting a tool

An owner or admin clicks **Connect** in the console.

1. `POST /console/api/connectors/{source}/connect` (JSON, `X-CSRF-Token`, `Origin` equal to
   `PUBLIC_URL`) answers `{authorize_url}`.
   - Its `state` is 32 random bytes. kv keeps `console:oauth:<sha256(state)>` for ten minutes, bound to
     the session (the sha256 of its cookie), the org and the source.
   - An operator session (`BRAIN_ADMIN_TOKEN`) has no browser session to bind to and is refused.
2. The vendor redirects to `GET /console/oauth/{jira,hubspot}/callback`. The state is deleted as it is
   read. The callback goes on only when the browser still carries the same session, that session's
   org is the state's, and the person is still an owner or admin. Then:
   - **Code exchange.** The code is traded for tokens.
   - **The vendor account.** The callback finds the Jira site or the HubSpot portal.
   - **Claim.** It claims the account (`ExternalAccount 'jira:<cloudId>'` / `'hubspot:<hubId>'`) for the
     org. An account feeds one org: one another org holds is refused.
   - **Store.** It stores the `ConnectorInstall` (`auth: 'oauth'`, `status: 'connecting'`), with the
     tokens MultiFernet-encrypted in `secret_enc` and bound to the org and the install. Connecting an
     account the org already has replaces that install's tokens instead of making a second install.
3. The redirect goes to a fixed page:
   - `/console?connected=<source>` on success; the console then offers the next step (Jira: pick projects).
     A tool with nothing to pick (HubSpot reads the whole portal) starts syncing from the callback
     itself: the install is loaded and its first step armed at once. It shows `connecting` until that
     step works, then `active`.
   - `/console/connect/jira/site?id=<connection id>` when the Jira consent covers several sites.
   - `/console?error=<code>` on a failure.
   - Nothing from the vendor (an error description, a site name) is put in a URL or a page.
   - The redirect is `no-store`, and nginx logs paths without query strings.

| `error=` | Meaning |
| --- | --- |
| `oauth_denied` | The person declined on the vendor's consent page |
| `oauth_state` | The state is unknown, expired (10 min), already used or for another tool, or the session changed (signed out, signed in again, created an org, no longer an admin). Start again. |
| `account_claimed` | Another org already reads this Jira site or HubSpot portal |
| `oauth_failed` | Anything else: the code exchange or the account lookup failed, the consent covers no Jira site on atlassian.net, or the app's client id or secret is missing. The backend logs the failure type, never its text. |

## Teams

A new org's graph starts with its Company node (`{id: <org slug>, name: <org name>}`); every team
it imports or onboards from Slack joins it. `src/api/v1/console_teams.py`:

- `POST /console/api/teams/import` (owners and admins; JSON, CSRF): `{"file": "<team file TOML>", "check": false}`.
  The file is the one `/ops/import` takes (docs/ONBOARDING-TEAMS.md), with the same 256 KiB limit and
  the same checks against the org's graph. `check: true` writes nothing.
  - 200 check: `{mode: "check", company, team, contents, counts: {people, runs: {<kind>: n}, decisions}}`
  - 200 merge: the same plus `mode: "merge"`, `nodes`, `nodes_created`, `edges_created`, `properties_set`
  - 400 `{error: "refused", problems: [...]}` (nothing written); 413 `{error: "too_large", limit_bytes}`
- `GET /console/api/teams` (members): `{teams: [{id, name, summary, members: [{id, name, role}],
  runs: [{id, name, kind, owners: [{id, name}]}]}]}`, what the member could read in a Slack DM.

## People

Jira often hides emails, so many synced accounts link to no person. `src/api/v1/console_people.py`
(owners and admins) lets the org link them; this replaces `CONNECTOR_*_PEOPLE` for console orgs.

- `GET /console/api/people/unlinked`: `{unlinked: [{id, name, source: "jira"|"hubspot", items,
  suggestions: [{person_id, name, reason: "same_name"|"same_email_domain"}]}], people: [{id, name}]}`.
  At most 500 accounts, most active first; at most 5 suggestions each; public mail domains suggest
  nobody. No email or domain is ever returned.
- `POST /console/api/people/link` (JSON, CSRF) `{external_user_id, person_id}`: writes
  `(:ExternalUser)-[:IS {via: "admin", by, at}]->(:Person)` and moves the account's OWNS/WORKED_ON
  onto the person. Audited as `people.link`. 200 `{ok, external_user_id, person: {id, name}, changed}`;
  404 unknown account or person (in this org); 409 when the account is already linked by email.
  An admin link can be corrected by linking again. Like an email link, it lets the person read, in a
  DM, the restricted Jira projects the account can read.

## Tokens at run time

Connectors get their bearer token from `src/connectors/oauth.py`'s `bearer_for(install)`:

1. **Cached.** A token cached in this process with more than five minutes left is used.
2. **Stored.** Otherwise the stored secret is read again, since another worker may have refreshed it.
3. **Refresh.** Otherwise the worker takes the kv lock `SET NX connector:refresh:<org>:<source>:<install>`
   (30 s, released only by its holder) and reads once more.
   - It spends the refresh token and writes the new secret, with the rotated refresh token, back to
     the store **before** it uses the new access token.
   - If that write fails, the new token is not used and the step fails. The next step spends the old
     refresh token again, which Atlassian accepts within its reuse interval.
   - A worker that finds the lock taken waits up to ten seconds for the holder's write, then gives
     the step back (`RetryLater`).

A refresh the vendor refuses (`invalid_grant`: revoked, expired, or the password changed) raises
`CredentialsRejected`. The sync records "reconnect it" as the install's last error and retries
hourly. The fix is **Reconnect** in the console, which replaces the tokens of the same install.
Tokens, codes and client secrets never reach a log line, an error message or a route's answer.
Logs show `[oauth] <install> token refreshed; rotated=True` and
`[oauth] <install> refresh refused (invalid_grant)`.

## Key rotation

1. Generate a new key and put it **first**:
   `TOKEN_ENCRYPTION_KEYS=<new>,<old>` (a new secret version). Redeploy. New writes use the new key;
   old values still decrypt.
2. Re-wrap what is stored under the new key, Slack bot tokens included. This also encrypts any legacy
   `plain:` Slack token:

   ```bash
   curl -s -X POST -H "Authorization: Bearer $BRAIN_ADMIN_TOKEN" https://brain.noobsurd.space/ops/tokens/rewrap
   ```

3. Drop the old key (`TOKEN_ENCRYPTION_KEYS=<new>`) and redeploy.

Refreshes write with the first key too, so active installs move to it on their own within an hour.

## Troubleshooting

**Sign-in says "Company Brain cannot read this workspace's Slack connection: its encryption key
changed"** (`/signin?error=workspace_key`). The workspace's stored bot token cannot be decrypted
with any key in `TOKEN_ENCRYPTION_KEYS`: the old key was dropped (step 3 above) before the rewrap
reached it. Until it is fixed nobody in that workspace can sign in and the bot cannot answer there.
The backend logs, once per attempt:

```
[console] the stored Slack bot token of team=T... cannot be decrypted (InvalidToken): TOKEN_ENCRYPTION_KEYS no longer holds the key it was encrypted with. ...
```

Fix it either way:

- Put the old key back as a second key (`TOKEN_ENCRYPTION_KEYS=<new>,<old>`), redeploy, run the
  rewrap (step 2 above), and only then drop it again.
- Or, if the old key is gone, have a workspace member reinstall the Slack app from the console
  (setup step 2): the install stores a fresh token under the current key.

A transient failure (Slack or the graph unreachable) is `workspace_unavailable` instead, and goes
away on a retry.

## nginx

`deploy/nginx/https.conf` and `nginx.conf` add, per client address:

| Path | Limit | Headers |
| --- | --- | --- |
| `/auth/` | 10 a minute, burst 10 (zone `auth`) | console headers |
| `/console/oauth/` (the vendor callbacks) | 10 a minute, burst 10 (zone `auth`) | console headers |
| `/console/api/` | 10 a second, burst 40 (zone `console_api`) | console headers |
| `/console`, `/console/`, `/signin` | — | console headers |

The console headers are in `console-headers.conf`:

- `Content-Security-Policy: default-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self' https://slack.com`
- `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`
- tls.conf's HSTS and `nosniff`, repeated because an `add_header` in a location replaces the server's

Over the limit, nginx answers 429.

## Disconnecting

In the console, an admin types the site's name to confirm. The install goes to `removing` and its
sync stops. What it wrote is deleted from the org's graph in batches. Then its state, the install
and the `ExternalAccount` are deleted, so another org may claim the account. An audit entry is
written. The vendor-side grant stays until the customer removes the app:

- Atlassian: their account's **Connected apps**.
- HubSpot: **Connected apps** in the portal's settings.

## Vendor documentation

Checked on 18 October 2026:

- Atlassian OAuth 2.0 (3LO) apps: <https://developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/>
- HubSpot OAuth tokens (2026-09): <https://developers.hubspot.com/docs/api-reference/latest/authentication/manage-oauth-tokens>
- HubSpot token introspection: <https://developers.hubspot.com/docs/api-reference/latest/authentication/oauth-tokens/token-introspect>
- HubSpot v1 OAuth migration: <https://developers.hubspot.com/docs/api-reference/legacy/authentication/oauth-tokens/v1/migration-guide>
- HubSpot developer-platform apps: <https://developers.hubspot.com/docs/apps/developer-platform/build-apps/create-an-app>

## Code

| Path | Role |
| --- | --- |
| `src/connectors/oauth.py` | Token exchange and refresh, and `bearer_for` with its kv lock |
| `src/connectors/jira/oauth.py` | The Atlassian `OAuthSpec`, `accessible-resources`, and site checks and choice |
| `src/connectors/hubspot/oauth.py` | The HubSpot `OAuthSpec`, token metadata, and the portal check |
| `src/api/v1/console_oauth.py` | The connect route and the vendor callbacks |
| `src/connectors/store.py` | Stored installs and their encrypted secrets |
| `deploy/nginx/console-headers.conf` | The CSP and other console headers |
| `tests/integration/test_console_oauth.py` | End to end, against fake Atlassian and HubSpot servers (`tests/fixtures/oauth_servers.py`) |
