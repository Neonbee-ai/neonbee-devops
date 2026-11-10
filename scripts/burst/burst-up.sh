#!/bin/bash
# Label-driven GCP burst VM controller (run by .github/workflows/burst-on-label.yml on Contabo).
# Pure curl + jq + openssl: no gcloud.
#
#   burst-up.sh up     size, create neonbee-ci-burst (Spot, DELETE on preemption, 3h max run), register
#                      gcp-burst-01..N; deletes the VM again if no runner is online after BURST_ONLINE_WAIT s
#   burst-up.sh reap   once the VM is gone (idle self-delete, preemption, max-run): re-run jobs it lost,
#                      deregister gcp-burst-* runners, write the estimated cost (summary + PR comment)
#
# Env: GCP_BURST_SA_KEY (SA nb-burst-ci JSON key), NB_BURST_GH_TOKEN (org self-hosted runners r/w,
#      actions r/w + pull requests r/w on the org repos), NB_BURST_JUMP_KEY + NB_BURST_JUMP_KNOWN,
#      SIZE (auto|s|m|l|xl), REPO + SHA (who asked; auto size counts their pending jobs), MACHINE + RUNNERS
#      (manual overrides), GCP_PROJECT, GCP_ZONE, BURST_ONLINE_WAIT (300), BURST_MAX_RUN_S (10800).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=scripts/burst/size.sh
. "$HERE/size.sh"
ORG=${ORG:-Neonbee-ai}; VM=neonbee-ci-burst
PROJECT=${GCP_PROJECT:-gen-lang-client-0521938031}; ZONE=${GCP_ZONE:-europe-west1-b}; REGION=${ZONE%-*}
SELF_SA=nb-burst-self@$PROJECT.iam.gserviceaccount.com
LABELS_BASE=lane-burst,nb-pool
WAIT=${BURST_ONLINE_WAIT:-300}; MAX_RUN=${BURST_MAX_RUN_S:-10800}
JUMP_HOST=207.180.217.124; JUMP_TARGETS="68.183.86.248 167.71.226.224 167.114.98.177 84.247.131.148"
BUNDLE_BASE=https://github.com/Neonbee-ai/neonbee-devops/releases/download/burst-bundle
CE=https://compute.googleapis.com/compute/v1/projects/$PROJECT/zones/$ZONE
SUMMARY=${GITHUB_STEP_SUMMARY:-/dev/stdout}
say() { echo "$*"; echo "$*" >> "$SUMMARY"; }

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
gce_token() {
  local k now hdr clm sig; k=$(mktemp); chmod 600 "$k"
  jq -r .private_key <<<"$GCP_BURST_SA_KEY" > "$k"; now=$(date +%s)
  hdr=$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)
  clm=$(jq -cjn --arg i "$(jq -r .client_email <<<"$GCP_BURST_SA_KEY")" --argjson n "$now" \
    '{iss:$i, aud:"https://oauth2.googleapis.com/token", iat:$n, exp:($n+3600),
      scope:"https://www.googleapis.com/auth/compute https://www.googleapis.com/auth/cloud-billing.readonly"}' | b64url)
  sig=$(printf '%s.%s' "$hdr" "$clm" | openssl dgst -sha256 -sign "$k" | b64url); rm -f "$k"
  curl -fsS -m 20 https://oauth2.googleapis.com/token -d grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer \
    -d "assertion=$hdr.$clm.$sig" | jq -r .access_token
}
gce() { local m=$1 u=$2; shift 2; curl -sS -m 30 -X "$m" -H "Authorization: Bearer $GT" -H 'Content-Type: application/json' "$u" "$@"; }
gh() { local m=$1 p=$2; shift 2; curl -fsS -m 30 -X "$m" -H "Authorization: Bearer $NB_BURST_GH_TOKEN" \
  -H 'Accept: application/vnd.github+json' -H 'X-GitHub-Api-Version: 2022-11-28' "https://api.github.com/$p" "$@"; }
vm_status() { gce GET "$CE/instances/$VM" | jq -r '.status // "ABSENT"'; }
burst_runners() { gh GET "orgs/$ORG/actions/runners?per_page=100" | jq -c '[.runners[] | select(.name | startswith("gcp-burst-"))]'; }

