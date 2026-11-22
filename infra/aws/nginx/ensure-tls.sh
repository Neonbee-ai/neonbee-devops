#!/usr/bin/env bash
# ensure-tls.sh - run by nb-deploy.sh (post-extract hook) before `nginx -t`.
# The generated sites reference the same origin cert paths as prod:
#   /etc/ssl/cloudflare/neonbee.app.{crt,key}      (api/dashboard/sso/cdn/... *.neonbee.app)
#   /etc/ssl/certs/neonbee-store.crt + /etc/ssl/private/neonbee-store.key  (*.neonbee.store)
# For each missing pair: read Secrets Manager neonbee/prod/tls/<name> (JSON {"crt":..,"key":..}),
# else create a self-signed pair so nginx starts (Cloudflare "Full" accepts it, "Full (strict)"
# does not - store the real origin cert in the secret). Never prints key material.
set -uo pipefail
PREFIX=${NB_SECRET_PREFIX:-neonbee/prod}
REGION=${NB_REGION:-ap-south-1}
R=${NB_TLS_ROOT:-}   # tests only: alternate filesystem root
log() { printf '%s [ensure-tls] %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

ensure() {  # $1 secret-name  $2 crt path  $3 key path  $4 CN
  local name=$1 crt=$2 key=$3 cn=$4 json
  install -d -m 0755 "$(dirname "$crt")" && install -d -m 0700 "$(dirname "$key")"
  # The secret wins over whatever is installed (replaces the self-signed placeholder and rotations);
  # a pair is only swapped in when the key matches the cert, so a bad secret never breaks nginx.
  if json=$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "$PREFIX/tls/$name" \
      --query SecretString --output text 2>/dev/null) && [ -n "$json" ] \
     && printf '%s' "$json" | jq -er '.crt' >"$crt.tmp" 2>/dev/null \
     && printf '%s' "$json" | jq -er '.key' >"$key.tmp" 2>/dev/null; then
    if [ -s "$crt" ] && cmp -s "$crt.tmp" "$crt" && cmp -s "$key.tmp" "$key"; then
      rm -f "$crt.tmp" "$key.tmp"; return 0
    fi
    if [ "$(openssl x509 -noout -pubkey -in "$crt.tmp" 2>/dev/null | openssl sha256)" = \
         "$(openssl pkey -pubout -in "$key.tmp" 2>/dev/null | openssl sha256)" ] \
       && openssl x509 -noout -in "$crt.tmp" >/dev/null 2>&1; then
      chmod 0644 "$crt.tmp"; chmod 0600 "$key.tmp"; mv -f "$crt.tmp" "$crt"; mv -f "$key.tmp" "$key"
      log "installed $name origin cert from Secrets Manager"
      return 0
    fi
    log "WARNING: $PREFIX/tls/$name key does not match its cert - ignored"
  fi
  rm -f "$crt.tmp" "$key.tmp"
  if [ -s "$crt" ] && [ -s "$key" ]; then return 0; fi
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=$cn" \
    -addext "subjectAltName=DNS:$cn,DNS:*.${cn#\*.}" -keyout "$key" -out "$crt" >/dev/null 2>&1 \
    || { log "cannot create self-signed cert for $name"; return 1; }
  chmod 0644 "$crt"; chmod 0600 "$key"
  log "WARNING: no $PREFIX/tls/$name secret - self-signed placeholder installed"
}

ensure neonbee-app "$R"/etc/ssl/cloudflare/neonbee.app.crt "$R"/etc/ssl/cloudflare/neonbee.app.key '*.neonbee.app' || exit 1
ensure neonbee-store "$R"/etc/ssl/certs/neonbee-store.crt "$R"/etc/ssl/private/neonbee-store.key '*.neonbee.store' || exit 1
