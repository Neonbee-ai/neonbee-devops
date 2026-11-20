#!/usr/bin/env bash
# nb-health.sh - run every 10 s by nb-health.timer.
# Writes /var/lib/nb-agent/health.json and creates /var/lib/nb-agent/www/__lb_health
# only when the CRITICAL set is healthy (nginx + every catalog service with
# "critical": true answers its health_path). Node apps are checked on 127.0.0.1:<port>;
# critical static nginx bundles (shell, SSO) are fetched through local nginx with
# Host: <server_name> (health_path default /index.html; a critical static entry
# without server_name fails the node). nginx serves that file at /__lb_health
# (vm-agent/nginx/nb-lb-health.conf), which is what the Cloudflare monitor and the
# cf-lb-sync Lambda check. A non-critical service being down does not fail the node:
# PM2 restarts it and the problem shows in health.json / CloudWatch logs.
set -uo pipefail
NB_COMPONENT=nb-health
# shellcheck source=nb-lib.sh
. "$(dirname "$0")/nb-lib.sh"

OK=true; REASONS=(); DOWN=()
FLAG="$NB_STATE/www/__lb_health"

[ -f "$NB_STATE/boot.done" ] || { OK=false; REASONS+=("booting"); }
[ -f "$NB_STATE/drain" ] && { OK=false; REASONS+=("draining"); }
systemctl is-active --quiet nginx || { OK=false; REASONS+=("nginx down"); }

if [ -f "$NB_STATE/services.json" ]; then
  # \x1f (not tab) as separator: tab is IFS whitespace, so an empty port would shift fields.
  while IFS=$'\x1f' read -r name type port path critical static host; do
    if [ "$type" = nginx ]; then
      # Only critical static bundles are probed; nb-nginx-sites = nginx itself (above).
      [ "$static" = true ] && [ "$critical" = true ] || continue
      if [ -z "$host" ]; then
        OK=false; DOWN+=("$name"); REASONS+=("$name has no server_name"); continue
      fi
      url="http://127.0.0.1:${NB_NGINX_PORT:-80}$path"
      hdr=(-H "Host: $host")
    else
      [ -n "$port" ] || continue
      url="http://127.0.0.1:$port$path"
      hdr=()
    fi
    if ! curl -fsS -m 3 -o /dev/null "${hdr[@]+"${hdr[@]}"}" "$url"; then
      DOWN+=("$name")
      [ "$critical" = true ] && { OK=false; REASONS+=("$name down"); }
    fi
  done < <(jq -r '.services[] | [.name, (.type // "node"), (.port // "" | tostring), (.health_path // (if (.type // "node") == "nginx" then "/index.html" else "/health" end)), ((.critical // false) | tostring), ((.static // false) | tostring), (.server_name // "")] | join("\u001f")' "$NB_STATE/services.json")
else
  OK=false; REASONS+=("no catalog")
fi

if $OK; then
  printf 'ok\n' > "$FLAG.tmp" && mv -f "$FLAG.tmp" "$FLAG"
else
  rm -f "$FLAG"
fi

to_json() { if [ "$#" -gt 0 ]; then printf '%s\n' "$@" | jq -R . | jq -sc .; else echo '[]'; fi; }
jq -n --argjson ok "$OK" --arg at "$(date -u +%FT%TZ)" \
  --argjson reasons "$(to_json "${REASONS[@]+"${REASONS[@]}"}")" \
  --argjson down "$(to_json "${DOWN[@]+"${DOWN[@]}"}")" \
  '{ok: $ok, at: $at, reasons: $reasons, down: $down}' \
  > "$NB_STATE/health.json.tmp" && mv -f "$NB_STATE/health.json.tmp" "$NB_STATE/health.json"

$OK
