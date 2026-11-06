#!/usr/bin/env bash
# Contabo runner VM (207.180.217.124) daily cleanup.
# Deployed to /opt/actions-runner/cleanup.sh, run by root cron: 0 2 * * *
#
# Never touches a slot with a job in flight: a slot is skipped when its
# Runner.Worker is active, and the check is repeated before every delete
# so a job that starts mid-cleanup is left alone.
set -uo pipefail
LOG="/var/log/actions-runner-cleanup.log"
log() { echo "$(date '+%Y-%m-%dT%H:%M:%S') $*" >> "$LOG"; }
slot_busy() { pgrep -af '[R]unner[.]Worker' 2>/dev/null | grep -q "runner-$1/bin"; }

log "=== Runner cleanup started ==="
BEFORE=$(df -h / | awk 'NR==2{print $5" ("$4" free)"}')

for RDIR in /opt/actions-runner/runner-*; do
  i="${RDIR##*/runner-}"
  WORK="$RDIR/_work"
  [ -d "$WORK" ] || continue

  if slot_busy "$i"; then log "  runner-${i}: BUSY — skipped"; continue; fi

  # _temp: per-job scratch; nothing in an idle slot needs it.
  slot_busy "$i" || find "$WORK/_temp" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null

  # _actions: cached action versions (_actions/<owner>/<repo>/<ref>) older than 2 days.
  slot_busy "$i" || find "$WORK/_actions" -mindepth 3 -maxdepth 3 -type d -mtime +2 -exec rm -rf {} + 2>/dev/null

  # Stale build output in repo checkouts (_work/<repo>/<repo>/...), older than 2 days.
  for D in "$WORK"/*/; do
    case "${D%/}" in "$WORK"/_*) continue ;; esac
    slot_busy "$i" && break
    # -prune on every match so we never descend into a fresh node_modules.
    find "$D" -maxdepth 5 -type d \( -name node_modules -o -name dist \) -prune -mtime +2 -exec rm -rf {} + 2>/dev/null
  done

  # Repo checkouts no job has touched in a week; the runner re-clones on demand.
  STALE=0
  if ! slot_busy "$i"; then
    STALE=$(find "$WORK" -maxdepth 1 -mindepth 1 -type d -mtime +7 ! -name '_*' 2>/dev/null | wc -l)
    find "$WORK" -maxdepth 1 -mindepth 1 -type d -mtime +7 ! -name '_*' -exec rm -rf {} + 2>/dev/null
  fi
  log "  runner-${i}: cleaned, ${STALE} stale checkout(s) removed"
done

# Trim npm cache (keep 7 days)
find /opt/actions-runner/shared/npm-cache -maxdepth 3 -type f -mtime +7 -delete 2>/dev/null

# CI leftovers in /tmp, regenerated on demand: entries older than 1 day.
find /tmp/jest_rt -mindepth 1 -maxdepth 1 -mtime +1 -exec rm -rf {} + 2>/dev/null
find /tmp/node-compile-cache -mindepth 1 -maxdepth 1 -mtime +1 -exec rm -rf {} + 2>/dev/null

# The PM2 DAEMON log grows unbounded (13G once). App logs live in
# ~/.pm2/logs and are NOT touched. Truncate in place so the daemon's open
# file handle stays valid — deleting it would free nothing until restart.
PM2LOG="/root/.pm2/pm2.log"
if [ -f "$PM2LOG" ] && [ "$(stat -c %s "$PM2LOG")" -gt 524288000 ]; then
  tail -n 2000 "$PM2LOG" > "${PM2LOG}.recent" 2>/dev/null
  : > "$PM2LOG"
  log "  pm2.log truncated (was over 500M)"
fi

log "  Disk usage: ${BEFORE} -> $(df -h / | awk 'NR==2{print $5" ("$4" free)"}')"
log "=== Cleanup done ==="
