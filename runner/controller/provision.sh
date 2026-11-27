#!/usr/bin/env bash
# One-time setup of the nb-ci controller (idempotent; run with gcloud as a project owner).
#   runner/controller/provision.sh plan    print what it would do (default)
#   runner/controller/provision.sh apply   do it
# Creates: custom role nbciController (least privilege), service account nbci-controller (NO key
# file: attached to the VM, tokens come from the metadata server), 4 Secret Manager secrets with
# per-secret accessor bindings (values are added by hand, never by this script), firewall 80/443 for
# the webhook receiver, and the always-on e2-micro `nbci-controller` in us-central1-a (free tier:
# 1 e2-micro + 30 GB standard PD in us-central1/us-west1/us-east1). Its external IPv4 is NOT free:
# SKU "External IP Charge on a Standard VM" = $0.005/h = $3.65/month.
set -euo pipefail
P=${GCP_PROJECT:-gen-lang-client-0521938031}; Z=${ZONE:-us-central1-a}
G=${GCLOUD:-/opt/homebrew/share/google-cloud-sdk/bin/gcloud}
SA=nbci-controller@$P.iam.gserviceaccount.com
# nbci-ref: branch the controller checks out. feat/ci-autoscaler until the code is on main, then main.
REF=${NBCI_REF:-feat/ci-autoscaler}
MODE=${1:-plan}
run() { echo "+ $*"; [ "$MODE" = apply ] && "$@" || true; }
PERMS=compute.instances.create,compute.instances.delete,compute.instances.get,compute.instances.list,compute.instances.setMetadata,compute.instances.setLabels,compute.instances.setScheduling,compute.disks.create,compute.disks.delete,compute.disks.get,compute.disks.list,compute.disks.use,compute.disks.useReadOnly,compute.disks.setLabels,compute.images.create,compute.images.delete,compute.images.get,compute.images.list,compute.images.getFromFamily,compute.images.useReadOnly,compute.images.setLabels,compute.subnetworks.use,compute.subnetworks.useExternalIp,compute.networks.use,compute.zoneOperations.get,compute.globalOperations.get,compute.regions.get,compute.projects.get,compute.machineTypes.get

run "$G" services enable compute.googleapis.com secretmanager.googleapis.com --project "$P"
if "$G" iam roles describe nbciController --project "$P" >/dev/null 2>&1; then
  run "$G" iam roles update nbciController --project "$P" --permissions "$PERMS" --quiet
else
  run "$G" iam roles create nbciController --project "$P" --title "nb-ci controller" --permissions "$PERMS" --stage GA
fi
"$G" iam service-accounts describe "$SA" --project "$P" >/dev/null 2>&1 ||
  run "$G" iam service-accounts create nbci-controller --project "$P" --display-name "nb-ci controller (no keys)"
run "$G" projects add-iam-policy-binding "$P" --member "serviceAccount:$SA" --role "projects/$P/roles/nbciController" --condition=None --quiet
for s in nbci-github-app-key nbci-webhook-secret nbci-teams-webhook nbci-pulse-token; do
  "$G" secrets describe "$s" --project "$P" >/dev/null 2>&1 || run "$G" secrets create "$s" --project "$P" --replication-policy automatic
  run "$G" secrets add-iam-policy-binding "$s" --project "$P" --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor --quiet
done
"$G" compute firewall-rules describe nbci-webhook --project "$P" >/dev/null 2>&1 ||
  run "$G" compute firewall-rules create nbci-webhook --project "$P" --allow tcp:80,tcp:443 --target-tags nbci-controller \
    --description "GitHub App webhook -> Caddy (HMAC-verified) on the nb-ci controller"
"$G" compute instances describe nbci-controller --zone "$Z" --project "$P" >/dev/null 2>&1 ||
  run "$G" compute instances create nbci-controller --project "$P" --zone "$Z" --machine-type e2-micro \
    --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud --boot-disk-size 30GB --boot-disk-type pd-standard \
    --service-account "$SA" --scopes cloud-platform --tags nbci-controller --labels nb-role=controller \
    --metadata-from-file startup-script="$(dirname "$0")/bootstrap.sh" --metadata nbci-ref="$REF",nbci-dry-run=1
echo "Then add secret values: gcloud secrets versions add <name> --data-file=- (App key PEM, webhook secret, Teams URL, Pulse token)."
