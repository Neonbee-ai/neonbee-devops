#!/usr/bin/env bash
# Dead-man check (root cron every 5 min on the controller VM): heartbeat older than 10 min ->
# Teams alert + restart ONLY the nbci unit. Independent of the controller process.
set -u
HB=/var/lib/nbci/heartbeat; MAX=${NB_HB_MAX_S:-600}
age=$(( $(date +%s) - $(cat "$HB" 2>/dev/null || echo 0) ))
[ "$age" -lt "$MAX" ] && exit 0
P=$(curl -sf -m 5 -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/project/project-id)
TOK=$(curl -sf -m 5 -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token | jq -r .access_token)
HOOK=$(curl -sf -m 10 -H "Authorization: Bearer $TOK" "https://secretmanager.googleapis.com/v1/projects/$P/secrets/nbci-teams-webhook/versions/latest:access" | jq -r .payload.data | base64 -d 2>/dev/null)
[ -n "$HOOK" ] && curl -fsS -m 10 -H 'Content-Type: application/json' \
  -d "{\"text\":\"[nb-ci] controller heartbeat ${age}s old: restarting nbci\"}" "$HOOK" >/dev/null
systemctl restart nbci
