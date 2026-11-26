#!/usr/bin/env bash
# Idempotent: the laptop-free "controller down" + budget alert path.
#   uptime check https://$HOST/healthz (every 5 min, 3 regions) -> alert policy -> Pub/Sub topic nbci-alerts
#   project budget (50/90/100 %) ----------------------------------------------^
#   topic -> push subscription (OIDC) -> Cloud Run nbci-alert-relay (stock python image + relay.py in env) -> Pulse comment
# Needs: owner/editor on the project, billing.budgets.create on the billing account. Re-run after editing relay.py.
set -euo pipefail
G=${GCLOUD:-gcloud}; P=${GCP_PROJECT:-gen-lang-client-0521938031}; R=us-central1; HOST=${NBCI_HOST:-nbci.moonhive.in}
N=$($G projects describe $P --format='value(projectNumber)'); SA=nbci-alert-relay@$P.iam.gserviceaccount.com
B=nbci-alerts-${P##*-}; BUDGET_INR=${BUDGET_INR:-6000}; HERE=$(cd "$(dirname "$0")" && pwd)
$G services enable run.googleapis.com pubsub.googleapis.com monitoring.googleapis.com billingbudgets.googleapis.com --project $P
$G iam service-accounts describe $SA --project $P >/dev/null 2>&1 || $G iam service-accounts create nbci-alert-relay --project $P --display-name "nbci alert relay (Pub/Sub -> Pulse)"
$G secrets add-iam-policy-binding nbci-pulse-token --project $P --member serviceAccount:$SA --role roles/secretmanager.secretAccessor --format=none
$G storage buckets describe gs://$B >/dev/null 2>&1 || $G storage buckets create gs://$B --project $P --location $R --uniform-bucket-level-access --public-access-prevention
printf '{"rule":[{"action":{"type":"Delete"},"condition":{"age":60}}]}' > /tmp/nbci-lc.json && $G storage buckets update gs://$B --lifecycle-file=/tmp/nbci-lc.json && rm -f /tmp/nbci-lc.json
$G storage buckets add-iam-policy-binding gs://$B --member serviceAccount:$SA --role roles/storage.objectUser --format=none
$G pubsub topics describe nbci-alerts --project $P >/dev/null 2>&1 || $G pubsub topics create nbci-alerts --project $P
for m in serviceAccount:service-$N@gcp-sa-monitoring-notification.iam.gserviceaccount.com serviceAccount:billing-budget-alert@system.gserviceaccount.com; do
  $G pubsub topics add-iam-policy-binding nbci-alerts --project $P --member $m --role roles/pubsub.publisher --format=none || echo "WARN: could not grant $m"
done
python3 - "$HERE/relay.py" > /tmp/nbci-relay-env.yaml <<'PY'
import json, sys; print('APP: ' + json.dumps(open(sys.argv[1]).read())); print('TASK: "71b8be5b"'); print('PULSE_URL: "https://pulse.moonhive.in/mcp"')
PY
echo "BUCKET: \"$B\"" >> /tmp/nbci-relay-env.yaml
$G run deploy nbci-alert-relay --project $P --region $R --image docker.io/library/python:3.12-alpine \
  --command python --args='-c,import os;exec(os.environ["APP"])' --env-vars-file /tmp/nbci-relay-env.yaml \
  --set-secrets PULSE_TOKEN=nbci-pulse-token:latest --service-account $SA --no-allow-unauthenticated \
  --min-instances 0 --max-instances 1 --memory 256Mi --cpu 1 --timeout 60 --ingress all --quiet
rm -f /tmp/nbci-relay-env.yaml
$G run services add-iam-policy-binding nbci-alert-relay --project $P --region $R --member serviceAccount:$SA --role roles/run.invoker --format=none
$G iam service-accounts add-iam-policy-binding $SA --project $P --member serviceAccount:service-$N@gcp-sa-pubsub.iam.gserviceaccount.com --role roles/iam.serviceAccountTokenCreator --format=none
URL=$($G run services describe nbci-alert-relay --project $P --region $R --format='value(status.url)')
if $G pubsub subscriptions describe nbci-alerts-relay --project $P >/dev/null 2>&1; then
  $G pubsub subscriptions update nbci-alerts-relay --project $P --push-endpoint "$URL/" --push-auth-service-account $SA
else
  $G pubsub subscriptions create nbci-alerts-relay --project $P --topic nbci-alerts --push-endpoint "$URL/" --push-auth-service-account $SA \
    --ack-deadline 60 --message-retention-duration 1d --min-retry-delay 30s --max-retry-delay 600s
fi
# notification channel (Pub/Sub) + uptime check + policy
CH=$($G beta monitoring channels list --project $P --filter='displayName="nbci-alerts"' --format='value(name)' | head -1)
[ -n "$CH" ] || CH=$($G beta monitoring channels create --project $P --display-name nbci-alerts --type pubsub --channel-labels topic=projects/$P/topics/nbci-alerts --format='value(name)')
UC=$($G monitoring uptime list-configs --project $P --filter='displayName="nbci-controller-healthz"' --format='value(name)' | head -1)
[ -n "$UC" ] || UC=$($G monitoring uptime create nbci-controller-healthz --project $P --resource-type uptime-url \
  --resource-labels host=$HOST,project_id=$P --protocol https --path /healthz --port 443 --period 5 --timeout 10 \
  --regions usa-iowa,usa-virginia,europe --status-codes 200 --format='value(name)')
CID=${UC##*/}
if ! $G alpha monitoring policies list --project $P --filter='displayName="nbci controller down"' --format='value(name)' | grep -q .; then
  cat > /tmp/nbci-policy.json <<J
{"displayName":"nbci controller down","combiner":"OR","notificationChannels":["$CH"],
 "documentation":{"content":"https://$HOST/healthz failing from 2+ regions for 10 min: the nbci controller VM or its loop is down. Runbook: neonbee-devops runner/controller/RUNBOOK.md","mimeType":"text/markdown"},
 "alertStrategy":{"autoClose":"86400s"},
 "conditions":[{"displayName":"healthz uptime check failing","conditionThreshold":{
   "filter":"metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND metric.label.check_id=\"$CID\" AND resource.type=\"uptime_url\"",
   "aggregations":[{"alignmentPeriod":"600s","perSeriesAligner":"ALIGN_NEXT_OLDER","crossSeriesReducer":"REDUCE_COUNT_FALSE","groupByFields":["resource.label.project_id","resource.label.host"]}],
   "comparison":"COMPARISON_GT","thresholdValue":1,"duration":"600s","trigger":{"count":1}}}]}
J
  $G alpha monitoring policies create --project $P --policy-from-file /tmp/nbci-policy.json --format='value(name)'; rm -f /tmp/nbci-policy.json
fi
BA=$($G billing projects describe $P --format='value(billingAccountName)'); BA=${BA##*/}
if ! $G billing budgets list --billing-account $BA --billing-project $P --format='value(displayName)' | grep -qx 'nbci project budget'; then
  $G billing budgets create --billing-account $BA --billing-project $P --display-name 'nbci project budget' \
    --budget-amount ${BUDGET_INR}INR --calendar-period month --filter-projects projects/$P \
    --threshold-rule percent=0.5 --threshold-rule percent=0.9 --threshold-rule percent=1.0 \
    --notifications-rule-pubsub-topic projects/$P/topics/nbci-alerts
fi
echo "relay=$URL channel=$CH uptime=$CID"
