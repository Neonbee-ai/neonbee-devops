#!/usr/bin/env bash
# =============================================================================
# One-time setup of a preview host. Run as root ON the preview host from a
# checkout of neonbee-devops:
#
#   DEV_ORIGIN=84.247.131.148 PROD_SUPABASE_HOST=<prod-ref>.supabase.co \
#     bash preview/bootstrap-host.sh
#
# Idempotent. Native nginx + pm2 only (no Docker). Never run it on the Dev VM
# (it hosts the Prod HA processes) or on any prod host.
# =============================================================================
set -euo pipefail

: "${DEV_ORIGIN:?set DEV_ORIGIN (the Dev VM IP that serves dev-cdn / dev-api / dev-dashboard)}"
: "${PROD_SUPABASE_HOST:?set PROD_SUPABASE_HOST (preview backends refuse any .env containing it)}"
HERE=$(cd "$(dirname "$0")" && pwd)

if [ "$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -cxF "$DEV_ORIGIN")" != 0 ]; then
  echo "refusing: this is the Dev VM ($DEV_ORIGIN) — previews must run elsewhere" >&2
  exit 1
fi

command -v apt-get >/dev/null && apt-get install -y -q jq nginx rsync curl util-linux >/dev/null
# nvm installs are not on the PATH of a non-interactive ssh session; record
# the node bin dir in preview.env so preview-ctl always finds node + pm2.
NODE_BIN=${NODE_BIN:-$(dirname "$(command -v node 2>/dev/null || ls -d /root/.nvm/versions/node/*/bin/node 2>/dev/null | tail -1)")}
[ -x "$NODE_BIN/node" ] || { echo "install Node 22 first (nvm or nodesource), or set NODE_BIN" >&2; exit 1; }
export PATH="$NODE_BIN:$PATH"
command -v pm2  >/dev/null || npm install -g pm2
pm2 startup systemd -u root --hp /root >/dev/null 2>&1 || true

install -d -m 755 /srv/previews /srv/previews/_incoming /srv/previews/_deps /etc/so360-preview /etc/nginx/snippets
install -m 755 "$HERE/bin/preview-ctl" /usr/local/bin/preview-ctl
install -m 644 "$HERE"/nginx/*.conf /etc/nginx/snippets/

if [ ! -f /etc/so360-preview/preview.env ]; then
  cat > /etc/so360-preview/preview.env <<EOF
PATH=$NODE_BIN:\$PATH
DEV_ORIGIN=$DEV_ORIGIN
PROD_SUPABASE_HOST=$PROD_SUPABASE_HOST
# PREVIEW_ROOT=/srv/previews
# PREVIEW_SSL_CERT=/etc/ssl/cloudflare/skyoffice360.com.crt
# PREVIEW_SSL_KEY=/etc/ssl/cloudflare/skyoffice360.com.key
# PREVIEW_PORT_MIN=7100
# PREVIEW_PORT_MAX=7999
# PREVIEW_PM2_MEM=300M
EOF
  chmod 600 /etc/so360-preview/preview.env
fi

. /etc/so360-preview/preview.env
CRT=${PREVIEW_SSL_CERT:-/etc/ssl/cloudflare/skyoffice360.com.crt}
KEY=${PREVIEW_SSL_KEY:-/etc/ssl/cloudflare/skyoffice360.com.key}
if [ ! -s "$CRT" ] || [ ! -s "$KEY" ]; then
  echo "missing Cloudflare origin cert for *.skyoffice360.com at $CRT / $KEY" >&2
  echo "copy it from the Dev VM (same paths) and re-run" >&2
  exit 1
fi

systemctl enable nginx >/dev/null 2>&1 || true
preview-ctl render
echo "preview host ready — add its IP as the PREVIEW_HOST org secret"
