#!/bin/bash
# Offline test of scripts/burst/burst-up.sh against stub curl/openssl: the create request is a Spot VM that
# DELETES on preemption with a 3h max run, sized as asked, self-delete SA, runner labels; boot timeout deletes
# the VM; reap writes a cost estimate and deregisters runners only once the VM is gone.
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd); T=${KEEP_T:-$(mktemp -d)}; [ -n "${KEEP_T:-}" ] || trap 'rm -rf "$T"' EXIT
pass=0; fail=0
eq() { if [ "$2" = "$3" ]; then pass=$((pass + 1)); else fail=$((fail + 1)); echo "FAIL [$1] got '$2' want '$3'"; fi; }
mkdir -p "$T/bin"
cat > "$T/bin/openssl" <<'S'
#!/bin/bash
case "$1" in base64) base64 | tr -d '\n' ;; dgst) cat >/dev/null; printf sig ;; esac
S
cat > "$T/bin/curl" <<'S'
#!/bin/bash
m=GET; data=; for ((i = 1; i <= $#; i++)); do a=${!i}; n=$((i + 1))
  case "$a" in -X) m=${!n} ;; -d|--data-binary) data=${!n} ;; esac; done
url=; for a in "$@"; do case "$a" in http*) url=$a ;; esac; done; [ "$data" = @- ] && data=$(cat)
echo "$m $url" >> "$S/calls"
case "$m $url" in
  *oauth2.googleapis.com/token*) echo '{"access_token":"gt"}' ;;
  "GET "*/instances/neonbee-ci-burst) cat "$S/vm" 2>/dev/null || echo '{"error":{"code":404}}' ;;
  "POST "*/instances) printf '%s' "$data" > "$S/insert"; echo '{"status":"RUNNING"}' > "$S/vm"; echo '{"name":"op1"}' ;;
  "DELETE "*/instances/neonbee-ci-burst) rm -f "$S/vm"; touch "$S/deleted"; echo '{"name":"op2"}' ;;
  *"/operations/"*) echo '{"status":"DONE"}' ;;
  *"/operations?"*) echo '{"items":[{"operationType":"insert","insertTime":"2026-11-10T10:00:00.000-08:00","endTime":"2026-11-10T10:00:40-08:00"},{"operationType":"delete","insertTime":"2026-11-10T10:41:00-08:00","endTime":"2026-11-10T10:41:30.5-08:00"}]}' ;;
  *cloudbilling*) echo '{"skus":[{"description":"Spot Preemptible T2D AMD Instance Core running in Belgium","serviceRegions":["europe-west1"],"pricingInfo":[{"pricingExpression":{"tieredRates":[{"unitPrice":{"units":"0","nanos":10000000}}]}}]},{"description":"Spot Preemptible T2D AMD Instance Ram running in Belgium","serviceRegions":["europe-west1"],"pricingInfo":[{"pricingExpression":{"tieredRates":[{"unitPrice":{"units":"0","nanos":1000000}}]}}]}]}' ;;
  "GET "*/actions/runners?*) cat "$S/runners" 2>/dev/null || { [ -f "$S/vm" ] && [ -z "${NO_ONLINE:-}" ] &&
      echo '{"runners":[{"id":5,"name":"gcp-burst-01","status":"online","labels":[{"name":"t2d-standard-4"}]}]}' || echo '{"runners":[]}'; } ;;
  "DELETE "*/actions/runners/*) touch "$S/runner-deleted" ;;
  *registration-token) echo '{"token":"REG"}' ;;
  *"/actions/runs?head_sha="*) echo '{"workflow_runs":[{"id":1,"status":"queued"}]}' ;;
  *"/runs/1/jobs"*) echo '{"jobs":[{"status":"queued"},{"status":"queued"},{"status":"queued"},{"status":"completed"}]}' ;;
  *"/orgs/Neonbee-ai/repos"*) echo '[]' ;;
  *"burst-on-label.yml/runs"*) echo '{"workflow_runs":[{"display_title":"burst up auto Neonbee-ai/so360-crm-fe@abc1234"}]}' ;;
  *"/commits/abc1234/pulls") echo '[{"number":9}]' ;;
  "POST "*/issues/9/comments) printf '%s' "$data" > "$S/comment"; echo '{}' ;;
  *) echo "{}" ;;