# Pending jobs (queued/waiting) across this commit's not-yet-finished runs: the auto-size input.
count_jobs() {
  local repo=$1 sha=$2 n=0 id
  [ -n "$repo" ] && [ -n "$sha" ] || { echo 0; return; }
  for id in $(gh GET "repos/$repo/actions/runs?head_sha=$sha&per_page=50" | jq -r '.workflow_runs[] | select(.status != "completed") | .id'); do
    n=$((n + $(gh GET "repos/$repo/actions/runs/$id/jobs?filter=latest&per_page=100" |
      jq '[.jobs[] | select(.status == "queued" or .status == "waiting" or .status == "pending")] | length')))
  done; echo "$n"
}

jump_script() {
  cat <<EOJ
#!/bin/bash
install -d -m 755 /opt/burst; install -d -m 700 -o runner -g runner /opt/burst/cm
base64 -d > /opt/burst/jump_key <<'K'
$(printf '%s\n' "$NB_BURST_JUMP_KEY" | base64 | tr -d '\n')
K
chmod 600 /opt/burst/jump_key; chown runner: /opt/burst/jump_key
grep -qF '$JUMP_HOST' /etc/ssh/ssh_known_hosts 2>/dev/null || cat >> /etc/ssh/ssh_known_hosts <<'H'
$NB_BURST_JUMP_KNOWN
H
cat > /etc/ssh/ssh_config.d/10-nb-jump.conf <<'C'
Match host ${JUMP_TARGETS// /,}
  ProxyJump burstjump@$JUMP_HOST
  StrictHostKeyChecking accept-new
Host $JUMP_HOST
  User burstjump
  IdentityFile /opt/burst/jump_key
  IdentitiesOnly yes
  ControlMaster auto
  ControlPath /opt/burst/cm/%C
  ControlPersist 120
C
EOJ
}

wait_op() { local op=$1 s; for _ in $(seq 60); do
  s=$(gce GET "$CE/operations/$op" | jq -r '.status // "?"'); [ "$s" = DONE ] && return 0; sleep 5; done; return 1; }
delete_vm() { local op; op=$(gce DELETE "$CE/instances/$VM" | jq -r '.name // empty'); [ -z "$op" ] || wait_op "$op" || true; }

cmd_up() {
  : "${GCP_BURST_SA_KEY:?}" "${NB_BURST_GH_TOKEN:?}" "${NB_BURST_JUMP_KEY:?missing - fail closed}" "${NB_BURST_JUMP_KNOWN:?missing - fail closed}"
  GT=$(gce_token)
  local st jobs=0 sz runners machine disk tok body op o=0
  st=$(vm_status)
  case "$st" in
    RUNNING|PROVISIONING|STAGING) say "Burst VM already $st - nothing to do (requested by ${SOURCE:-?})"; return 0 ;;
    STOPPING|SUSPENDING|SUSPENDED|TERMINATED|STOPPED) say "Burst VM $st - deleting before re-create"; delete_vm ;;
  esac
  [ "${SIZE:-auto}" = auto ] && [ -z "${RUNNERS:-}" ] && { sleep 15; jobs=$(count_jobs "${REPO:-}" "${SHA:-}"); }
  sz=$(burst_size "${SIZE:-auto}" "$jobs" "${MACHINE:-}" "${RUNNERS:-}")
  read -r runners machine disk <<<"$sz"
  say "Burst size: **$machine**, $runners runners, ${disk} GB pd-balanced (size=${SIZE:-auto}, pending jobs=$jobs, source=${SOURCE:-manual})"
  reap_runners "VM absent before create"
  tok=$(gh POST "orgs/$ORG/actions/runners/registration-token" | jq -r .token)
  body=$(jq -n --arg n "$VM" --arg z "$ZONE" --arg m "$machine" --arg d "$disk" --arg sa "$SELF_SA" \
    --arg max "$MAX_RUN" --arg cnt "$runners" --arg tok "$tok" --arg lab "$LABELS_BASE,$machine" --arg org "$ORG" \
    --arg base "$BUNDLE_BASE" --rawfile ss "$HERE/vm-startup.sh" --arg jump "$(jump_script)" '{
    name:$n, machineType:"zones/\($z)/machineTypes/\($m)", labels:{"nb-burst":"label"},
    scheduling:{provisioningModel:"SPOT", preemptible:true, automaticRestart:false, onHostMaintenance:"TERMINATE",
      instanceTerminationAction:"DELETE", maxRunDuration:{seconds:$max}},
    disks:[{boot:true, autoDelete:true, initializeParams:{diskSizeGb:$d, diskType:"zones/\($z)/diskTypes/pd-balanced",
      sourceImage:"projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64"}}],
    networkInterfaces:[{network:"global/networks/default", accessConfigs:[{type:"ONE_TO_ONE_NAT", name:"External NAT"}]}],
    serviceAccounts:[{email:$sa, scopes:["https://www.googleapis.com/auth/compute"]}],
    metadata:{items:[{key:"startup-script",value:$ss},{key:"runner-count",value:$cnt},{key:"reg-token",value:$tok},
      {key:"runner-labels",value:$lab},{key:"org",value:$org},{key:"bundle-base",value:$base},{key:"nb-jump",value:$jump}]}}')
  op=$(gce POST "$CE/instances" --data-binary @- <<<"$body" | jq -r '.name // empty')
  [ -n "$op" ] || { say "Burst VM create failed - jobs stay on Contabo"; return 1; }
  wait_op "$op" || true
  for _ in $(seq $((WAIT / 15))); do
    o=$(burst_runners | jq '[.[] | select(.status == "online")] | length'); [ "$o" -ge 1 ] && break; sleep 15; done
  if [ "$o" -ge 1 ]; then say "Burst runners online: $o/$runners (labels $LABELS_BASE) - VM deletes itself after 3 idle min"
  else say "No burst runner online after ${WAIT}s - deleting VM; jobs run on Contabo (nb-pool)"; delete_vm; reap_runners "boot timeout"; return 1; fi
}

