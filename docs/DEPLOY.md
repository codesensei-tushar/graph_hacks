# Deploy

Production runs on one GCP VM with Docker Compose. Every push to `main` is checked, built,
deployed by git SHA, health-checked, and rolled back automatically if the new version
doesn't come up healthy.

| | |
| --- | --- |
| App | https://brain.noobsurd.space (health: [`/healthz`](https://brain.noobsurd.space/healthz)) |
| Slack | Events `https://brain.noobsurd.space/slack/events`, interactivity `https://brain.noobsurd.space/slack/interactions` |
| Graph browser | https://gdb.noobsurd.space (two logins, see [gdb](#the-graph-browser-gdb)) |
| GCP project | `graphhacks-brain-8490` (org wattcycle.ai, account admin@wattcycle.ai) |
| VM | `brain-vm`, `asia-south1-c`, e2-medium, Ubuntu 24.04, static IP `35.200.189.204` |
| Images | `asia-south1-docker.pkg.dev/graphhacks-brain-8490/brain/backend:<git sha>` |

Every `gcloud` command here uses the `graphhacks` configuration, which holds the account and
project. Never `gcloud config configurations activate` it on a shared machine; pass
`--configuration=graphhacks` instead, so other projects' defaults stay untouched.

## Architecture

```
  Slack servers ───────┐                     you, in a browser ───────────┐
  (signed POSTs)       │                     (basic auth, then the        │
                       ▼                      FalkorDB password)          ▼
        brain.noobsurd.space                             gdb.noobsurd.space
                       │      A records, DNS only (no Cloudflare proxy)   │
                       └───────────► 35.200.189.204 ◄─────────────────────┘
                                     brain-vm firewall: tcp 80, 443 from anywhere;
                                     tcp 22 from IAP (35.235.240.0/20) only
  ┌──────────────────────────────────────────────────────────────────────────────┐
  │ Docker Compose project "brain" (/opt/brain)                                  │
  │                                                                              │
  │   nginx :80 :443 ── the only published ports                                 │
  │     │  TLS 1.2+, :80 → https, ACME challenges from the certbot webroot       │
  │     ├── brain.* ─────────────► backend :8000 (FastAPI, src/)                 │
  │     │                          /slack/*, /healthz, /api/*, /mcp (public)     │
  │     └── gdb.* (basic auth, ──► falkordb-browser :3000                        │
  │          rate limited)                 │                                     │
  │                       backend ──► falkordb :6379 ◄──┘  (requirepass, AOF,    │
  │                                                         volume falkordb-data)│
  │   certbot: idles; issues and renews into the letsencrypt volume              │
  └──────────────────────────────────────────────────────────────────────────────┘

  GitHub Actions ──(OIDC → Workload Identity Federation)──► brain-deployer
      builds and pushes the image, then IAP SSH → sudo /opt/brain/deploy.sh <sha>
```

Ports 6379 (FalkorDB) and 3000 (browser) are never published, not even on the VM's
loopback. They exist only on the Compose network.

## Files

| Path | What it is |
| --- | --- |
| `Dockerfile`, `.dockerignore` | Backend image: multi-stage, uv, non-root, `GIT_SHA` build arg |
| `deploy/docker-compose.yml` | The production stack, pinned images |
| `deploy/nginx/` | `nginx.conf`, `http.conf` (pre-TLS), `https.conf`, shared TLS and proxy snippets |
| `deploy/deploy.sh` | Deploy one SHA, health-check, roll back. Lives at `/opt/brain/deploy.sh` |
| `deploy/bootstrap-tls.sh` | First certificate and the switch to HTTPS (repeatable) |
| `deploy/systemd/` | Twice-daily renewal timer, nightly FalkorDB backup timer |
| `deploy/backup/` | `falkordb-backup.py` (nightly backup, restore check) and the bucket's `lifecycle.json` |
| `deploy/vm/startup.sh` | VM startup script: Docker, log caps, swap, registry credential helper |
| `deploy/slack-app-manifest.yaml` | Manifest for the production Slack app |
| `.github/workflows/ci.yml` | ruff, pytest (with a FalkorDB service), docker build |
| `.github/workflows/deploy.yml` | CI, then build, push, deploy, verify |
| `src/api/ops.py` | `/healthz`, `POST /ops/seed`, `POST /ops/import`, the Slack event clock |
| `src/api/mcp.py` | `/mcp`: the read-only tools over MCP, public and rate-limited |
| `src/api/v1/connectors.py` | `/connectors/<source>/webhook`, `/ops/connectors` and its sync-now route (admin token) |