esac
S
chmod +x "$T/bin/"*
run() { # mode [env...]
  local mode=$1; shift; rm -rf "$T/s"; mkdir -p "$T/s"; [ -n "${PRE:-}" ] && eval "$PRE"
  env -i PATH="$T/bin:/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin" S="$T/s" GITHUB_STEP_SUMMARY="$T/s/sum" \
    GCP_BURST_SA_KEY='{"private_key":"k","client_email":"nb-burst-ci@p.iam.gserviceaccount.com"}' NB_BURST_GH_TOKEN=gh \
    NB_BURST_JUMP_KEY=key NB_BURST_JUMP_KNOWN='207.180.217.124 ssh-ed25519 AAAA' BURST_ONLINE_WAIT=15 "$@" \
    bash "$ROOT/scripts/burst/burst-up.sh" "$mode" > "$T/s/out" 2>&1; r=$?; cp "$T/s/out" "$T/last.out"; echo $r
}
j() { jq -r "$1" "$T/s/insert"; }

eq up-rc "$(run up SIZE=auto REPO=Neonbee-ai/so360-crm-fe SHA=abc1234)" 0
eq auto-sizes-to-pending-jobs "$(j .machineType)" "zones/europe-west1-b/machineTypes/t2d-standard-4"
eq spot "$(j '.scheduling | "\(.provisioningModel) \(.instanceTerminationAction) \(.maxRunDuration.seconds) \(.automaticRestart)"')" "SPOT DELETE 10800 false"
eq disk "$(j '.disks[0] | "\(.autoDelete) \(.initializeParams.diskSizeGb) \(.initializeParams.diskType)"')" "true 39 zones/europe-west1-b/diskTypes/pd-balanced"
eq image "$(j .disks[0].initializeParams.sourceImage)" "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64"
eq self-sa "$(j .serviceAccounts[0].email)" "nb-burst-self@gen-lang-client-0521938031.iam.gserviceaccount.com"
md() { jq -r --arg k "$1" '.metadata.items[] | select(.key == $k) | .value' "$T/s/insert"; }
eq md-count "$(md runner-count)" 3
eq md-labels "$(md runner-labels)" "lane-burst,nb-pool,t2d-standard-4"
eq md-token "$(md reg-token)" REG
eq md-startup "$(md startup-script | head -1)" '#!/bin/bash'
eq md-jump-has-host "$(md nb-jump | grep -c 'ProxyJump burstjump@207.180.217.124')" 1
eq size-in-summary "$(grep -c 'Burst size: \*\*t2d-standard-4\*\*, 3 runners' "$T/s/sum")" 1

eq preset-rc "$(run up SIZE=xl)" 0
eq preset-xl "$(j .machineType | sed 's|.*/||') $(md runner-count)" "t2d-standard-32 20"
eq override-rc "$(run up SIZE=auto MACHINE=t2d-standard-8 RUNNERS=5)" 0
eq override "$(j .machineType | sed 's|.*/||') $(md runner-count)" "t2d-standard-8 5"

PRE='echo "{\"status\":\"RUNNING\"}" > "$T/s/vm"'
eq already-up-rc "$(run up SIZE=m)" 0
eq already-up-no-insert "$([ -f "$T/s/insert" ] && echo yes || echo no)" no
PRE=
eq no-jump-key-fails-closed "$(run up SIZE=s NB_BURST_JUMP_KEY=)" 1
eq boot-timeout-rc "$(run up SIZE=s NO_ONLINE=1)" 1
eq boot-timeout-deletes-vm "$([ -f "$T/s/deleted" ] && echo yes)" yes

PRE='echo "{\"runners\":[{\"id\":5,\"name\":\"gcp-burst-01\",\"status\":\"offline\",\"labels\":[{\"name\":\"t2d-standard-16\"}]}]}" > "$T/s/runners"'
eq reap-rc "$(run reap)" 0
eq reap-deregisters "$([ -f "$T/s/runner-deleted" ] && echo yes)" yes
# 16 vCPU, 42 min (10:00:00 -> 10:41:30 ceil), disk 33 GB, catalog 0.01 core / 0.001 RAM
eq reap-cost-row "$(grep -c '| t2d-standard-16 | 16 | 1 | 33 GB | 42 min | Cloud Billing Catalog, europe-west1 Spot | \$0.160 |' "$T/s/sum")" 1
eq reap-pr-comment "$(jq -r .body "$T/s/comment" | grep -c 'est. \*\*\$0.160\*\*')" 1
PRE="$PRE"'; echo "{\"status\":\"RUNNING\"}" > "$T/s/vm"'
eq reap-live-vm-rc "$(run reap)" 0
eq reap-live-vm-keeps-runners "$([ -f "$T/s/runner-deleted" ] && echo yes || echo no)" no

echo "burst_up_test: $pass passed, $fail failed"; [ "$fail" = 0 ]
