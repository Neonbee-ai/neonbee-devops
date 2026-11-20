#!/usr/bin/env bash
# nb-boot.sh - run once per boot by nb-boot.service (enabled from user data).
# 1. start the CloudWatch agent with the config in SSM
# 2. fetch the service catalog (s3://<bucket>/agent/services.json)
# 3. for every catalog service read s3://<bucket>/<svc>/current.json and deploy that
#    sha with nb-deploy.sh (same path the deploy workflow uses), nginx sites first
# 4. mark boot done; nb-health.sh then decides whether /__lb_health is served.
# The node never serves traffic until the cf-lb-sync Lambda sees /__lb_health = 200.
set -uo pipefail
NB_COMPONENT=nb-boot
# shellcheck source=nb-lib.sh
. "$(dirname "$0")/nb-lib.sh"

rm -f "$NB_STATE/boot.done" "$NB_STATE/www/__lb_health"
: "${NB_RELEASE_BUCKET:?NB_RELEASE_BUCKET missing in $NB_CONF}"

if [ -n "${NB_CWAGENT_PARAM:-}" ] && [ -x /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl ]; then
  /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 \
    -c "ssm:$NB_CWAGENT_PARAM" -s >/dev/null || nb_log "cloudwatch agent start failed (continuing)"
fi

for i in 1 2 3 4 5; do nb_fetch_catalog && break; sleep $((i * 5)); done
[ -f "$NB_STATE/services.json" ] || nb_die "cannot fetch service catalog"

nb_pm2 ping >/dev/null 2>&1 || true

FAILED=()
# nginx-type entries first so sites exist before apps report healthy.
mapfile -t SERVICES < <(jq -r '.services | sort_by(if .type == "nginx" then 0 else 1 end) | .[].name' "$NB_STATE/services.json")
for SVC in "${SERVICES[@]}"; do
  PTR=$(aws s3 cp "s3://$NB_RELEASE_BUCKET/$SVC/current.json" - --only-show-errors 2>/dev/null) || {
    nb_log "$SVC has no current.json yet - skipped"; continue; }
  SHA=$(printf '%s' "$PTR" | jq -r '.sha // empty'); KEY=$(printf '%s' "$PTR" | jq -r '.key // empty')
  if [ -z "$SHA" ] || [ -z "$KEY" ]; then nb_log "$SVC current.json malformed"; FAILED+=("$SVC"); continue; fi
  if [ -f "$NB_STATE/deployed/$SVC.json" ] && [ "$(jq -r .sha "$NB_STATE/deployed/$SVC.json")" = "$SHA" ] \
     && [ -L "$NB_ROOT/services/$SVC/current" ]; then
    nb_log "$SVC already at $SHA"; continue    # reboot of an existing node: PM2 resurrect handles it
  fi
  "$(dirname "$0")/nb-deploy.sh" "$SVC" "$SHA" "$NB_RELEASE_BUCKET" "$KEY" || FAILED+=("$SVC")
done

nb_pm2 save --force >/dev/null 2>&1 || true
touch "$NB_STATE/boot.done"
if [ "${#FAILED[@]}" -gt 0 ]; then
  nb_log "boot finished with failures: ${FAILED[*]}"
else
  nb_log "boot finished OK"
fi
"$(dirname "$0")/nb-health.sh" || true
exit 0