## How a deploy works

1. A push to `main` (or **Run workflow** on Deploy, or `gh workflow run deploy.yml`) starts
   `deploy.yml`. Its first job is the whole of `ci.yml`: ruff, pytest against a FalkorDB
   service container, and a docker build. Nothing deploys unless that passes.
2. The deploy job (one at a time, concurrency group `deploy`) authenticates to GCP through
   Workload Identity Federation as `brain-deployer`. No key file exists anywhere.
3. It builds the image and pushes `backend:<full git sha>`. Tags are immutable; a re-run for
   the same commit reuses the image. Nothing is ever tagged or deployed as `:latest`.
4. It copies the commit's `deploy/` directory to `/opt/brain/releases/<sha>/` over IAP SSH,
   then runs `sudo /opt/brain/deploy.sh <sha>`, which:
   - copies that release's compose file, nginx config and renewal units into place,
   - reads the latest version of each secret and writes `/opt/brain/.env` and the gdb
     htpasswd (root-only),
   - `docker compose pull` and `up -d`, then `nginx -t` and a reload,
   - polls `https://brain.noobsurd.space/healthz` (pinned to the VM itself) until it reports
     the new `git_sha` with `"falkordb": "ok"`, for up to 120 s.
5. Success: the SHA goes into `/opt/brain/last_good_sha`, and old releases (beyond 10) and
   old images are pruned. Failure: `deploy.sh` redeploys `last_good_sha` the same way, then
   exits non-zero, so the workflow goes red.
6. The workflow checks the public `/healthz` from outside as well.

`deploy.sh` never touches `/etc/letsencrypt`, so a redeploy can't trigger certificate
issuance.

## Rollback

Automatic: see step 5. To roll back by hand, prefer `git revert <sha> && git push`, so the
fix goes through CI. In an emergency, any release still on the VM (the last 10 plus the last
good one) can be redeployed directly:

```bash
gcloud compute ssh brain-vm --zone asia-south1-c --tunnel-through-iap --configuration=graphhacks \
  --command 'ls -t /opt/brain/releases; cat /opt/brain/last_good_sha'
gcloud compute ssh brain-vm --zone asia-south1-c --tunnel-through-iap --configuration=graphhacks \
  --command 'sudo /opt/brain/deploy.sh <older full sha>'
```

## Secrets

All secrets are in Secret Manager in `graphhacks-brain-8490`, and only there. The VM reads
them on each deploy; GitHub holds none (its Actions variables are non-secret identifiers).

| Secret | Used for |
| --- | --- |
| `SLACK_SIGNING_SECRET` | Verifies Slack requests |
| `SLACK_BOT_TOKEN` | The bot's `xoxb-` token |
| `MODEL_API_KEY` | Anthropic API key, given to the app as `LLM_API_KEY` (and `MODEL_API_KEY`) |
| `FALKORDB_PASSWORD` | FalkorDB `requirepass`; the backend and the browser login use it |
| `GDB_BASIC_AUTH_PASSWORD` | nginx basic auth on gdb, user `admin` |
| `NEXTAUTH_SECRET` | Signs the browser's session cookies |

The first three start as the placeholder `REPLACE_ME`. Until they're replaced, `deploy.sh`
writes an empty bot token and model key, and a random signing secret, so the app boots, says
"Slack is not configured", and rejects every Slack request.

