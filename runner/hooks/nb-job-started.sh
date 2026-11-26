#!/usr/bin/env bash
# Runner pre-job hook for the Mac Minis (and any long-lived runner): ACTIONS_RUNNER_HOOK_JOB_STARTED in
# each runner's .env. Keeps the disk from filling (2026-11-26: a Mini hit 120 MB free and went offline).
# Below MIN_GB free it cleans caches no running job needs; below HARD_GB it fails this job fast with a
# clear error instead of letting it (and the runner) die mid-build. Never touches another job's _work.
MIN_GB=${NB_DISK_MIN_GB:-15}; HARD_GB=${NB_DISK_HARD_GB:-5}
free_gb() { df -Pk "${RUNNER_WORKSPACE:-$HOME}" | awk 'NR==2{print int($4/1048576)}'; }
[ "$(free_gb)" -ge "$MIN_GB" ] && exit 0
echo "disk guard: $(free_gb) GB free < $MIN_GB, cleaning"
root=$(cd "${RUNNER_WORKSPACE:-.}/../.." 2>/dev/null && pwd)
[ -n "$root" ] && find "$root/_work/_temp" -mindepth 1 -maxdepth 1 -mmin +60 -exec rm -rf {} + 2>/dev/null
npm cache clean --force >/dev/null 2>&1
rm -rf "$HOME/Library/Caches/ms-playwright/.links" "$HOME/Library/Caches/npm" "$HOME/.cache/node-gyp" 2>/dev/null
echo "disk guard: $(free_gb) GB free after cleanup"
[ "$(free_gb)" -ge "$HARD_GB" ] || { echo "::error::runner $RUNNER_NAME disk full ($(free_gb) GB free): job stopped before it could fill the disk"; exit 1; }
