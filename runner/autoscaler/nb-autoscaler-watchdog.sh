#!/usr/bin/env bash
# Independent dead-man check (root cron on the ITVM, every 5 min): if the controller's heartbeat file is
# older than 10 min, alert Teams and restart ONLY the nb-autoscaler unit. Never touches pm2 or anything else.
set -u
HB=/var/lib/nb-autoscaler/heartbeat; MAX=${NB_HB_MAX_S:-600}
. /etc/nb-autoscaler.env 2>/dev/null || true
age=$(( $(date +%s) - $(cat "$HB" 2>/dev/null || echo 0) ))
[ "$age" -lt "$MAX" ] && exit 0
[ -n "${TEAMS_WEBHOOK:-}" ] && curl -fsS -m 10 -H 'Content-Type: application/json' \
  -d "{\"text\":\"[nb-autoscaler] heartbeat ${age}s old on $(hostname): restarting the unit\"}" "$TEAMS_WEBHOOK" >/dev/null
systemctl restart nb-autoscaler
