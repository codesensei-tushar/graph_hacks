#!/usr/bin/env bash
# Get the Let's Encrypt certificate for brain.noobsurd.space + gdb.noobsurd.space and
# switch nginx to the HTTPS site. Safe to re-run: a valid certificate is kept as it is.
#
#   sudo /opt/brain/bootstrap-tls.sh
#
# Needs one successful deploy.sh run first (it writes .env and starts the stack), and
# both names resolving to this VM. DNS is checked first and a --dry-run against the
# staging CA goes before the real request, so mistakes don't eat Let's Encrypt's
# production rate limits. Renewal is the brain-certbot-renew.timer systemd unit.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin

BRAIN=/opt/brain
CERT_NAME=brain
DOMAINS=(brain.noobsurd.space gdb.noobsurd.space)
EMAIL=admin@wattcycle.ai

log() { printf '[tls] %s\n' "$*" >&2; }
compose() { docker compose --project-directory "$BRAIN" "$@"; }

if [ "$(id -u)" != 0 ]; then
  echo "run as root: sudo $0" >&2
  exit 2
fi
if [ ! -f "$BRAIN/.env" ]; then
  log "run deploy.sh once first"
  exit 1
fi

ip=$(curl -fsS -H 'Metadata-Flavor: Google' \
  http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip)
for domain in "${DOMAINS[@]}"; do
  resolved=$(getent ahostsv4 "$domain" | awk 'NR == 1 { print $1 }')
  if [ "$resolved" != "$ip" ]; then
    log "$domain resolves to '${resolved:-nothing}', not this VM ($ip). Fix DNS first."
    exit 1
  fi
done

request=(certonly --webroot -w /var/www/certbot --cert-name "$CERT_NAME"
  --email "$EMAIL" --agree-tos --no-eff-email --non-interactive)
for domain in "${DOMAINS[@]}"; do request+=(-d "$domain"); done

compose up -d nginx certbot
if ! compose exec -T certbot test -f "/etc/letsencrypt/live/$CERT_NAME/fullchain.pem"; then
  log "no certificate yet: serving the HTTP-only site for the challenge"
  ln -sfn http.conf "$BRAIN/nginx/active.conf"
  compose exec -T nginx nginx -t -q
  compose exec -T nginx nginx -s reload
fi

log "dry run against the Let's Encrypt staging CA"
compose exec -T certbot certbot "${request[@]}" --dry-run
log "requesting the certificate (kept if it is valid and not yet due for renewal)"
compose exec -T certbot certbot "${request[@]}" --keep-until-expiring

ln -sfn https.conf "$BRAIN/nginx/active.conf"
compose exec -T nginx nginx -t -q
compose exec -T nginx nginx -s reload
compose exec -T certbot certbot certificates --cert-name "$CERT_NAME"
log "HTTPS is on for ${DOMAINS[*]}"