Optional secrets, written to `.env` only if you create them: `SLACK_OWNER_ID`,
`SLACK_ADMIN_IDS`, `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `BRAIN_ADMIN_TOKEN` (gates `/slack/status`, `/api/proof` and `POST /ops/seed`), `MCP_TOKEN` (lifts `/mcp`'s per-address rate limit; `/mcp` is public without it),
`TOKEN_ENCRYPTION_KEY`, `LLM_MODEL` (see [SLACK.md](SLACK.md) for what each does), and the GitHub App's
`GITHUB_APP_ID`, `GITHUB_WEBHOOK_SECRET`, `GITHUB_PRIVATE_KEY`, `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`
(see [GITHUB.md](GITHUB.md); the private key is stored on one line), and the org console's
`CONSOLE_ENABLED`, `TOKEN_ENCRYPTION_KEYS`, `JIRA_OAUTH_CLIENT_ID`, `JIRA_OAUTH_CLIENT_SECRET`,
`HUBSPOT_OAUTH_CLIENT_ID` and `HUBSPOT_OAUTH_CLIENT_SECRET` (see [CONSOLE.md](CONSOLE.md)). Connector settings are
secrets named `CONNECTOR_<SOURCE>_<FIELD>` (`CONNECTOR_JIRA_API_TOKEN`, ...): `deploy.sh` writes the
names in its `CONNECTOR_SECRETS` list that exist, plus any other `CONNECTOR_` secret when the VM may
list secrets; see [CONNECTORS.md](CONNECTORS.md#production).

**Add or rotate a value** without it landing in shell history, then redeploy:

```bash
read -rs V && printf '%s' "$V" | gcloud secrets versions add SLACK_BOT_TOKEN --data-file=- --configuration=graphhacks; unset V
gh workflow run deploy.yml
```

To create an optional secret, run `gcloud secrets create NAME --data-file=- --configuration=graphhacks`
with the same `read -rs` pipe.

Rotation notes:
- `FALKORDB_PASSWORD`: the redeploy recreates the FalkorDB container with the new
  password, and the data volume is kept. Generate one with `openssl rand -hex 32 | gcloud secrets versions add FALKORDB_PASSWORD --data-file=- --configuration=graphhacks`.
- `NEXTAUTH_SECRET`: signs everyone out of the browser.
- `GDB_BASIC_AUTH_PASSWORD`: the htpasswd is rewritten on every deploy.
- Values can't contain a single quote or a newline. `deploy.sh` refuses them, because they
  would break the `.env` quoting.

## The graph browser (gdb)

Open https://gdb.noobsurd.space. Two locks:

1. **Basic auth** (nginx): user `admin`, password from
   ```bash
   gcloud secrets versions access latest --secret=GDB_BASIC_AUTH_PASSWORD --configuration=graphhacks
   ```
2. **FalkorDB login** (the browser's form): host `falkordb`, port `6379`, username `default`
   (or empty), password from
   ```bash
   gcloud secrets versions access latest --secret=FALKORDB_PASSWORD --configuration=graphhacks
   ```
   The host and port are prefilled. The browser connects from inside the Compose network,
   which is why the host is `falkordb` and not the public name.

Once logged in you have full read and write access to every graph. The app's graph is
`company`.

nginx rate-limits gdb per client IP: 10 requests/s with a burst of 200 for the page,
and 6/min with a burst of 5 on the sign-in endpoint (`/api/auth/callback/`).

The htpasswd is mode 0640, group 101: that's the nginx user in the container. On the VM host,
gid 101 is the `messagebus` system group. It holds only an apr1 hash of a 64-character
random password.

## Local access to production FalkorDB

FalkorDB has no published port, so forward to its container address over SSH through IAP:

```bash
IP=$(gcloud compute ssh brain-vm --zone asia-south1-c --tunnel-through-iap --configuration=graphhacks \
  --command "sudo docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' brain-falkordb-1" | tail -1)
