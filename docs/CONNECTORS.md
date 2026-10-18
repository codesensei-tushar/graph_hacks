# Connectors

A connector brings one external source (Jira first, then HubSpot) into the company graph. It
only reads the source and returns normalised records; the framework in `src/connectors/`
writes them, schedules the sync, takes signed webhooks, and keeps who may read what.

## Contract

The contract is `src/connectors/base.py`. It takes additive changes only.

**A connector** subclasses `Connector`, sets `source` (`"jira"`), `required` (the env fields
that must be set), `poll_seconds` and `accepts_webhooks`, and calls
`registry.register(TheConnector())` when its module is imported (`src/connectors/<source>/__init__.py`;
`registry.KNOWN_MODULES` lists the modules). Its methods:

| Method | Returns | Notes |
| --- | --- | --- |
| `check_credentials(install)` | `InstallInfo(account, site)` | One cheap call. Stored on the state node, so nothing secret. |
| `containers(install)` | `list[Container]` | The allow-list. A container that drops off it is removed with its items. |
| `fetch(install, container, cursor)` | `Page` | One page. `cursor` is None on the backfill; fetch newest first then. |
| `readers(install, container)` | `list[ExternalUser] \| None` | Restricted containers only. None means nobody (fails closed). |
| `verify_webhook(install, request)` | `bool` | False answers 401 and nothing runs. |
| `parse_webhook(install, request)` | `Page \| None` | May call the source for the full record. Cursors are not touched. |

Raise `RetryLater(seconds)` to wait, `CredentialsRejected` on a 401, any `ConnectorError`
otherwise. `src/connectors/http.py`'s `JsonClient(base_url, headers)` does this already: it
retries short 429/503 waits and 5xx in place, raises `RetryLater` for long waits, never follows
a redirect (it would resend the credentials), never sends the headers to another host, and never
logs them. No wait is honoured past an hour (`base.MAX_RETRY_AFTER_SECONDS`), whoever asks for it. `basic_auth(email, token)` and `bearer(token)`
build the headers.

**The install.** `Install(source, id, config)`. In v1 it comes from environment variables
`CONNECTOR_<SOURCE>_<FIELD>`; `config` maps each field, lowercased, to its value
(`CONNECTOR_JIRA_API_TOKEN` is `config["api_token"]`). Secrets stay in memory: they are never
written to the graph, logged, or returned, and `repr(install)` leaves `config` out. Build every
id with the install: `install.item_id(ext)` is `<source>:<install>:<ext>`, `install.user_id(ext)`
and `install.container_id(ext)` add `user:` and `container:`. The graph writer refuses ids from
outside the install, so a connector cannot touch a seeded or team-imported node.

**Records** (`Page` holds `items`, `users`, `edges`, `deleted_ids`, `next_cursor`, `has_more`, `retry_after`, `poll_cursor`):

| Record | Written as |
| --- | --- |
| `Container(id, name, external_id, url, kind, restricted)` | `(:Container)`. Scope `org_internal`, or its own scope when `restricted`. |
| `Item(label, id, external_id, container, name, title, url, state, status, updated_at, summary, kind, about, props)` | `(:Ticket\|Service\|Document)-[:IN_CONTAINER]->(:Container)`, scope from the container. `name` is the short handle (`PAY-12`), `title` the full text. `state` is `open` or `closed`, the raw value goes in `status`. A CRM company is `Service` with `kind='account'`. |
| `ExternalUser(id, source, external_id, name, email)` | `(:ExternalUser)-[:IS]->(:Person)` when exactly one Person has the email. Connectors never create a Person. |
| `Edge(rel, from_id, to_id, props)` | `OPENED`, `ASSIGNED`, `OWNS`, `WORKED_ON`, `ABOUT` (within the install) or `BLOCKS`. `OWNS` and `WORKED_ON` from a linked user are written from its Person. |
| `Item.about` hints | `(item)-[:ABOUT]->(:Service)`: each hint (a component, a label) resolved strictly against the graph's services, else the best guess over all hints. |

Every item node gets `id`, `name`, `title`, `summary` (clipped to 2000 characters), `url`,
`state`, `status`, `source_updated_at`, `external_id`, `source`, `install`, `scope` and
`synced_at` (ms), then `props` under any name not already taken.

**Edges belong to an item**: the `from_id` item, else the `to_id` item. Writing an item
replaces all of its edges, so a page carries every edge of each item in it, and an edge between
two items comes once, with the item it points out of (`PAY-13 BLOCKS PAY-12` comes with
PAY-13). An edge whose item is not in the page is only added.

**Cursors.** `next_cursor` is stored per container exactly as returned and handed to the next
`fetch`; None starts the next fetch over. `has_more` fetches the next page on the next
scheduler step; otherwise the next fetch is `poll_seconds` away. `retry_after` is the least wait
before the next fetch either way. A container keeps at most `sync.MAX_ITEMS_PER_CONTAINER`
items; past that, new items are dropped and existing ones still update. Reaching it ends the
backfill, and the next fetch waits for the next poll. Set `poll_cursor` on every backfill page to
where incremental polling should resume (the newest update the backfill has seen, e.g.
`updated >= 2026-10-05 10:00`): at the cap the container's cursor becomes the latest
`poll_cursor` given, so the items it kept stay current. Without one, the backfill cursor is kept
and each poll reads the next older page, all of it dropped, until the backfill is walked.