# Delete gcp-burst-* registrations, but only while the VM is gone (never under a live VM).
reap_runners() {
  local id; for id in $(burst_runners | jq -r '.[].id'); do gh DELETE "orgs/$ORG/actions/runners/$id" >/dev/null && echo "removed runner $id ($1)"; done
}

# Spot $/h for T2D core + RAM in REGION from the Cloud Billing Catalog (Compute Engine 6F81-5844-456A).
spot_rates() {
  local page='' out core='' ram=''
  for _ in $(seq 20); do
    out=$(gce GET "https://cloudbilling.googleapis.com/v1/services/6F81-5844-456A/skus?currencyCode=USD&pageSize=5000${page:+&pageToken=$page}") || return 1
    r() { jq -r --arg reg "$REGION" --arg re "$1" '[.skus[]? | select((.serviceRegions | index($reg)) and (.description | test($re)))
      | .pricingInfo[0].pricingExpression.tieredRates[-1].unitPrice | ((.units | tonumber) + .nanos / 1e9)][0] // empty' <<<"$out"; }
    [ -n "$core" ] || core=$(r '^Spot Preemptible T2D AMD Instance Core')
    [ -n "$ram" ] || ram=$(r '^Spot Preemptible T2D AMD Instance Ram')
    [ -n "$core" ] && [ -n "$ram" ] && { echo "$core $ram"; return 0; }
    page=$(jq -r '.nextPageToken // empty' <<<"$out"); [ -n "$page" ] || break
  done; return 1
}