gcloud compute ssh brain-vm --zone asia-south1-c --tunnel-through-iap --configuration=graphhacks \
  -- -N -L 127.0.0.1:16379:$IP:6379
```

Leave that running. In another terminal, `127.0.0.1:16379` is production FalkorDB with
`FALKORDB_PASSWORD`, e.g. `FALKORDB_PORT=16379 FALKORDB_PASSWORD=... python scripts/check_graph.py`.
Be careful: this is the live graph. The container IP changes when the container is
recreated, so look it up again after a deploy.

## Certificates

- One Let's Encrypt certificate, named `brain`, covers both names; it's in the
  `brain_letsencrypt` volume.
- First issuance: `sudo /opt/brain/bootstrap-tls.sh` on the VM. It checks that both
  names resolve to the VM, serves the HTTP-only site, does a staging `--dry-run`, then
  the real `certonly --webroot`, and switches nginx to `https.conf`. It's safe to re-run.
- Renewal: `brain-certbot-renew.timer` runs at 00:00 and 12:00 UTC (plus up to 1 h jitter).
  It runs `certbot renew` (a no-op until 30 days before expiry), and on success reloads
  nginx.
- Check: `sudo docker compose -f /opt/brain/docker-compose.yml exec certbot certbot certificates`,
  `sudo docker compose -f /opt/brain/docker-compose.yml exec certbot certbot renew --dry-run`,
  `systemctl list-timers brain-certbot-renew.timer`, `journalctl -u brain-certbot-renew`.

## Operations

```bash
# Shell on the VM (OS Login over IAP; no public SSH)
gcloud compute ssh brain-vm --zone asia-south1-c --tunnel-through-iap --configuration=graphhacks

# On the VM
cd /opt/brain
sudo docker compose ps
sudo docker compose logs --tail 100 backend
sudo cat last_good_sha
```

- Container logs are capped at 3 × 10 MB per container. nginx access logs omit query
  strings.
- The stack restarts on its own after a reboot (`restart: unless-stopped`); graph data lives
  in the `brain_falkordb-data` volume (append-only file, fsync every second).
- FalkorDB limits are set on purpose; see "FalkorDB limits" below.
- The boot disk is snapshotted daily at 20:30 UTC (02:00 IST), and snapshots are kept for 7
  days (`brain-daily-snapshots`).
- FalkorDB itself is backed up nightly to GCS and kept 30 days; see "Backups and restore".
- Budget alert: ₹3,500/month (about $40; the billing account is in INR), with emails to
  admin@wattcycle.ai at 50, 90 and 100 %, plus a forecast alert at 100 %.

## FalkorDB limits

The FalkorDB images (`falkordb/falkordb` and `falkordb/falkordb-server`) start with
`FALKORDB_ARGS="MAX_QUEUED_QUERIES 25 TIMEOUT 1000 RESULTSET_SIZE 10000"`. With those, every
result is cut at 10,000 rows without an error, and any read that runs past 1 s fails with
"Query timed out". Both compose files therefore set:

```
FALKORDB_ARGS: MAX_QUEUED_QUERIES 100 TIMEOUT 10000 RESULTSET_SIZE -1
```

| Setting | Value | Why |
| --- | --- | --- |
| `RESULTSET_SIZE` | `-1` (no cap) | A capped result looks like a complete one. Queries bound their own size with `LIMIT`. |
| `TIMEOUT` | `10000` ms | Applies to read queries only; writes are never cut off. A read slower than 10 s is a bug to fix, not to wait for. |
| `MAX_QUEUED_QUERIES` | `100` | Queries waiting for a worker thread (one per core, 2 on the e2-medium) before new ones are refused. 25 was tight once a slow tool held both threads. |

The values take effect when the container is created, so after changing them recreate it
(`sudo docker compose up -d falkordb`; the data volume is kept). Check what is in force:

```bash
sudo docker compose exec falkordb redis-cli GRAPH.CONFIG GET TIMEOUT          # 10000
sudo docker compose exec falkordb redis-cli GRAPH.CONFIG GET RESULTSET_SIZE   # -1
sudo docker compose exec falkordb redis-cli GRAPH.CONFIG GET MAX_QUEUED_QUERIES  # 100
```

(`REDISCLI_AUTH` is set in the container, so no password is typed.) Locally, recreate the dev
container with `docker compose up -d falkordb` to pick up the same values.

## Backups and restore

The boot-disk snapshot copies the FalkorDB volume as the disk happens to be at that moment.
The FalkorDB backup is a real RDB snapshot, kept off the VM, with a record of what it should
contain.

| | |
| --- | --- |
| When | `brain-falkordb-backup.timer`, 21:30 UTC (03:00 IST) plus up to 15 min, after the disk snapshot |
| What | `deploy/backup/falkordb-backup.py backup`, at `/opt/brain/falkordb-backup.py` |
| Where | `gs://graphhacks-brain-8490-falkordb-backups/falkordb/YYYY/MM/DD/brain-falkordb-<UTC stamp>.rdb` and `.json` |
| Kept | 30 days (lifecycle), then 7 more days as a noncurrent version (versioning), then deleted |
| Who writes | `brain-vm-sa`, with `roles/storage.objectCreator` on the bucket only: it can add objects but not read, overwrite or delete them |

