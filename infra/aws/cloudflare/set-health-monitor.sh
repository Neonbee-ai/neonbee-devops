#!/usr/bin/env bash
# Stage 2.6 - tighten the Cloudflare LB health monitor for the AWS pool.
# DRY RUN by default: prints the request. Pass --apply to send it.
# Today's monitor (neonbee-cf-lb-setup.yml): GET / every 60 s, 2 retries, 5 s timeout.
# Target: GET /__lb_health (nb-agent flag) with a shorter interval if the plan allows it.
# The minimum interval depends on the Cloudflare plan / LB add-on; if the API rejects
# the value it returns an error and nothing changes - lower INTERVAL step by step.
#
# Usage:
#   CF_ACCOUNT_ID=<32 hex> CF_MONITOR_ID=<32 hex> CF_LB_API_TOKEN=<token> \
#   INTERVAL=15 RETRIES=2 TIMEOUT=5 ./set-health-monitor.sh [--apply]
set -euo pipefail
: "${CF_ACCOUNT_ID:?set CF_ACCOUNT_ID}"
: "${CF_MONITOR_ID:?set CF_MONITOR_ID}"
INTERVAL=${INTERVAL:-15}
RETRIES=${RETRIES:-2}
TIMEOUT=${TIMEOUT:-5}
HEALTH_PATH=${HEALTH_PATH:-/__lb_health}
HOST_HEADER=${HOST_HEADER:-api.neonbee.app}
EXPECTED=${EXPECTED:-200}
CONSECUTIVE_UP=${CONSECUTIVE_UP:-2}
CONSECUTIVE_DOWN=${CONSECUTIVE_DOWN:-2}

[[ $INTERVAL =~ ^[0-9]+$ && $RETRIES =~ ^[0-9]+$ && $TIMEOUT =~ ^[0-9]+$ ]] || { echo "numbers only" >&2; exit 2; }
[ "$TIMEOUT" -lt "$INTERVAL" ] || { echo "TIMEOUT must be < INTERVAL" >&2; exit 2; }

BODY=$(cat <<JSON
{"type":"http","method":"GET","path":"$HEALTH_PATH","expected_codes":"$EXPECTED",
 "header":{"Host":["$HOST_HEADER"]},"interval":$INTERVAL,"retries":$RETRIES,"timeout":$TIMEOUT,
 "consecutive_up":$CONSECUTIVE_UP,"consecutive_down":$CONSECUTIVE_DOWN,
 "follow_redirects":false,"allow_insecure":false,"description":"neonbee-aws-nodes"}
JSON
)
URL="https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT_ID/load_balancers/monitors/$CF_MONITOR_ID"
echo "PATCH $URL"
echo "$BODY"
echo "Worst-case failover ~= interval x (retries+1) x consecutive_down = $((INTERVAL * (RETRIES + 1) * CONSECUTIVE_DOWN)) s"
if [ "${1:-}" != "--apply" ]; then echo "(dry run - pass --apply to send)"; exit 0; fi
: "${CF_LB_API_TOKEN:?set CF_LB_API_TOKEN}"
curl -fsS -X PATCH "$URL" -H "Authorization: Bearer $CF_LB_API_TOKEN" \
  -H 'Content-Type: application/json' --data "$BODY" | jq '{success, errors, interval: .result.interval, path: .result.path}'
