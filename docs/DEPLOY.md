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
| `deploy/systemd/` | Twice-daily renewal timer |
| `deploy/vm/startup.sh` | VM startup script: Docker, log caps, swap, registry credential helper |
| `deploy/slack-app-manifest.yaml` | Manifest for the production Slack app |
| `.github/workflows/ci.yml` | ruff, pytest (with a FalkorDB service), docker build |
| `.github/workflows/deploy.yml` | CI, then build, push, deploy, verify |
| `src/api/ops.py` | `/healthz`, `POST /ops/seed`, `POST /ops/import`, the Slack event clock |
| `src/api/mcp.py` | `/mcp`: the read-only tools over MCP, public and rate-limited |

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
(see [GITHUB.md](GITHUB.md); the private key is stored on one line).

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
- The boot disk is snapshotted daily at 20:30 UTC (02:00 IST), and snapshots are kept for 7
  days (`brain-daily-snapshots`).
- Budget alert: ₹3,500/month (about $40; the billing account is in INR), with emails to
  admin@wattcycle.ai at 50, 90 and 100 %, plus a forecast alert at 100 %.

## Costs (approximate, per month)

| Item | ≈ USD |
| --- | --- |
| e2-medium VM, asia-south1, always on | 29 |
| 30 GB pd-balanced boot disk | 3.5 |
| Static external IPv4 in use | 3.7 |
| Snapshots (7 daily, incremental) | 1 |
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
registry, secrets, service accounts and the workload identity pool:

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