Each run:

1. counts the nodes per label and the edges per relationship type in every graph,
2. runs `BGSAVE SCHEDULE` in the `falkordb` container and waits for it to finish (and
   `rdb_last_bgsave_status:ok`),
3. counts again; if the counts moved (a write landed while the snapshot was taken) it starts
   over, up to three times, and otherwise records both sets with `"consistent": false`,
4. copies the RDB out with `docker cp` (and starts over if another save finished meanwhile),
5. uploads the RDB, then the manifest: the counts, the FalkorDB image, the RDB's size and
   sha256. The upload uses the VM's token from the metadata server, the JSON API with
   `ifGenerationMatch=0` (never overwrites), and checks the MD5 GCS reports.

Nothing is kept on the VM; the RDB is staged in `/var/tmp` and deleted. `deploy.sh`
installs the script and enables the timer on every deploy. Until the bucket exists, each run
logs one line (`skipped: gs://... does not exist yet`) and exits 0. Any other failure, such as
a 403 from missing IAM or scopes, is one `failed:` line, and the unit exits non-zero.

### What a backup holds, and who may read it

An RDB is the whole of FalkorDB, not only the graphs. It also holds the key-value store
(console sessions, OAuth state, event dedupe, approvals, the scheduler's keys) and every
connector install's credentials, which are encrypted with `TOKEN_ENCRYPTION_KEYS`. Treat the
bucket like production itself:

- Only the people who may read production secrets may read the bucket. Nobody is granted
  anything on it beyond the VM's `objectCreator`, but project-level roles (Owner, Editor,
  Storage Admin or Viewer) reach it too. Check both before adding anyone:
  `gcloud storage buckets get-iam-policy gs://graphhacks-brain-8490-falkordb-backups --configuration=graphhacks`
  and `gcloud projects get-iam-policy graphhacks-brain-8490 --configuration=graphhacks`.
- Never make it public, and never copy a backup anywhere outside the project, except to a
  local disk for `verify`. Delete `/tmp/brain-restore` afterwards.

**The encryption keys are not in the backup.** `TOKEN_ENCRYPTION_KEYS` (and the older single
`TOKEN_ENCRYPTION_KEY`, which encrypts Slack bot tokens) live in Secret Manager, project
`graphhacks-brain-8490`. `deploy.sh` writes the latest version into `/opt/brain/.env`. A
restored graph whose connector secrets were encrypted with a key that is no longer in that
list has credentials nobody can decrypt, so every connector needs reconnecting. Hence:

