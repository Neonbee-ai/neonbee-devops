#!/usr/bin/env bash
# Dead-man check (root cron every 5 min on the controller VM): heartbeat older than 10 min ->
# alert (Pulse "[nbci] alerts" + Teams if set) + restart ONLY the nbci unit. Independent of the controller process.
# The GCP uptime check on https://<host>/healthz covers the case where this VM itself is down.
set -u
HB=/var/lib/nbci/heartbeat; MAX=${NB_HB_MAX_S:-600}
age=$(( $(date +%s) - $(cat "$HB" 2>/dev/null || echo 0) ))
[ "$age" -lt "$MAX" ] && exit 0
[ -f /run/nbci-stopped-by-guard ] && exit 0   # the churn guard stopped nbci on purpose: no auto-restart (cleared on reboot or by hand)
set -a; . /etc/nbci.env; set +a
python3 /opt/nbci/src/runner/controller/nbci_alert.py "controller heartbeat ${age}s old: restarting nbci" >>/var/log/nbci-watchdog.log 2>&1
systemctl restart nbci
