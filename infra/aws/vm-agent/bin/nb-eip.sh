#!/usr/bin/env bash
# nb-eip.sh [boot|reconcile] - keep this node on a pool Elastic IP that serves traffic.
#
# Pool EIPs carry tag nb-pool=<env>. The ones Cloudflare points at (LB pool origins and the
# proxied A records) also carry nb-serving=true; the rest are spares. Origins are pinned to
# IPs, so a replacement node must take over a SERVING address, never a random spare:
#   boot       node holds no pool EIP: claim a free serving EIP, else a free spare.
#   reconcile  node holds a spare (or nothing): move to a free serving EIP when one frees up
#              (rolling instance refresh launches before it terminates, so the old node
#              still holds the serving EIP while the new one boots). Run by nb-health.sh.
# A node that already holds a serving EIP is never moved. Associating a new EIP replaces
# the node's current one (AWS disassociates it); --no-allow-reassociation means a serving
# EIP held by another node is never taken from it.
set -uo pipefail
NB_COMPONENT=nb-eip
# shellcheck source=nb-lib.sh
. "$(dirname "$0")/nb-lib.sh"
MODE=${1:-boot}
AWSCLI=${NB_AWS_CLI:-aws}
# reconcile runs inside nb-health (TimeoutStartSec=30): one quick round there.
ROUNDS=3; T=20; [ "$MODE" = reconcile ] && { ROUNDS=1; T=8; }

# Print "<state> <allocation ids...>" choices from describe-addresses JSON on stdin.
# $1 instance id. Lines: "mine serving|spare|none", then "serving <id>" / "spare <id>" for free ones.
nb_eip_plan() {
  jq -r --arg i "$1" '
    def serving: any((.Tags // [])[]; .Key == "nb-serving" and .Value == "true");
    ([.Addresses[] | select(.InstanceId == $i)] | if length == 0 then "mine none"
      elif any(.[]; serving) then "mine serving" else "mine spare" end),
    (.Addresses[] | select(.AssociationId == null) | (if serving then "serving " else "spare " end) + .AllocationId)'
}

iid=$(nb_imds instance-id) || { nb_log "eip: no instance id (continuing)"; exit 0; }
for round in $(seq 1 "$ROUNDS"); do
  json=$(timeout "$T" "$AWSCLI" ec2 describe-addresses --filters "Name=tag-key,Values=nb-pool" --output json 2>/dev/null) \
    || { sleep $((round * 3)); continue; }
  plan=$(printf '%s' "$json" | nb_eip_plan "$iid") || { sleep $((round * 3)); continue; }
  mine=$(printf '%s\n' "$plan" | awk '$1 == "mine" {print $2}')
  [ "$mine" = serving ] && exit 0
  cands=$(printf '%s\n' "$plan" | awk '$1 == "serving" {print $2}' | shuf)
  if [ "$MODE" = boot ] && [ "$mine" = none ]; then
    cands="$cands"$'\n'$(printf '%s\n' "$plan" | awk '$1 == "spare" {print $2}' | shuf)
  fi
  [ -n "${cands//[$'\n ']/}" ] || { [ "$MODE" = reconcile ] && exit 0; sleep $((round * 3)); continue; }
  for id in $cands; do
    if timeout "$T" "$AWSCLI" ec2 associate-address --instance-id "$iid" --allocation-id "$id" \
         --no-allow-reassociation >/dev/null 2>&1; then
      nb_log "eip: associated $id (mode=$MODE, was=$mine)"; exit 0
    fi
  done
  sleep $((round * 3))
done
[ "$MODE" = boot ] && nb_log "eip: no free pool address (continuing with the auto-assigned IP)"
exit 0