- Never disable or destroy a version of either secret until 37 days after a newer version
  replaced it (30 days of backups plus 7 noncurrent). Key rotation (docs/CONSOLE.md) only adds
  versions, so nothing more is needed in the ordinary course.
- If the keys were rotated after the backup was taken, find the version that was latest at
  the backup's `created_at` before restoring. Add a new version that lists the current keys
  first and then that version's keys, and redeploy. MultiFernet decrypts with any key in the
  list.

  ```bash
  gcloud secrets versions list TOKEN_ENCRYPTION_KEYS --configuration=graphhacks   # createTime per version
  gcloud secrets versions access <n> --secret=TOKEN_ENCRYPTION_KEYS --configuration=graphhacks
  ```

A restore also brings back the key-value state from that moment. Sessions and OAuth flows
started since are gone, and anything disconnected since (a connector install, a revoked
session) is back. Disconnect it again after the restore.

```bash
# On the VM
systemctl list-timers brain-falkordb-backup.timer
journalctl -u brain-falkordb-backup --since -2d
sudo systemctl start brain-falkordb-backup     # a backup now (runs in the foreground)

# From your machine
gcloud storage ls -l gs://graphhacks-brain-8490-falkordb-backups/falkordb/** --configuration=graphhacks | tail
```

### Checking that a backup restores

Run this on any machine with Docker (not the VM: its service account cannot read the
bucket). It checks the sha256, loads the RDB into a throwaway FalkorDB container (no network,
the image named in the manifest), compares every graph's counts per label and per type with
the manifest, prints each mismatch, and removes the container. Exit 0 means every count
matched. For a manifest with `"consistent": false`, a count passes if it lies between the
before and after values.

This is a shape check, not a content check. Matching counts mean the RDB loads and has the
same nodes per label and edges per type as production had. They say nothing about property
values, scopes or the key-value store. The sha256 proves only that the RDB is the file that
was uploaded. For more, `--keep` the container and query it: open a few known nodes, or a
connector install's `secret_enc` decrypted with the current keys.

```bash
B=gs://graphhacks-brain-8490-falkordb-backups/falkordb/2026/10/19/brain-falkordb-20261019T213512Z
mkdir -p /tmp/brain-restore && gcloud storage cp "$B.rdb" "$B.json" /tmp/brain-restore/ --configuration=graphhacks
deploy/backup/falkordb-backup.py verify /tmp/brain-restore/brain-falkordb-20261019T213512Z.{rdb,json}
```

`--keep` leaves the container running for a look around (`docker exec -it <name> redis-cli`);
remove it with `docker rm -f -v <name>` afterwards.

### Restoring production from a backup

Only after `verify` passes on the chosen RDB. This replaces everything in FalkorDB, including
anything written since the backup.

```bash
gcloud compute scp /tmp/brain-restore/brain-falkordb-<stamp>.rdb brain-vm:/tmp/restore.rdb \
  --zone asia-south1-c --tunnel-through-iap --configuration=graphhacks
# On the VM
cd /opt/brain
sudo docker compose stop backend falkordb-browser falkordb
V=$(sudo docker volume inspect brain_falkordb-data --format '{{.Mountpoint}}')
sudo mv "$V" "$V.before-restore.$(date -u +%Y%m%dT%H%M%SZ)" && sudo install -d -m 0755 "$V/appendonlydir"
# With appendonly on, FalkorDB ignores a bare dump.rdb and starts empty. Hand it the RDB as
# the base of a new append-only file instead (tested on falkordb-server v4.22.1).
sudo install -m 0644 /tmp/restore.rdb "$V/appendonlydir/appendonly.aof.1.base.rdb" && rm /tmp/restore.rdb
sudo install -m 0644 /dev/null "$V/appendonlydir/appendonly.aof.1.incr.aof"
printf 'file appendonly.aof.1.base.rdb seq 1 type b\nfile appendonly.aof.1.incr.aof seq 1 type i\n' |
  sudo tee "$V/appendonlydir/appendonly.aof.manifest" >/dev/null
sudo docker compose up -d
sudo docker compose exec falkordb redis-cli GRAPH.LIST
```

