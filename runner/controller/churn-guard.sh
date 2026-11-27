#!/usr/bin/env bash
# Churn guard for canary runs: reads VM names (any whitespace separation) on stdin, remembers every distinct
# name in $SEEN_FILE, and runs $STOP_CMD and exits 1 as soon as more than $MAX_VMS (default 1) distinct VMs
# were ever seen. bash only: the old inline guard looped over an unquoted string, which zsh does not split,
# so it only ever saw the first VM.
set -u
SEEN_FILE=${SEEN_FILE:?}; MAX_VMS=${MAX_VMS:-1}
touch "$SEEN_FILE"
{ cat "$SEEN_FILE"; tr -s ' \t:' '\n\n\n' | grep '^nbci-'; } | sort -u > "$SEEN_FILE.new" || true
mv "$SEEN_FILE.new" "$SEEN_FILE"
n=$(wc -l < "$SEEN_FILE" | tr -d ' ')
if [ "$n" -gt "$MAX_VMS" ]; then
  echo "ALERT $n VMs created (max $MAX_VMS): $(tr '\n' ' ' < "$SEEN_FILE")"
  eval "${STOP_CMD:-true}"
  exit 1
fi
