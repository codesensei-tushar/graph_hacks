#!/usr/bin/env bash
# The Let's Encrypt certificate for every name in DOMAINS (one certificate, cert name "brain",
# one SAN per name), and the switch of nginx to the HTTPS site.
#
#   sudo /opt/brain/bootstrap-tls.sh            # first issuance; strict, exits non-zero on any problem
#   sudo /opt/brain/bootstrap-tls.sh --ensure   # after deploys and before renewals; never fails
#
# Without --ensure: needs one successful deploy.sh run first (it writes .env and starts the
# stack), and every name resolving to this VM. DNS is checked first and a --dry-run against
# the staging CA goes before the real request, so mistakes don't eat Let's Encrypt's
# production rate limits. Safe to re-run: a valid certificate is kept as it is.
#
# --ensure (deploy.sh after a good deploy, brain-certbot-renew.service before each renewal):
# if HTTPS is on and the certificate lacks a name in DOMAINS, it checks that every name
# resolves to this VM, then expands the certificate (staging dry run first). When DNS is not
# there yet, or anything fails, it logs one line and exits 0: certbot replaces a certificate
# only after a new one is issued, so the current one keeps serving, and the next deploy or
# renewal tries again. Once the certificate covers falkordb.noobsurd.space it points
# gdb.noobsurd.space at the redirect (nginx/gdb-active.conf -> gdb-redirect.conf) and the
# browser's public URL at the new name (BROWSER_HOST in .env).
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin

BRAIN=/opt/brain
CERT_NAME=brain
# falkordb.* is the graph browser; gdb.* is its old name, kept (and redirected) for a while.
DOMAINS=(brain.noobsurd.space falkordb.noobsurd.space gdb.noobsurd.space)
BROWSER_HOST=falkordb.noobsurd.space
EMAIL=admin@wattcycle.ai
LIVE=/etc/letsencrypt/live/$CERT_NAME

log() { printf '[tls] %s\n' "$*" >&2; }
compose() { docker compose --project-directory "$BRAIN" "$@"; }

ENSURE=0
if [ "${1:-}" = --ensure ]; then ENSURE=1; fi

if [ "$(id -u)" != 0 ]; then
  echo "run as root: sudo $0" >&2
  exit 2
fi

vm_ip() {
  curl -fsS --max-time 5 -H 'Metadata-Flavor: Google' \
    http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip
}

# Every name in "$@" resolves to this VM, or one line says which does not.
dns_ready() {
  local ip domain resolved
  ip=$(vm_ip)
  for domain in "$@"; do
    resolved=$(getent ahostsv4 "$domain" | awk 'NR == 1 { print $1 }') || resolved=
    if [ "$resolved" != "$ip" ]; then
      log "$domain resolves to '${resolved:-nothing}', not this VM ($ip)"
      return 1
    fi
  done
}

request() {
  local args=(certonly --webroot -w /var/www/certbot --cert-name "$CERT_NAME"
    --email "$EMAIL" --agree-tos --no-eff-email --non-interactive "$@")
  local domain
  for domain in "${DOMAINS[@]}"; do args+=(-d "$domain"); done
  compose exec -T certbot certbot "${args[@]}"
}

reload_nginx() {
  compose exec -T nginx nginx -t -q && compose exec -T nginx nginx -s reload
}

# The names on the current certificate, one per line (empty if there is none).
cert_names() {
  compose exec -T certbot certbot certificates --cert-name "$CERT_NAME" 2>/dev/null |
    awk '/Domains:/ { for (i = 2; i <= NF; i++) print $i }'
}

covers() {
  local names
  # Not a pipe into grep -q: with pipefail, grep quitting early would fail the pipeline.
  names=$(cert_names) || names=
  grep -qxF "$1" <<<"$names"
}

# gdb.* -> 301 to falkordb.*, and the browser's own URL to falkordb.*. Only once the
# certificate covers the new name; does nothing if already done.
switch_browser_name() {
  local link=$BRAIN/nginx/gdb-active.conf
  if ! covers "$BROWSER_HOST"; then return 0; fi
  if [ "$(readlink "$link" 2>/dev/null)" != gdb-redirect.conf ]; then
    ln -sfn gdb-redirect.conf "$link"
    if ! reload_nginx; then
      ln -sfn browser.conf "$link"
      reload_nginx || true
      log "nginx refused the gdb redirect; gdb.* keeps serving the browser"
      return 1
    fi
    log "gdb.noobsurd.space now redirects to $BROWSER_HOST"
  fi
  if ! grep -qxF "BROWSER_HOST='$BROWSER_HOST'" "$BRAIN/.env"; then
    sed -i "/^BROWSER_HOST=/d" "$BRAIN/.env"
    printf "BROWSER_HOST='%s'\n" "$BROWSER_HOST" >>"$BRAIN/.env"
    compose up -d falkordb-browser
    log "the browser's public URL is now https://$BROWSER_HOST"
  fi
}

ensure() {
  local missing=() domain
  if [ "$(readlink "$BRAIN/nginx/active.conf" 2>/dev/null)" != https.conf ] || ! compose exec -T certbot test -f "$LIVE/fullchain.pem"; then
    log "HTTPS is not set up yet; run sudo $BRAIN/bootstrap-tls.sh once. Nothing to ensure."
    return 0
  fi
  for domain in "${DOMAINS[@]}"; do
    if ! covers "$domain"; then missing+=("$domain"); fi
  done
  if [ ${#missing[@]} -gt 0 ]; then
    if ! dns_ready "${DOMAINS[@]}"; then
      log "not adding ${missing[*]} to the certificate yet; it stays as it is. Retried on the next deploy or renewal."
      return 0
    fi
    log "adding ${missing[*]} to the certificate (staging dry run first)"
    if ! request --expand --dry-run || ! request --expand; then
      log "could not add ${missing[*]}; the current certificate stays. Retried on the next deploy or renewal."
      return 0
    fi
    reload_nginx
    log "the certificate now covers ${DOMAINS[*]}"
  fi
  switch_browser_name || true
}

if [ "$ENSURE" = 1 ]; then
  ensure || log "--ensure stopped early; nothing was changed that the current setup needs"
  exit 0
fi

if [ ! -f "$BRAIN/.env" ]; then
  log "run deploy.sh once first"
  exit 1
fi
if ! dns_ready "${DOMAINS[@]}"; then
  log "Fix DNS first."
  exit 1
fi

compose up -d nginx certbot
if ! compose exec -T certbot test -f "$LIVE/fullchain.pem"; then
  log "no certificate yet: serving the HTTP-only site for the challenge"
  ln -sfn http.conf "$BRAIN/nginx/active.conf"
  reload_nginx
fi

log "dry run against the Let's Encrypt staging CA"
request --expand --dry-run
log "requesting the certificate (kept if it is valid, covers every name and is not yet due for renewal)"
request --expand --keep-until-expiring

ln -sfn https.conf "$BRAIN/nginx/active.conf"
reload_nginx
switch_browser_name
compose exec -T certbot certbot certificates --cert-name "$CERT_NAME"
log "HTTPS is on for ${DOMAINS[*]}"
