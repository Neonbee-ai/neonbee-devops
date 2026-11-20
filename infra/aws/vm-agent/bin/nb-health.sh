#!/usr/bin/env bash
# nb-health.sh - run every 10 s by nb-health.timer.
# Writes /var/lib/nb-agent/health.json and creates /var/lib/nb-agent/www/__lb_health
# only when the CRITICAL set is healthy (nginx + every catalog service with
# "critical": true answers its health_path). nginx serves that file at /__lb_health
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
  while IFS=$'\t' read -r name type port path critical; do
    [ "$type" = nginx ] && continue
    [ -n "$port" ] || continue
    if ! curl -fsS -m 3 -o /dev/null "http://127.0.0.1:$port$path"; then
      DOWN+=("$name")
      [ "$critical" = true ] && { OK=false; REASONS+=("$name down"); }
    fi
  done < <(jq -r '.services[] | [.name, (.type // "node"), (.port // "" | tostring), (.health_path // "/health"), ((.critical // false) | tostring)] | @tsv' "$NB_STATE/services.json")
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