Delete the `.before-restore` directory once the restore is confirmed.

### One-time setup (human, with the graphhacks configuration)

```bash
C=--configuration=graphhacks
BUCKET=gs://graphhacks-brain-8490-falkordb-backups
VM_SA=brain-vm-sa@graphhacks-brain-8490.iam.gserviceaccount.com

# 1. The bucket: same region as the VM, no public access, versioned, 30-day lifecycle
gcloud storage buckets create $BUCKET --project=graphhacks-brain-8490 --location=asia-south1 \
  --default-storage-class=STANDARD --uniform-bucket-level-access --public-access-prevention $C
gcloud storage buckets update $BUCKET --versioning --lifecycle-file=deploy/backup/lifecycle.json $C

# 2. The VM may add objects to it, and nothing else
gcloud storage buckets add-iam-policy-binding $BUCKET \
  --member=serviceAccount:$VM_SA --role=roles/storage.objectCreator $C

# 3. The VM's access scopes must allow writing to GCS: either cloud-platform or devstorage.read_write
gcloud compute instances describe brain-vm --zone asia-south1-c \
  --format='value(serviceAccounts[0].email,serviceAccounts[0].scopes)' $C
```

If step 3 shows neither `https://www.googleapis.com/auth/cloud-platform` nor
`.../devstorage.read_write`, the upload fails with HTTP 403 (insufficient scopes). Changing
scopes needs the VM stopped, so plan a short outage:

```bash
gcloud compute instances stop brain-vm --zone asia-south1-c $C
gcloud compute instances set-service-account brain-vm --zone asia-south1-c \
  --service-account=$VM_SA --scopes=cloud-platform $C
gcloud compute instances start brain-vm --zone asia-south1-c $C
```

Then deploy (any push to `main`) so the timer is installed, and run one backup by hand on
the VM (`sudo systemctl start brain-falkordb-backup`) and one `verify` of it from your machine.

## Costs (approximate, per month)

| Item | ≈ USD |
| --- | --- |
| e2-medium VM, asia-south1, always on | 29 |
| 30 GB pd-balanced boot disk | 3.5 |
| Static external IPv4 in use | 3.7 |
| Snapshots (7 daily, incremental) | 1 |
| FalkorDB backups (about 30 nightly RDBs in GCS) | < 0.1 |
| Artifact Registry, Secret Manager, logging, egress | < 1 |
| **Total** | **≈ 37** |

## The two Slack apps

A Slack app delivers events to exactly one Request URL, so production and development are
separate apps that run the same code with different secrets:

| App | Mode | Request URLs | Secrets |
| --- | --- | --- | --- |
| `central-brain` (production) | HTTP (Events API) | `https://brain.noobsurd.space/slack/events` and `/slack/interactions` | Secret Manager, as above |
| `dev-central-brain` (development) | HTTP through a tunnel | the developer's ngrok URL ([SLACK.md](SLACK.md)) | the developer's local `.env` |

Production's manifest is [`deploy/slack-app-manifest.yaml`](../deploy/slack-app-manifest.yaml);
the dev app's is [`slack_app/manifest.yaml`](../slack_app/manifest.yaml). Pointing the dev
app at the server, or the production app at a laptop, cuts the other one off.

The code only takes Slack over HTTP. A Socket Mode dev app wouldn't need a tunnel, but it
would need a Socket Mode entry point in the code, which nothing here has yet. Until then the
dev app uses a tunnel.

## For the Slack/AI developer

- **Where the code goes.** The Slack routes and the dispatch are `src/api/v1/slack.py`,
  the turn handling is `src/domain/slack/`, and the model seam is
  `src/integrations/llm/model.py`. The infra touches only `src/api/ops.py` (`/healthz`,
  the seed refresh), one line in `src/main.py`, and the FalkorDB password in
  `src/core/config.py` and `src/db/falkordb_client.py`.