**Pull requests.** `graph.link_references(pull_request_id, handles, source, rel="REFERENCES")`
links a pull request `-[:REFERENCES]->` (or, with `rel="CLOSES"`, `-[:CLOSES]->`) the tickets
of `source` named by `handles` (`["PAY-12"]`, any case), replacing that pull request's earlier
links of that source and relationship. The handles are kept on the pull request
(`ticket_references`, `ticket_closes`), so a ticket synced later links back when it is written.

**Reserved properties.** `props` may not use a name graph.py writes itself, nor `number`:
`who_should_know` looks tickets and pull requests up by `#12`, and a Jira ticket must not answer
for a GitHub issue. `Edge.props` may not use `item`, `source`, `install`, `via`, `user` or
`state`, which graph.py writes on every edge (or GitHub's reviews use). A refused name is logged
and dropped.

**How answers read it.** An unlinked account is named with its tool ("Yash Patil (HubSpot)") in
`who_owns`, `who_knows` and paths, so it is never mistaken for a person the company knows by the
same name. Ticket work counts once per person in `who_knows`, so many tickets never outrank
owning the thing. A team's page and brief list the accounts its members own (scope-filtered,
the first ten and a count). No `RUNS` edge is made from connector data.

**Scopes.** Connector data is `org_internal`: every Slack asker reads it, in channels and DMs.
The public ask page, the team pages and MCP read `org_shared` only, so they never see it.

## Who reads connector data

| Surface | Reads | Sees connector data |
| --- | --- | --- |
| Slack, any channel or DM | `org_shared`, `org_internal`, the room's own scope; a DM adds the asker's private channels, private repos and restricted projects | yes |
| Public ask page (`/`, `/api/ask`), team pages, graph overview, `/api/proof` | `org_shared` | no |
| MCP (`/mcp`) | `org_shared` | no |

