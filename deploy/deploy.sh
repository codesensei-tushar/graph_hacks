#!/usr/bin/env bash
# Deploy the backend image and the deploy/ files of one commit. Runs on brain-vm as root:
#
#   sudo /opt/brain/deploy.sh <40-char git sha>
#
# CI first unpacks that commit's deploy/ directory into /opt/brain/releases/<sha>/ (see
# .github/workflows/deploy.yml). This script then copies that release's compose and nginx
# files into place, writes /opt/brain/.env and the gdb htpasswd from Secret Manager
# (root-only), pulls, runs `docker compose up -d`, reloads nginx, and polls /healthz until
# it reports <sha> with FalkorDB ok (120 s). On failure it redeploys the SHA in
# /opt/brain/last_good_sha and exits non-zero; on success it records <sha> there.
#
# Certificates: bootstrap-tls.sh issues the first one, the systemd timer renews. After a
# good deploy this script runs `bootstrap-tls.sh --ensure`, which adds any name in its DOMAINS
# the certificate lacks (falkordb.noobsurd.space), but only once DNS points it at this VM; it
# never fails the deploy and never touches a certificate it cannot replace.
# It installs the nightly FalkorDB backup (backup/falkordb-backup.py and its timer) the same way.
set -euo pipefail
umask 077
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin

BRAIN=/opt/brain
PUBLIC_HOST=brain.noobsurd.space
HEALTH_TIMEOUT_SECONDS=120
KEEP_RELEASES=10
PLACEHOLDER=REPLACE_ME
# The nginx group inside nginx:alpine. Workers read the htpasswd on every request.
NGINX_GID=101
# Written to .env only when the secret exists. See docs/DEPLOY.md.
OPTIONAL_SECRETS="SLACK_OWNER_ID SLACK_ADMIN_IDS SLACK_CLIENT_ID SLACK_CLIENT_SECRET BRAIN_ADMIN_TOKEN MCP_TOKEN TOKEN_ENCRYPTION_KEY LLM_MODEL GITHUB_APP_ID GITHUB_WEBHOOK_SECRET GITHUB_PRIVATE_KEY GITHUB_CLIENT_ID GITHUB_CLIENT_SECRET"
# The org console (docs/CONSOLE.md). Never named CONNECTOR_*: registry.load would read those as
# connector install fields.
OPTIONAL_SECRETS="$OPTIONAL_SECRETS CONSOLE_ENABLED TOKEN_ENCRYPTION_KEYS JIRA_OAUTH_CLIENT_ID JIRA_OAUTH_CLIENT_SECRET HUBSPOT_OAUTH_CLIENT_ID HUBSPOT_OAUTH_CLIENT_SECRET"
# Connector settings (docs/CONNECTORS.md): every secret named CONNECTOR_<SOURCE>_<FIELD>. These
# names are tried on every deploy; any other CONNECTOR_ secret is found too once the VM may list
# secrets (roles/secretmanager.viewer). Like the optional ones, a missing secret is skipped.
CONNECTOR_SECRETS="CONNECTOR_JIRA_BASE_URL CONNECTOR_JIRA_EMAIL CONNECTOR_JIRA_API_TOKEN CONNECTOR_JIRA_PROJECTS CONNECTOR_JIRA_PEOPLE CONNECTOR_JIRA_WEBHOOK_SECRET CONNECTOR_HUBSPOT_TOKEN CONNECTOR_HUBSPOT_PEOPLE CONNECTOR_HUBSPOT_API_VERSION CONNECTOR_HUBSPOT_CLIENT_SECRET"
SELF=$(readlink -f "$0")

log() { printf '[deploy] %s\n' "$*" >&2; }
compose() { docker compose --project-directory "$BRAIN" "$@"; }
secret() { gcloud secrets versions access latest --secret="$1" --project="$PROJECT" --quiet; }

# The known connector names, plus every CONNECTOR_ secret the VM is allowed to list. Names only
# ever match CONNECTOR_[A-Z0-9_]+, because each becomes a key in .env.
connector_secrets() {
  local listed
  listed=$(gcloud secrets list --project="$PROJECT" --quiet --filter='name ~ /secrets/CONNECTOR_' --format='value(name.basename())' 2>/dev/null) || listed=
  # shellcheck disable=SC2086  # word splitting is the point: two space- or newline-separated lists
  printf '%s\n' $CONNECTOR_SECRETS $listed | grep -E '^CONNECTOR_[A-Z0-9_]+$' | sort -u || true
}