- **Env vars in production.** `FALKORDB_HOST=falkordb`, `FALKORDB_PORT=6379`,
  `FALKORDB_PASSWORD`, `PUBLIC_URL=https://brain.noobsurd.space`, `SLACK_SIGNING_SECRET`,
  `SLACK_BOT_TOKEN`, `LLM_API_KEY` (also as `MODEL_API_KEY`), `GIT_SHA`, plus any
  optional secret above that exists. `LLM_MODEL` falls back to the code's default.
- **Local runs.** Follow [SLACK.md](SLACK.md) with the `dev-central-brain` app and your
  tunnel. Locally `FALKORDB_PASSWORD` is empty and the dev compose has no password.
- **To production.** Merge to `main`. The Deploy workflow runs CI, builds `backend:<sha>`,
  deploys it, and rolls back if `/healthz` doesn't come up. Watch it with
  `gh run watch`, or check `https://brain.noobsurd.space/healthz` for the new `git_sha`.
- **Keep it green.** `ruff check .` (rules in `ruff.toml`) and `pytest` (needs FalkorDB on
  `localhost:6379`, e.g. `docker compose up -d`) are what CI runs. New Python dependencies go
  in `requirements.txt`; the image installs from it.
- **Logging.** Don't log message text or secrets; the production logs are kept.
- `slack_last_event_at` on `/healthz` moves whenever Slack gets a 200 from `/slack/events`
  or `/slack/interactions`. That's a quick way to see that events are arriving.

## Teardown

Everything lives in one project, so deleting it removes the VM, disk, snapshots, IP,
backup bucket, registry, secrets, service accounts and the workload identity pool:

```bash
gcloud projects delete graphhacks-brain-8490 --configuration=graphhacks
```

Then, outside the project:

```bash
# Budget and its alert (the budget lives on the billing account, not in the project)
gcloud billing budgets list --billing-account=0174FA-F3C7EC-EBB0F3 --configuration=graphhacks
gcloud billing budgets delete <budget id> --billing-account=0174FA-F3C7EC-EBB0F3 --configuration=graphhacks

# GitHub Actions variables
for v in GCP_PROJECT_ID GCP_REGION GCP_ZONE GCP_VM GCP_DEPLOYER_SA GCP_WIF_PROVIDER; do
  gh variable delete $v -R codesensei-tushar/graph_hacks
done

# DNS: delete only the brain and gdb A records in Cloudflare (noobsurd.space).

# The local gcloud configuration
gcloud config configurations delete graphhacks
```

Piece by piece, if the project should stay:

```bash
C=--configuration=graphhacks
gcloud compute instances delete brain-vm --zone asia-south1-c $C
gcloud compute resource-policies delete brain-daily-snapshots --region asia-south1 $C
gcloud storage rm --recursive --all-versions gs://graphhacks-brain-8490-falkordb-backups $C
gcloud compute addresses delete brain-ip --region asia-south1 $C
gcloud compute firewall-rules delete brain-allow-web brain-allow-ssh-iap $C
gcloud compute networks subnets delete brain-subnet --region asia-south1 $C
gcloud compute networks delete brain-vpc $C
gcloud artifacts repositories delete brain --location asia-south1 $C
for s in SLACK_SIGNING_SECRET SLACK_BOT_TOKEN MODEL_API_KEY FALKORDB_PASSWORD GDB_BASIC_AUTH_PASSWORD NEXTAUTH_SECRET; do
  gcloud secrets delete $s $C
done
gcloud iam workload-identity-pools delete github --location global $C
gcloud iam service-accounts delete brain-deployer@graphhacks-brain-8490.iam.gserviceaccount.com $C
gcloud iam service-accounts delete brain-vm-sa@graphhacks-brain-8490.iam.gserviceaccount.com $C
```
