#!/usr/bin/env bash
# ONE command to move the controller to another neonbee-devops commit (deploy or roll back), e.g.
#   nbci-set-ref.sh 1d23c94...   roll back to the pre-packing controller (main before Pulse 652ffcd8)
#   nbci-set-ref.sh main         back to main
# Sets instance metadata nbci-ref and re-runs bootstrap.sh over IAP (code checkout + restart of nbci/nbci-guard
# only; job VMs keep running). Note: a controller older than packing does not know packed VMs (label nb-slots)
# and deletes them as "no job after boot" within one pass, busy slots included: their jobs fail as infra.
set -euo pipefail
REF=${1:?usage: nbci-set-ref.sh <git ref or sha>}
G=${GCLOUD:-gcloud}; P=${GCP_PROJECT:-gen-lang-client-0521938031}; Z=${ZONE:-us-central1-a}
"$G" compute instances add-metadata nbci-controller --zone "$Z" --project "$P" --metadata "nbci-ref=$REF"
"$G" compute ssh nbci-controller --zone "$Z" --project "$P" --tunnel-through-iap --command \
  'sudo /opt/nbci/src/runner/controller/bootstrap.sh >/tmp/nbci-boot.log 2>&1; tail -3 /tmp/nbci-boot.log; sudo git -C /opt/nbci/src log -1 --oneline; systemctl is-active nbci nbci-guard'