cmd_reap() {
  : "${GCP_BURST_SA_KEY:?}" "${NB_BURST_GH_TOKEN:?}"
  local runners st n machine vcpu disk ins t0 t1 min rates src cost title repo sha pr since lost=0
  runners=$(burst_runners); n=$(jq length <<<"$runners")
  [ "$n" -gt 0 ] || { echo "no gcp-burst runners registered - nothing to reap"; return 0; }
  GT=$(gce_token); st=$(vm_status)
  case "$st" in RUNNING|PROVISIONING|STAGING|STOPPING) echo "VM $st - runners stay"; return 0 ;; esac
  machine=$(jq -r '[.[].labels[].name | select(startswith("t2d-standard-"))][0] // "t2d-standard-16"' <<<"$runners")
  vcpu=${machine#t2d-standard-}; disk=$((30 + 3 * n))
  # VM life from the zone operations log: last insert -> last delete (preemption/max-run show as deletes too).
  ins=$(gce GET "$CE/operations?filter=$(jq -rn --arg v "targetLink eq .*/instances/$VM" '$v|@uri')&maxResults=50" |
    jq -c '[.items[]? | {t: .operationType, s: .insertTime, e: (.endTime // .insertTime)}]')
  t0=$(jq -r '[.[] | select(.t == "insert") | .s] | max // empty' <<<"$ins")
  t1=$(jq -r '[.[] | select(.t == "delete" or .t == "compute.instances.preempted" or .t == "stop") | .e] | max // empty' <<<"$ins")
  since=${t0:-$(date -u -d '-4 hours' +%FT%TZ 2>/dev/null || date -u -v-4H +%FT%TZ)}
  min=-; [ -n "$t0" ] && [ -n "$t1" ] && min=$(jq -rn --arg a "$t0" --arg b "$t1" \
    '(($b | sub("\\.[0-9]+"; "") | sub("[-+][0-9:]+$"; "Z") | fromdateiso8601) - ($a | sub("\\.[0-9]+"; "") | sub("[-+][0-9:]+$"; "Z") | fromdateiso8601)) / 60 | ceil')

  # Re-run jobs the VM took down with it (preempted / max-run / killed): they re-route to nb-pool, which
  # only Contabo serves once the VM is gone.
  local r run job
  for r in $(gh GET "orgs/$ORG/repos?sort=pushed&direction=desc&per_page=30" | jq -r --arg s "$since" '.[] | select(.pushed_at >= $s) | .full_name'); do
    for run in $(gh GET "repos/$r/actions/runs?status=failure&created=>=$since&per_page=50" | jq -r '.workflow_runs[] | select(.run_attempt < 3) | .id'); do
      for job in $(gh GET "repos/$r/actions/runs/$run/jobs?filter=latest&per_page=100" |
          jq -r '.jobs[] | select(.conclusion == "failure" and ((.runner_name // "") | startswith("gcp-burst-"))) | .id'); do
        if gh GET "repos/$r/check-runs/$job/annotations" | jq -e '[.[].message | test("lost communication|shutdown signal|runner .* (was )?(deleted|removed)"; "i")] | any' >/dev/null; then
          gh POST "repos/$r/actions/runs/$run/rerun-failed-jobs" >/dev/null && { say "Re-ran $r run $run (job $job lost with the burst VM)"; lost=$((lost + 1)); }
          break
        fi
      done
    done
  done
  reap_runners "VM $st"

  if rates=$(spot_rates); then src="Cloud Billing Catalog, $REGION Spot"; else rates=; src="FALLBACK constant table in size.sh - estimate"; fi
  # shellcheck disable=SC2086
  cost="?"; [ "$min" = - ] || cost=$(burst_cost "$vcpu" "$min" "$disk" $rates)
  say "### Burst VM cost (estimate)"
  say "| machine | vCPU | runners | disk | uptime | rate source | est. cost |"
  say "|---|---|---|---|---|---|---|"
  say "| $machine | $vcpu | $n | ${disk} GB | ${min} min | $src | \$$cost |"
  [ "$lost" -gt 0 ] && say "Jobs re-run on Contabo after VM loss: $lost"
  # PR comment on the PR that asked (from the last burst-on-label up run's run-name "burst up ... <repo>@<sha>").
  title=$(gh GET "repos/$ORG/neonbee-devops/actions/workflows/burst-on-label.yml/runs?event=workflow_dispatch&per_page=1" |
    jq -r '.workflow_runs[0].display_title // empty')
  repo=$(sed -nE 's/.* ([^ ]+\/[^ @]+)@([0-9a-f]{7,40}).*/\1/p' <<<"$title"); sha=$(sed -nE 's/.*@([0-9a-f]{7,40}).*/\1/p' <<<"$title")
  if [ -n "$repo" ] && [ -n "$sha" ] && pr=$(gh GET "repos/$repo/commits/$sha/pulls" | jq -r '.[0].number // empty') && [ -n "$pr" ]; then
    gh POST "repos/$repo/issues/$pr/comments" -d "$(jq -cn --arg b "Burst VM finished: **$machine** ($vcpu vCPU, $n runners, ${disk} GB), up ${min} min, est. **\$$cost** ($src). Jobs re-run after VM loss: $lost." '{body:$b}')" >/dev/null &&
      echo "cost posted on $repo#$pr"
  fi
}

case "${1:-}" in up) cmd_up ;; reap) cmd_reap ;; *) echo "usage: $0 up|reap" >&2; exit 2 ;; esac