env_line() {
  case $2 in
    *$'\n'* | *"'"*) log "$1 contains a newline or a single quote; refusing to write it"; return 1 ;;
  esac
  printf "%s='%s'\n" "$1" "$2"
}

install_units() {
  local unit changed=0
  for unit in "$1"/*; do
    if ! cmp -s "$unit" "/etc/systemd/system/${unit##*/}"; then
      install -m 0644 "$unit" /etc/systemd/system/
      changed=1
    fi
  done
  if [ "$changed" = 1 ]; then systemctl daemon-reload; fi
  systemctl enable --now brain-certbot-renew.timer brain-falkordb-backup.timer 2>/dev/null
}

activate() {
  local rel=$BRAIN/releases/$1
  if [ ! -f "$rel/docker-compose.yml" ]; then
    log "no release unpacked at $rel"
    return 1
  fi
  install -m 0644 "$rel/docker-compose.yml" "$BRAIN/docker-compose.yml"
  install -d -m 0755 "$BRAIN/nginx"
  install -m 0644 "$rel"/nginx/*.conf "$BRAIN/nginx/"
  install -m 0755 "$rel/bootstrap-tls.sh" "$BRAIN/bootstrap-tls.sh"
  install -m 0755 "$rel/backup/falkordb-backup.py" "$BRAIN/falkordb-backup.py"
  if [ ! -e "$BRAIN/nginx/active.conf" ]; then ln -s http.conf "$BRAIN/nginx/active.conf"; fi
  # gdb.noobsurd.space serves the browser until --ensure switches it to the redirect.
  if [ ! -e "$BRAIN/nginx/gdb-active.conf" ]; then ln -s browser.conf "$BRAIN/nginx/gdb-active.conf"; fi
  install_units "$rel/systemd"
}

browser_host() {
  if [ "$(readlink "$BRAIN/nginx/gdb-active.conf" 2>/dev/null)" = gdb-redirect.conf ]; then
    echo falkordb.noobsurd.space
  else
    echo gdb.noobsurd.space
  fi
}

write_env() {
  local signing bot model falkordb nextauth name value tmp err
  falkordb=$(secret FALKORDB_PASSWORD)
  nextauth=$(secret NEXTAUTH_SECRET)
  signing=$(secret SLACK_SIGNING_SECRET)
  bot=$(secret SLACK_BOT_TOKEN)
  model=$(secret MODEL_API_KEY)
  # Until the real values land: verify Slack requests against a random secret (so every one
  # is rejected, and nobody can sign with the public placeholder), and leave the bot token
  # and model key empty so the app takes its "not configured" paths.
  if [ "$signing" = "$PLACEHOLDER" ]; then signing=$(openssl rand -hex 32); fi
  if [ "$bot" = "$PLACEHOLDER" ]; then bot=; fi
  if [ "$model" = "$PLACEHOLDER" ]; then model=; fi

  tmp=$(mktemp "$BRAIN/.env.XXXXXX")
  err=$(mktemp)
  {
    echo "# Written by deploy.sh from Secret Manager. Don't edit: add a secret version and redeploy."
    env_line IMAGE_TAG "$1"
    env_line FALKORDB_PASSWORD "$falkordb"
    env_line NEXTAUTH_SECRET "$nextauth"
    # The browser's public name: falkordb.* once gdb.* redirects there (bootstrap-tls.sh --ensure).
    env_line BROWSER_HOST "$(browser_host)"
    env_line SLACK_SIGNING_SECRET "$signing"
    env_line SLACK_BOT_TOKEN "$bot"
    # The app reads LLM_API_KEY; MODEL_API_KEY is the Secret Manager name, kept for parity.
    env_line LLM_API_KEY "$model"
    env_line MODEL_API_KEY "$model"
    # The VM can read secrets but not list them, so a missing one shows up as NOT_FOUND.
    for name in $OPTIONAL_SECRETS $(connector_secrets); do
      if value=$(secret "$name" 2>"$err"); then
        env_line "$name" "$value"
      elif ! grep -q NOT_FOUND "$err"; then
        cat "$err" >&2
        return 1
      fi
    done
  } >"$tmp"
  rm -f "$err"
  chmod 0600 "$tmp"
  mv -f "$tmp" "$BRAIN/.env"
}

write_htpasswd() {
  local password hash tmp
  password=$(secret GDB_BASIC_AUTH_PASSWORD)
  hash=$(printf '%s\n' "$password" | openssl passwd -apr1 -stdin)
  tmp=$(mktemp "$BRAIN/nginx/.htpasswd.XXXXXX")
  printf 'admin:%s\n' "$hash" >"$tmp"
  chown "root:$NGINX_GID" "$tmp"
  chmod 0640 "$tmp"
  mv -f "$tmp" "$BRAIN/nginx/gdb.htpasswd"
}

healthz() {
  if [ "$(readlink "$BRAIN/nginx/active.conf")" = https.conf ]; then
    # The public path (nginx, TLS, backend), pinned to this host.
    curl -fsS --max-time 5 --resolve "$PUBLIC_HOST:443:127.0.0.1" "https://$PUBLIC_HOST/healthz"
  else
    # Before bootstrap-tls.sh nginx serves nothing, so ask the backend directly.
    compose exec -T backend python -c \
      "import sys, urllib.request; sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5).read().decode())"
  fi
}

wait_healthy() {
  local body deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
  while ((SECONDS < deadline)); do
    if body=$(healthz 2>/dev/null) && python3 -c '
import json, sys
health = json.loads(sys.argv[1])
sys.exit(0 if health.get("git_sha") == sys.argv[2] and health.get("falkordb") == "ok" else 1)
' "$body" "$1"; then
      log "healthy: $body"
      return 0
    fi
    sleep 3
  done
  log "not healthy at $1 after ${HEALTH_TIMEOUT_SECONDS}s; last /healthz: ${body:-no answer}"
  return 1
}

# One attempt at one SHA. Runs as its own process (see below) so `set -e` holds throughout.
apply() {
  PROJECT=$(curl -fsS -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/project/project-id)
  activate "$1"
  write_env "$1"
  write_htpasswd
  compose pull --quiet
  compose up -d --remove-orphans
  compose exec -T nginx nginx -t -q
  compose exec -T nginx nginx -s reload
  wait_healthy "$1"
}

prune() {
  local keep old
  keep=$(cat "$BRAIN/last_good_sha")
  find "$BRAIN/releases" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %f\n' | sort -rn | tail -n +$((KEEP_RELEASES + 1)) |
    while read -r _ old; do
      if [ "$old" != "$keep" ]; then rm -rf "${BRAIN:?}/releases/$old"; fi
    done
  docker image prune -af --filter "until=168h" >/dev/null || true
}

if [ "${1:-}" = --apply ]; then
  apply "$2"
  exit 0
fi

SHA=${1:-}
if [[ ! $SHA =~ ^[0-9a-f]{40}$ ]]; then
  echo "usage: $0 <40-char git sha>" >&2
  exit 2
fi
if [ "$(id -u)" != 0 ]; then
  echo "run as root: sudo $0 $SHA" >&2
  exit 2
fi
exec 9>"$BRAIN/.deploy.lock"
if ! flock -n 9; then
  log "another deploy is running"
  exit 1
fi

log "deploying $SHA"
if "$SELF" --apply "$SHA"; then
  printf '%s\n' "$SHA" >"$BRAIN/last_good_sha"
  chmod 0644 "$BRAIN/last_good_sha"
  prune
  log "deployed $SHA"
  # Add new certificate names if their DNS is ready. Never fails the deploy.
  timeout 600 "$BRAIN/bootstrap-tls.sh" --ensure || log "certificate check failed (see above); the deploy stands"
  exit 0
fi

LAST=$(cat "$BRAIN/last_good_sha" 2>/dev/null || true)
if [ -n "$LAST" ] && [ "$LAST" != "$SHA" ]; then
  log "deploy of $SHA failed; rolling back to $LAST"
  if "$SELF" --apply "$LAST"; then
    log "rolled back to $LAST"
  else
    log "rollback to $LAST failed too; inspect with: cd $BRAIN && docker compose ps && docker compose logs --tail 100"
  fi
else
  log "deploy of $SHA failed and there is no earlier good release to roll back to"
fi
exit 1
