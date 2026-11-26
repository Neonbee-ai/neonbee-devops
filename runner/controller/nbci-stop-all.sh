#!/usr/bin/env bash
# ONE command to stop nb-ci everywhere (run from any machine with gcloud + gh logged in):
#   nbci-stop-all.sh            stop the controller VM (loop, guard, nb-ctl runners), delete every nbci job VM,
#                               every aqa-* VM and every unattached nbci disk. Queued nb-auto jobs wait.
#   nbci-stop-all.sh --legacy   the same, then flip every repo with NB_CI_MODE=auto to legacy (old runners take over).
#   nbci-stop-all.sh --dry      print what would happen.
# Undo: gcloud compute instances start nbci-controller (and set NB_CI_MODE=auto again on the repos).
set -uo pipefail
G=${GCLOUD:-gcloud}; P=${GCP_PROJECT:-gen-lang-client-0521938031}; ORG=${ORG:-Neonbee-ai}
LEGACY=0; DRY=0
for a in "$@"; do case $a in --legacy) LEGACY=1 ;; --dry) DRY=1 ;; esac; done
run() { echo "+ $*"; [ $DRY = 1 ] || "$@"; }
run "$G" compute instances stop nbci-controller --zone us-central1-a --project "$P" --quiet
for nz in $("$G" compute instances list --project "$P" --filter='labels.nbci=1 OR name~^aqa-' --format='value[separator=@](name,zone.basename())'); do
  run "$G" compute instances delete "${nz%@*}" --zone "${nz#*@}" --project "$P" --quiet
done
for nz in $("$G" compute disks list --project "$P" --filter='labels.nbci=1 AND -users:*' --format='value[separator=@](name,zone.basename())'); do
  run "$G" compute disks delete "${nz%@*}" --zone "${nz#*@}" --project "$P" --quiet
done
if [ $LEGACY = 1 ]; then
  for r in $(gh repo list "$ORG" --limit 300 --no-archived --json name --jq '.[].name'); do
    v=$(gh variable get NB_CI_MODE -R "$ORG/$r" 2>/dev/null || true)
    [ "$v" = auto ] && run gh variable set NB_CI_MODE -R "$ORG/$r" --body legacy
  done
fi
echo "nb-ci stopped$([ $LEGACY = 1 ] && echo ', repos on legacy runners')."