`org_internal` means "everyone in the Slack workspace". A Slack Connect channel or a guest in a
public channel can therefore read it; sync only projects everyone in the workspace may see, or
mark a project `restricted`. A restricted container gets its own scope
(`connector_<hash of its id>`), and only a DM whose asker's `SlackUser -[:IS]-> Person <-[:IS]-
ExternalUser` is one of its `readers()` reads it. With readers unknown, nobody does.

## How it runs

- **Startup.** `registry.load` reads every `CONNECTOR_*` variable, imports the modules of the
  sources that have any set, and configures each source whose `required` fields are all set
  (a missing one is logged by name, never by value). `sync.schedule_all` arms each install's job.
- **Sync** (`src/connectors/sync.py`, scheduler job `runConnectorSync`, id `connector:sync:<source>`).
  Each step fetches one page. Credentials are checked and containers listed hourly and after
  a restart; a container that disappears is deleted with its items. Then one page for the
  due container fetched least recently, so backfills and polls of different containers take
  turns. The cursor,
  due time, counts and last error live on a `ConnectorState` node per container and one per
  install (scope `system`, which no read set holds). The job is re-armed in `finally`.
  `RetryLater` waits as asked, up to an hour, without moving the cursor; a 401 waits an hour; other failures
  wait 1, 5, 15, then 60 minutes, per container, so one broken project does not hold up the
  rest. A container keeps at most 1000 items. No step is put off past an hour (or the poll
  interval, if longer); at startup a job parked further out is re-armed, and a container whose
  due time is further out than that is due now.
- **Webhooks.** `POST /connectors/<source>/webhook`: 404 unless the source is configured and
  accepts webhooks, 413 over 1 MB, 401 unless `verify_webhook` passes, then answered at once.
  The page is written in the background. A delivery is identified by its body's SHA-256
  (`connector:delivery:*`, kept 24 h) and marked seen only after it ran.
- **Writes** (`src/connectors/graph.py`) MERGE on `id` only, so a page written twice changes
  nothing, and refuse any id outside the install.

## Installs made in the console

With `CONSOLE_ENABLED`, an org's admins connect a source from the console instead of env
variables. The install is a `(:ConnectorInstall {id: '<source>:<install>', org, auth, status,
external_account, site, config_json, secret_enc, key_id})` node in the org's own graph
(`src/connectors/store.py`): `config_json` holds the selected projects and the site,
`secret_enc` the credentials, MultiFernet-encrypted (`TOKEN_ENCRYPTION_KEYS`) over
`{org, install, ...}` and refused if copied to another install or org. The registry is keyed
`(org, source, install)`; `registry.load_from_graph(org)` loads each org's installs at startup.
Each install has its own sync job, `connector:sync:<org>:<source>:<install>`, run in its org;
the legacy org's env install keeps `connector:sync:<source>`. An external account
(`jira:<cloudId>`, `hubspot:<hubId>`) feeds one org only.

Disconnecting (`DELETE /console/api/connections/{id}`) marks the install `removing`, stops its
job, deletes everything it wrote 500 nodes at a time (a scheduler job, so a restart resumes it),
then the install node and its external account (unless another install of the org still holds
it), and writes an audit entry. Env installs can only be turned off in the environment, as below.

Data whose install is configured nowhere any more (its env variables were removed) keeps
answering until it is purged. `GET /ops/connectors` lists such installs under `orphaned`, and
`DELETE /ops/connectors/orphans/<source>:<install>` deletes their data 500 nodes at a time
(`?org=<id>` for another org than the legacy one).

## Operate it

Both routes need `BRAIN_ADMIN_TOKEN`:

```bash
# Accounts, containers, item counts, cursors, last errors. Never a configured value.
curl -s -H "Authorization: Bearer $BRAIN_ADMIN_TOKEN" https://brain.noobsurd.space/ops/connectors
# Fetch now; full=true starts every container over (a re-backfill; writes are idempotent).
curl -s -X POST -H "Authorization: Bearer $BRAIN_ADMIN_TOKEN" "https://brain.noobsurd.space/ops/connectors/jira/sync?full=true"
```

Logs: `[connectors] synced container=... items=... more=...`, `[connectors] <source> delivery ...`,
`[connectors] ... credentials rejected`.

## Production

Each field is one Secret Manager secret named like its variable. `deploy.sh` writes every one
that exists into `/opt/brain/.env`, and the backend reads it from there. It always tries the
names in its `CONNECTOR_SECRETS` list (the Jira and HubSpot fields). Any other `CONNECTOR_` secret
is picked up only if the VM is allowed to list secrets (step 3). Values may not contain a
newline or a single quote. Every `gcloud` command uses `--configuration=graphhacks`, never
the default configuration.

1. Create one secret per field, typing each value at the silent prompt so it stays out of
   shell history (Jira shown; see [JIRA.md](JIRA.md) for where each value comes from):

   ```bash
   read -rs V && printf '%s' "$V" | gcloud secrets create CONNECTOR_JIRA_BASE_URL --data-file=- --configuration=graphhacks; unset V
   read -rs V && printf '%s' "$V" | gcloud secrets create CONNECTOR_JIRA_EMAIL --data-file=- --configuration=graphhacks; unset V
   read -rs V && printf '%s' "$V" | gcloud secrets create CONNECTOR_JIRA_API_TOKEN --data-file=- --configuration=graphhacks; unset V
   read -rs V && printf '%s' "$V" | gcloud secrets create CONNECTOR_JIRA_PROJECTS --data-file=- --configuration=graphhacks; unset V
   # Only if Jira will send webhooks:
   read -rs V && printf '%s' "$V" | gcloud secrets create CONNECTOR_JIRA_WEBHOOK_SECRET --data-file=- --configuration=graphhacks; unset V
   ```

2. Make sure `BRAIN_ADMIN_TOKEN` exists (it gates the status route). Then redeploy, and check:

   ```bash
   gh workflow run deploy.yml
   curl -s -H "Authorization: Bearer $BRAIN_ADMIN_TOKEN" https://brain.noobsurd.space/ops/connectors
   ```

   The status names the account the token acts as and each project's item count. An empty
   list means a required field is missing. The backend log names the missing variable.

3. Optional, once: let the VM list secret names (not read more values) so a new
   connector's `CONNECTOR_` secrets are picked up without editing `deploy.sh`:

   ```bash
   gcloud projects add-iam-policy-binding graphhacks-brain-8490 \
     --member=serviceAccount:brain-vm-sa@graphhacks-brain-8490.iam.gserviceaccount.com \
     --role=roles/secretmanager.viewer --condition=None --configuration=graphhacks
   ```

To rotate a value, add a version and redeploy:

```bash
read -rs V && printf '%s' "$V" | gcloud secrets versions add CONNECTOR_JIRA_API_TOKEN --data-file=- --configuration=graphhacks; unset V
gh workflow run deploy.yml
```

To turn a source off, delete one of its required secrets and redeploy: the sync stops, and
what it wrote stays in the graph (still `org_internal`). To remove that too, run this in the
graph browser (every node a connector writes carries its install):

```cypher
MATCH (n) WHERE n.install = 'jira:default' DETACH DELETE n
```

Dropping one project from `CONNECTOR_JIRA_PROJECTS` and redeploying deletes that project's
items at the next container listing.

## Adding a connector

1. `src/connectors/<source>/__init__.py` defines the `Connector` subclass and calls `registry.register`.
2. Add the module to `registry.KNOWN_MODULES`, and its secret names to `CONNECTOR_SECRETS` in
   `deploy/deploy.sh` (unless the VM may list secrets).
3. Tests drive `sync.sync_step(source)` against a fake source, as `tests/integration/test_connectors.py` does.
