#!/usr/bin/env bash
# nb-batch: one on-demand GCP Spot VM with 16 GitHub runners for ONE delivery batch, then it deletes itself.
# Design, cost, failure handling and security: documents/ci/batch-runner.md
# Lives in neonbee-devops/scripts; the workspace-root copy (scripts/nb-batch.sh) is the original. Run with NB_WS=<workspace root> so repo dirs resolve.
#
#   nb-batch.sh setup               one-time, idempotent: self-delete service account + conditional role, firewall
#                                   (tag nb-batch: deny all ingress except IAP ssh). All free.
#   nb-batch.sh plan [sel] [opts]   dry run: repos/branches to push, PR targets, develop->main carry, runners, ETA,
#                                   cost, blockers. Changes nothing (only `git fetch`).
#   nb-batch.sh go [sel] [opts]     the whole batch; asks you to type GO, then detaches (log in ~/.neonbee/batch/<id>/):
#                                   VM up -> NB_BURST=batch@<expiry> on the batch repos -> push -> PRs to develop ->
#                                   merge gate + CI -> merge -> develop deploys green -> PR develop->main -> gate + CI ->
#                                   merge -> main deploys green (HA) -> report -> routing off -> runners removed -> VM gone
#   nb-batch.sh status [id]         batch progress, VM, runners, routing variables
#   nb-batch.sh abort [id]          stop the controller, routing off, VM deleted, runners removed, proof nothing is left
#   nb-batch.sh handoff [host]      move the running batch's watcher to the Mac Mini (needs gh only; default macmini)
#   nb-batch.sh smoke               infra smoke test (~$0.01): 1 runner on c2d-highcpu-2, registration, deploy-target ssh,
#                                   self-delete on the done signal, full teardown + proof
#   nb-batch.sh hold <id> [min]     raise the VM idle limit (default 40 min) while more dispatches are still to come
#   nb-batch.sh release <id>        back to the idle limit (NB_IDLE_MIN, 3): the VM deletes itself after 3 idle minutes
#   internal: up <id> <n> [machine] | run <id> | resume <id> | teardown <id> | route-off <id> | vm-script
#
# sel:  --repos a,b          repo dirs or names (default: every repo whose checked-out branch is ahead of origin/develop)
#       --manifest FILE      lines "<dir> <branch> [base]" (base other than develop = no main leg)
# opts: --no-main            develop only
#       --gate on|ci-only    on (default) = Mac Mini merge gate (scripts/merge-gate.sh) + GitHub checks before every
#                            merge; ci-only = GitHub checks only (logged like SO360_GATE_SKIP)
#       --allow-comb         allow branches built on feat/comb-adoption (blocked by default: they would land on develop)
#       --yes                skip the GO prompt        --foreground   do not detach        --no-fetch   (plan)
#
# Env: GCP_PROJECT (gen-lang-client-0521938031), GCP_ZONE (europe-west1-b), NB_MACHINE (c2d-standard-16),
#      NB_RUNNERS (16), NB_DISK_GB (100), NB_LIFETIME_MIN (60 = GCE hard delete), NB_DRAIN_MIN (15: routing expires and
#      the VM drains this long before the hard delete), NB_IDLE_MIN (3), NB_FIRST_JOB_MIN (15), NB_POLL_S (30),
#      NB_GATE_MIN (4: est. Mini gate minutes per PR), GATE_HOSTS (macmini), GCLOUD, NB_WS (workspace root)
set -euo pipefail

SELF=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")
WS=${NB_WS:-$(cd "$(dirname "$SELF")/.." && pwd)}
ORG=Neonbee-ai
PROJECT=${GCP_PROJECT:-gen-lang-client-0521938031}
ZONE=${GCP_ZONE:-europe-west1-b}
MACHINE=${NB_MACHINE:-c2d-standard-16}
RUNNERS=${NB_RUNNERS:-16}
DISK_GB=${NB_DISK_GB:-100}
LIFETIME_MIN=${NB_LIFETIME_MIN:-60}; DRAIN_MIN=${NB_DRAIN_MIN:-15}
IDLE_MIN=${NB_IDLE_MIN:-3}; FIRST_JOB_MIN=${NB_FIRST_JOB_MIN:-15}
TAG=nb-batch; PREFIX=nbb; RUNNER_LABELS=${NB_RUNNER_LABELS:-lane-burst,nb-pool,nb-route,nb-batch}   # nb-route: route jobs never wait behind a build
SA_ID=nb-batch-self; SA=$SA_ID@$PROJECT.iam.gserviceaccount.com; ROLE=nbBatchSelf
IMAGE_FAMILY=nb-runner
# Deploy targets the VM must reach on :22 (HA/Dev VM, Internal Tools VM; the DO VMs 68.183.86.248/167.71.226.224 are
# deleted). Direct by default: the Contabo jump host 207.180.217.124 is gone (2026-11-20). Set NB_JUMP_HOST to route
# ssh to these targets through a restricted jump user again (scripts/gcp-burst.sh jump-setup).
SSH_TARGETS=${NB_SSH_TARGETS:-84.247.131.148 167.114.98.177}
JUMP_HOST=${NB_JUMP_HOST:-}; JUMP_USER=burstjump
JUMP_KEY=$HOME/.neonbee/burst-jump-key
ST=$HOME/.neonbee/batch
GCLOUD=${GCLOUD:-$(command -v gcloud 2>/dev/null || echo /opt/homebrew/share/google-cloud-sdk/bin/gcloud)}
G=("$GCLOUD" --project "$PROJECT" --quiet)

have_gcloud() { [ -x "$GCLOUD" ]; }
gh_() { env -u GH_TOKEN gh "$@"; }
log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { echo "nb-batch: $*" >&2; exit 1; }
latest_id() { ls -1t "$ST" 2>/dev/null | grep -E '^[0-9]{8}-[0-9]{4}$' | head -1 || true; }
need_id() { local id=${1:-$(latest_id)}; [ -n "$id" ] && [ -d "$ST/$id" ] || die "no batch state (${1:-latest})"; echo "$id"; }
alive() { [ -f "$ST/$1/pid" ] && kill -0 "$(cat "$ST/$1/pid")" 2>/dev/null; }

# ---------------------------------------------------------------------------------------------------------------------
# VM side. Startup script: watchdog first (so even a failed boot deletes itself), then runners, then jump host check.
vm_script() { cat <<'EOS'
#!/bin/bash
mkdir -p /opt/nb
cat > /opt/nb/lib.sh <<'EOL'
M=http://metadata.google.internal/computeMetadata/v1
md() { curl -sf -m 5 -H Metadata-Flavor:Google "$M/$1"; }
say() { echo "NB-$*" > /dev/ttyS0; logger -t nb-batch "$*"; }
api() { local t; t=$(md instance/service-accounts/default/token | sed -nE 's/.*"access_token":"([^"]+)".*/\1/p')
  curl -sf -m 30 -H "Authorization: Bearer $t" -H 'Content-Type: application/json' "$@"; }
self_url() { local z; z=$(md instance/zone)
  echo "https://compute.googleapis.com/compute/v1/projects/$(md project/project-id)/zones/${z##*/}/instances/$(md instance/name)"; }
# Delete this instance (the boot disk auto-deletes with it). Fallback: power off (disk-only cost until abort/status).
self_delete() {
  mkdir /run/nb-deleting 2>/dev/null || exit 0
  say "SELF-DELETE $*"
  systemctl stop 'actions.runner.*' >/dev/null 2>&1
  api -X DELETE "$(self_url)" >/dev/null || { say "SELF-DELETE-FAILED poweroff"; poweroff; }
  sleep 300; poweroff
}
EOL
. /opt/nb/lib.sh
[ -f /opt/nb/booted ] && { say "REBOOTED (agent keeps running)"; exit 0; }
date +%s > /opt/nb/booted

# Agent, every minute: done signal | hard deadline | drain window | idle -> delete this VM.
cat > /opt/nb/agent.sh <<'EOA'
#!/bin/bash
. /opt/nb/lib.sh
now=$(date +%s)
[ "$(md instance/attributes/nb-done)" = 1 ] && self_delete "done signal"
dl=$(md instance/attributes/deadline); [ -n "$dl" ] && [ "$now" -ge "$dl" ] && self_delete "deadline"
dr=$(md instance/attributes/drain-at)
if [ -n "$dr" ] && [ "$now" -ge "$dr" ]; then
  # Drain: stop every idle runner so no new job starts; delete the VM once nothing runs.
  for d in /home/runner/r*; do pgrep -f "$d/bin/Runner.Worker" >/dev/null && continue
    (cd "$d" && ./svc.sh stop >/dev/null 2>&1); done
  pgrep -f 'bin/Runner.Worker' >/dev/null || self_delete "drained"
  exit 0
fi
if pgrep -f 'bin/Runner.Worker' >/dev/null; then echo 0 > /opt/nb/idle; touch /opt/nb/had-job; exit 0; fi
n=$(( $(cat /opt/nb/idle 2>/dev/null || echo 0) + 1 )); echo "$n" > /opt/nb/idle
lim=$(md instance/attributes/idle-min); [ -f /opt/nb/had-job ] || lim=$(md instance/attributes/first-job-min)
[ "$n" -ge "${lim:-3}" ] && self_delete "idle ${n}m"
exit 0
EOA
chmod 755 /opt/nb/agent.sh; echo 0 > /opt/nb/idle
echo '* * * * * root /opt/nb/agent.sh' > /etc/cron.d/nb-agent

N=$(md instance/attributes/runner-count); ORG=$(md instance/attributes/org)
LABELS=$(md instance/attributes/runner-labels); NAME=$(md instance/attributes/runner-prefix)
TOKEN=$(md instance/attributes/reg-token)
# Scrub the one-hour registration token from metadata at once (job code can read the metadata server).
u=$(self_url)
api "$u" | python3 -c 'import json,sys; m=json.load(sys.stdin)["metadata"]; print(json.dumps({"fingerprint":m["fingerprint"],"items":[i for i in m.get("items",[]) if i["key"]!="reg-token"]}))' |
  api -X POST "$u/setMetadata" -d @- >/dev/null && say "TOKEN-SCRUBBED" || say "TOKEN-SCRUB-FAILED"
# Job code (user runner) never needs the metadata server (no SA token for jobs).
command -v iptables >/dev/null && id runner >/dev/null 2>&1 &&
  iptables -A OUTPUT -d 169.254.169.254 -m owner --uid-owner runner -j REJECT

export DEBIAN_FRONTEND=noninteractive
if [ ! -d /opt/runner ]; then   # stock image fallback (+4 min); the baked nb-runner image has all of this
  apt-get update -q && apt-get install -yq git rsync jq curl unzip zip zstd build-essential openssh-client python3
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && apt-get install -yq nodejs
  id runner >/dev/null 2>&1 || useradd -m -s /bin/bash runner
  echo 'runner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/runner
  iptables -A OUTPUT -d 169.254.169.254 -m owner --uid-owner runner -j REJECT 2>/dev/null
  mkdir -p /opt/runner
fi
V=$(curl -fsSL -m 20 https://api.github.com/repos/actions/runner/releases/latest | sed -nE 's/.*"tag_name": *"v([^"]+)".*/\1/p')
H=$(/opt/runner/bin/Runner.Listener --version 2>/dev/null || true)
if [ -n "$V" ] && [ "$V" != "$H" ]; then
  rm -rf /opt/runner/bin /opt/runner/externals
  curl -fsSL "https://github.com/actions/runner/releases/download/v$V/actions-runner-linux-x64-$V.tar.gz" | tar xz -C /opt/runner
  /opt/runner/bin/installdependencies.sh >/dev/null 2>&1
fi
sed -i '/ACTIONS_RUNNER_HOOK_JOB_COMPLETED/d' /opt/runner/.env 2>/dev/null   # old burst bucket hook, not used here
chown -R runner: /opt/runner
# Private /tmp per runner service (jobs write fixed paths like /tmp/gitleaks).
mkdir -p /etc/systemd/system/actions.runner.Neonbee-.service.d
printf '[Service]\nPrivateTmp=yes\n' > /etc/systemd/system/actions.runner.Neonbee-.service.d/tmp.conf
reg() { local d=/home/runner/r$1
  mkdir -p "$d" && cp -a /opt/runner/. "$d/" && chown -R runner: "$d" &&
  sudo -u runner "$d/config.sh" --unattended --replace --url "https://github.com/$ORG" --token "$TOKEN" \
    --name "$NAME-$1" --labels "$LABELS" --work _work > "/opt/nb/reg-$1.log" 2>&1; }
for i in $(seq -w 1 "$N"); do reg "$i" & done; wait
unset TOKEN
systemctl daemon-reload
for i in $(seq -w 1 "$N"); do d=/home/runner/r$i; [ -f "$d/.runner" ] || { say "REGISTER-FAILED r$i"; continue; }
  (cd "$d" && ./svc.sh install runner >/dev/null 2>&1 && ./svc.sh start >/dev/null 2>&1); done
say "RUNNERS-UP $(systemctl list-units 'actions.runner.*' --no-legend | grep -c running)/$N"

# Disk guard: past 80 %, drop idle runners' checkouts older than 15 min.
cat > /opt/nb/disk.sh <<'EOC'
#!/bin/bash
[ "$(df --output=pcent / | tail -1 | tr -dc 0-9)" -lt 80 ] && exit 0
for d in /home/runner/r*; do pgrep -f "$d/bin/Runner.Worker" >/dev/null && continue
  find "$d/_work" -mindepth 1 -maxdepth 1 ! -name _tool ! -name _actions -mmin +15 -exec rm -rf {} + 2>/dev/null; done
EOC
chmod 755 /opt/nb/disk.sh; echo '*/2 * * * * root /opt/nb/disk.sh' > /etc/cron.d/nb-disk

# Deploy targets: ssh banner check per target, direct or through the optional jump host.
J=$(md instance/attributes/jump-host)
[ -n "$J" ] && md instance/attributes/nb-jump | bash
for t in $(md instance/attributes/ssh-targets); do
  if [ -n "$J" ]; then b=$(sudo -u runner timeout 15 ssh -o BatchMode=yes -W "$t:22" "$J" </dev/null 2>/dev/null | head -c 7)
  else b=$(sudo -u runner timeout 10 bash -c "exec 3<>/dev/tcp/$t/22 && head -c 7 <&3" 2>/dev/null); fi
  [ "$b" = SSH-2.0 ] && say "SSH-OK $t${J:+ via $J}" || say "SSH-FAIL $t${J:+ via $J}"
done
EOS
}

# Optional boot-time ssh config (metadata nb-jump): jobs reach SSH_TARGETS through NB_JUMP_HOST (restricted burstjump user).
jump_script() {
  [ -n "$JUMP_HOST" ] || { echo '#!/bin/bash'; return; }
  [ -f "$JUMP_KEY" ] && [ -f "$JUMP_KEY.known" ] || die "no jump key at $JUMP_KEY (scripts/gcp-burst.sh jump-setup)"
  cat <<EOJ
#!/bin/bash
install -d -m 755 /opt/burst; install -d -m 700 -o runner -g runner /opt/burst/cm
base64 -d > /opt/burst/jump_key <<'K'
$(base64 < "$JUMP_KEY")
K
chmod 600 /opt/burst/jump_key; chown runner: /opt/burst/jump_key
grep -qF '$JUMP_HOST' /etc/ssh/ssh_known_hosts 2>/dev/null || cat >> /etc/ssh/ssh_known_hosts <<'H'
$(cat "$JUMP_KEY.known")
H
cat > /etc/ssh/ssh_config.d/10-nb-jump.conf <<'C'
Match host ${SSH_TARGETS// /,}
  ProxyJump $JUMP_USER@$JUMP_HOST
  StrictHostKeyChecking accept-new
Host $JUMP_HOST
  User $JUMP_USER
  IdentityFile /opt/burst/jump_key
  IdentitiesOnly yes
  ControlMaster auto
  ControlPath /opt/burst/cm/%C
  ControlPersist 120
C
EOJ
}

# ---------------------------------------------------------------------------------------------------------------------
# GitHub helpers
runners() { gh_ api --paginate "orgs/$ORG/actions/runners?per_page=100" --jq '.runners[] | [.id, .name, .status, .busy] | @tsv'; }
runners_online() { runners | awk -F'\t' -v p="$1" 'index($2, p) == 1 && $3 == "online" {n++} END {print n+0}'; }
set_route() {  # <owner/repo> <value>: per-repo NB_BURST (org variables are invisible to private repos)
  gh_ api -X PATCH "repos/$1/actions/variables/NB_BURST" -f name=NB_BURST -f value="$2" >/dev/null 2>&1 ||
    gh_ api -X POST "repos/$1/actions/variables" -f name=NB_BURST -f value="$2" >/dev/null
}
get_route() { gh_ api "repos/$1/actions/variables/NB_BURST" --jq .value 2>/dev/null || echo "(unset)"; }
deregister() {  # <name prefix>: remove runner registrations; busy ones are retried for up to 3 min
  local p=$1 left i id name st busy
  for i in $(seq 10); do
    left=0
    while IFS=$'\t' read -r id name st busy; do
      [[ $name == "$p"* ]] || continue
      if gh_ api -X DELETE "orgs/$ORG/actions/runners/$id" >/dev/null 2>&1; then log "runner $name removed"
      else left=$((left + 1)); fi
    done < <(runners)
    [ "$left" = 0 ] && return 0; sleep 20
  done
  log "WARN: $left runner(s) with prefix $p still registered (busy); 'nb-batch abort' removes them once the VM is gone"
}

# ---------------------------------------------------------------------------------------------------------------------
# Discovery: TSV  dir repo branch base ahead unpushed carry status note
discover() {
  local repos=$1 manifest=$2 fetch=$3 allow_comb=$4 d br base line want=() dirs=()
  if [ -n "$manifest" ]; then
    while read -r d br base; do [ -z "$d" ] || [[ $d == \#* ]] && continue
      [ -d "$WS/$d" ] && d=$WS/$d; dirs+=("$d|$br|${base:-develop}"); done < "$manifest"
  else
    [ -n "$repos" ] && IFS=, read -r -a want <<<"$repos"
    while read -r d; do d=${d%/.git}
      if [ ${#want[@]} -gt 0 ]; then local hit=0 w
        for w in "${want[@]}"; do [[ ${d#"$WS"/} == "$w" || $(basename "$d") == "${w##*/}" ]] && hit=1; done
        [ $hit = 1 ] || continue; fi
      dirs+=("$d||develop")
    done < <(find "$WS" -mindepth 2 -maxdepth 3 -name .git -type d -not -path '*/.wt/*' -not -path '*/.worktrees/*' -not -path '*/node_modules/*' | sort)
  fi
  [ ${#dirs[@]} -gt 0 ] || return 0
  if [ "$fetch" = 1 ]; then
    printf '%s\0' "${dirs[@]%%|*}" | xargs -0 -P 8 -I{} sh -c 'git -C "{}" fetch -q origin >/dev/null 2>&1 || true'
  fi
  for line in "${dirs[@]}"; do
    IFS='|' read -r d br base <<<"$line"
    local url repo ahead unp behind carry status=OK note=""
    url=$(git -C "$d" remote get-url origin 2>/dev/null) || continue
    repo=$(sed -E 's#^(https://[^/]+/|git@[^:]+:)##; s#\.git$##' <<<"$url"); [[ $repo == "$ORG"/* ]] || continue
    [ -n "$br" ] || br=$(git -C "$d" symbolic-ref --short -q HEAD || true)
    case "$br" in ""|develop|main|master|qa) [ -n "$manifest" ] && echo -e "${d#"$WS"/}\t$repo\t${br:-?}\t$base\t0\t0\t0\tBLOCK\tnot a fix branch"; continue ;; esac
    git -C "$d" rev-parse -q --verify "refs/remotes/origin/$base" >/dev/null || { echo -e "${d#"$WS"/}\t$repo\t$br\t$base\t0\t0\t0\tBLOCK\tno origin/$base"; continue; }
    git -C "$d" rev-parse -q --verify "refs/heads/$br" >/dev/null || { echo -e "${d#"$WS"/}\t$repo\t$br\t$base\t0\t0\t0\tBLOCK\tno local branch"; continue; }
    ahead=$(git -C "$d" rev-list --count "origin/$base..$br")
    [ "$ahead" -gt 0 ] || { [ -n "$manifest$repos" ] && echo -e "${d#"$WS"/}\t$repo\t$br\t$base\t0\t0\t0\tSKIP\tnothing ahead of $base"; continue; }
    if git -C "$d" rev-parse -q --verify "refs/remotes/origin/$br" >/dev/null; then
      unp=$(git -C "$d" rev-list --count "origin/$br..$br"); behind=$(git -C "$d" rev-list --count "$br..origin/$br")
      [ "$behind" -gt 0 ] && { status=BLOCK; note="origin/$br has $behind commit(s) not in local: push would be rejected"; }
    else unp=new; fi
    carry=-
    if [ "$base" = develop ] && git -C "$d" rev-parse -q --verify refs/remotes/origin/main >/dev/null; then
      carry=$(git -C "$d" rev-list --count origin/main..origin/develop); fi
    if [ "$allow_comb" != 1 ] && [ "$base" != feat/comb-adoption ]; then
      if [ "$br" = feat/comb-adoption ]; then status=BLOCK; note="feat/comb-adoption itself (--allow-comb to merge it)"
      elif git -C "$d" rev-parse -q --verify refs/remotes/origin/feat/comb-adoption >/dev/null; then
        local mb n; mb=$(git -C "$d" merge-base "$br" origin/feat/comb-adoption 2>/dev/null || true)
        n=$([ -n "$mb" ] && git -C "$d" rev-list --count "origin/$base..$mb" || echo 0)
        [ "$n" -gt 0 ] && { status=BLOCK; note="built on feat/comb-adoption: $n comb commit(s) would land on $base (--allow-comb)"; }
      fi
    fi
    [ "$ahead" -gt 40 ] && note="${note:+$note; }$ahead commits ahead"
    [ -n "$(git -C "$d" status --porcelain --untracked-files=no 2>/dev/null)" ] && note="${note:+$note; }uncommitted changes stay local"
    echo -e "${d#"$WS"/}\t$repo\t$br\t$base\t$ahead\t$unp\t$carry\t$status\t$note"
  done
}

# ETA + cost (python: spot price from the Cloud Billing catalog, cached 24 h; history from gcp-burst plan cache)
estimate() {  # <repos> <legs> <gate:on|ci-only> <machine> <runners>
  local tok=""; have_gcloud && tok=$("$GCLOUD" auth print-access-token 2>/dev/null || true)
  P=$1 LEGS=$2 GATE=$3 MACH=$4 R=$5 TOK=$tok DISK=$DISK_GB LIFE=$LIFETIME_MIN DRAIN=$DRAIN_MIN \
  GATE_N=$(wc -w <<<"${GATE_HOSTS:-macmini}") GATE_MIN=${NB_GATE_MIN:-4} python3 - <<'EOP'
import os,json,time,math,urllib.request
E=os.environ.get; P=int(E('P')); LEGS=int(E('LEGS')); R=int(E('R')); M=E('MACH')
fam,kind,n=M.split('-')[0],M.split('-')[1],int(M.split('-')[-1]); ram=n*{'standard':4,'highcpu':2,'highmem':8}.get(kind,4)
C=os.path.expanduser('~/.neonbee/spot-prices.json'); FALL={'c2d':(0.00453,0.00061),'t2d':(0.01815,0.00243),'n2d':(0.01815,0.00243)}
try: pr=json.load(open(C)); assert time.time()-pr['at']<86400
except Exception:
  pr={'at':time.time(),'p':{}}
  if E('TOK'):
    try:
      u='https://cloudbilling.googleapis.com/v1/services/6F81-5844-456A/skus?pageSize=5000&currencyCode=USD';tok=''
      for _ in range(12):
        r=json.load(urllib.request.urlopen(urllib.request.Request(u+('&pageToken='+tok if tok else ''),headers={'Authorization':'Bearer '+E('TOK')}),timeout=60))
        for s in r['skus']:
          d=s['description']
          if 'europe-west1' in s.get('serviceRegions',[]) and d.startswith('Spot Preemptible') and 'Custom' not in d:
            for f in FALL:
              if (' %s '%f.upper()) in d:
                x=s['pricingInfo'][0]['pricingExpression']['tieredRates'][-1]['unitPrice']; v=int(x['units'])+x['nanos']/1e9
                pr['p'].setdefault(f,[0,0])[0 if 'Core' in d else 1]=v
        tok=r.get('nextPageToken')
        if not tok: break
      os.makedirs(os.path.dirname(C),exist_ok=True); json.dump(pr,open(C,'w'))
    except Exception as e: pr['err']=str(e)
core,gb=pr['p'].get(fam) or FALL.get(fam,(0.02,0.0027)); src='catalog' if fam in pr['p'] else 'fallback'
hr=n*core+ram*gb+int(E('DISK'))*0.11/730
try: st=json.load(open(os.path.expanduser('~/.neonbee/plan-stats.json'))); Mr,L=st['M'],st['L']
except Exception: Mr,L=20,31
# Burst runners run one job each on dedicated C2D cores: assume the Contabo history chain shrinks by 1/3.
L2=L*0.67; per_leg_ci=max(L2*0.5,P*Mr*0.5/R)   # PR checks ~ half a pipeline; push deploy = full pipeline
per_leg_dep=max(L2,P*Mr/R); gate=math.ceil(P/int(E('GATE_N')))*float(E('GATE_MIN')) if E('GATE')=='on' else 0
leg=max(per_leg_ci,gate)+per_leg_dep+1
eta=3+LEGS*leg; life=int(E('LIFE')); billed=min(eta+int(E('IDLE','3')),life)
print('price   %s spot %d vCPU / %d GB: $%.4f/vCPU-h + $%.5f/GB-h (%s) + %d GB pd-balanced = $%.3f/h'%(M,n,ram,core,gb,src,int(E('DISK')),hr))
print('history Contabo p90 %.0f runner-min/pipeline, p75 chain %.0f min (burst est. %.0f)'%(Mr,L,L2))
print('ETA     ~%.0f min for %d repo(s) x %d leg(s): boot 3 + per leg [checks %.0f | Mini gate %.0f] + deploy %.0f'%(eta,P,LEGS,per_leg_ci,gate,per_leg_dep))
print('cost    ~$%.2f per batch (%.0f billed min); hard cap %d min = $%.2f'%(hr*billed/60,billed,life,hr*life/60))
if eta>life-int(E('DRAIN')): print('WARN    ETA exceeds the burst window (%d-%d min): later jobs run on the default lane (runner-vm, today the Mac Mini); consider NB_LIFETIME_MIN=%d'%(life,int(E('DRAIN')),int(math.ceil((eta+20)/10)*10)))
EOP
}

parse_sel() {  # sets REPOS MANIFEST MAIN GATE ALLOW_COMB YES FG FETCH
  REPOS=""; MANIFEST=""; MAIN=1; GATE=on; ALLOW_COMB=0; YES=0; FG=0; FETCH=1
  while [ $# -gt 0 ]; do case "$1" in
    --repos) REPOS=$2; shift ;; --manifest) MANIFEST=$2; shift ;; --no-main) MAIN=0 ;;
    --gate) GATE=$2; shift; [[ $GATE == on || $GATE == ci-only ]] || die "--gate on|ci-only" ;;
    --allow-comb) ALLOW_COMB=1 ;; --yes) YES=1 ;; --foreground) FG=1 ;; --no-fetch) FETCH=0 ;;
    *) die "unknown option $1" ;; esac; shift; done
}

preflight() {  # prints problems; returns 1 if any blocks a go
  local bad=0 q
  gh_ api user --jq .login >/dev/null 2>&1 || { echo "BLOCK gh not authenticated (gh auth login)"; bad=1; }
  have_gcloud || { echo "BLOCK no gcloud at $GCLOUD"; return 1; }
  "$GCLOUD" auth print-access-token >/dev/null 2>&1 || { echo "BLOCK gcloud not authenticated"; bad=1; }
  "${G[@]}" iam service-accounts describe "$SA" >/dev/null 2>&1 || { echo "BLOCK run: nb-batch.sh setup (no $SA_ID)"; bad=1; }
  "${G[@]}" compute firewall-rules describe nb-batch-deny-ingress >/dev/null 2>&1 || { echo "BLOCK run: nb-batch.sh setup (no firewall)"; bad=1; }
  [ -z "$JUMP_HOST" ] || { [ -f "$JUMP_KEY" ] && [ -f "$JUMP_KEY.known" ]; } || { echo "BLOCK no jump key ($JUMP_KEY): scripts/gcp-burst.sh jump-setup"; bad=1; }
  "${G[@]}" compute images describe-from-family "$IMAGE_FAMILY" --format='value(name)' >/dev/null 2>&1 ||
    echo "note  no $IMAGE_FAMILY image: stock Ubuntu boot (+4 min); bake with scripts/gcp-burst.sh image"
  q=$("${G[@]}" compute regions describe "${ZONE%-*}" --format=json 2>/dev/null | python3 -c "
import json,sys; f='${MACHINE%%-*}'.upper()+'_CPUS'; q={x['metric']:x for x in json.load(sys.stdin)['quotas']}
print(int(min(q[m]['limit']-q[m]['usage'] for m in ('CPUS',f) if m in q)))" 2>/dev/null || echo 0)
  [ "$q" -ge "${MACHINE##*-}" ] || { echo "BLOCK free ${MACHINE%%-*} vCPU quota in ${ZONE%-*} is $q < ${MACHINE##*-}"; bad=1; }
  q=$("${G[@]}" compute project-info describe --format=json 2>/dev/null | python3 -c "
import json,sys; q={x['metric']:x for x in json.load(sys.stdin)['quotas']}['CPUS_ALL_REGIONS']; print(int(q['limit']-q['usage']))" 2>/dev/null || echo 0)
  [ "$q" -ge "${MACHINE##*-}" ] || { echo "BLOCK global CPUS_ALL_REGIONS free $q < ${MACHINE##*-} (AutoQA Spot workers use it; wait, or raise the quota)"; bad=1; }
  local other; other=$("${G[@]}" compute instances list --filter="labels.nb-batch:*" --format='value(name)' 2>/dev/null)
  [ -z "$other" ] || { echo "BLOCK batch VM already running: $other (nb-batch.sh status / abort)"; bad=1; }
  return $bad
}

cmd_plan() {
  parse_sel "$@"
  local tsv ok n legs
  tsv=$(discover "$REPOS" "$MANIFEST" "$FETCH" "$ALLOW_COMB")
  echo "== repos (push -> PR -> merge; base develop; then develop -> main unless --no-main; never qa)"
  { echo -e "DIR\tBRANCH\tBASE\tAHEAD\tUNPUSHED\tDEV->MAIN CARRY\tSTATUS\tNOTE"
    awk -F'\t' -v m="$MAIN" '{c=($4=="develop" && m==1)?$7:"-"; print $1"\t"$3"\t"$4"\t"$5"\t"$6"\t"c"\t"$8"\t"$9}' <<<"$tsv"; } |
    column -t -s $'\t' | cut -c1-200
  mkdir -p "$ST"; printf '%s\n' "$tsv" > "$ST/plan.tsv"
  ok=$(awk -F'\t' '$8=="OK"' <<<"$tsv"); n=$(grep -c . <<<"$ok" || true)
  echo; echo "== $n repo(s) would go; gate=$GATE; main leg=$([ "$MAIN" = 1 ] && echo yes || echo no)"
  [ "$n" -gt 0 ] || return 0
  echo "   carry = commits already on develop (any author) that the develop->main PR also releases"
  echo; echo "== routing: NB_BURST=batch@<start+$((LIFETIME_MIN - DRAIN_MIN))m> on these repos only; currently:"
  local r; while IFS=$'\t' read -r _ r _; do printf '   %-45s %s\n' "$r" "$(get_route "$r")"; done <<<"$ok"
  echo; echo "== infra: 1 x $MACHINE Spot in $ZONE, $RUNNERS runners ($RUNNER_LABELS), ${DISK_GB} GB, hard delete at ${LIFETIME_MIN}m, idle ${IDLE_MIN}m"
  legs=1; [ "$MAIN" = 1 ] && legs=2
  estimate "$n" "$legs" "$GATE" "$MACHINE" "$RUNNERS" || true
  echo; echo "== preflight"; preflight && echo "ready" || true
}

cmd_setup() {
  have_gcloud || die "no gcloud"
  "${G[@]}" services enable compute.googleapis.com cloudbilling.googleapis.com >/dev/null
  "${G[@]}" iam service-accounts describe "$SA" >/dev/null 2>&1 ||
    "${G[@]}" iam service-accounts create "$SA_ID" --display-name "nb-batch VM self-delete (own instance only)"
  local perms=compute.instances.get,compute.instances.delete,compute.instances.setMetadata,compute.disks.delete
  "${G[@]}" iam roles describe "$ROLE" --project "$PROJECT" >/dev/null 2>&1 &&
    "${G[@]}" iam roles update "$ROLE" --project "$PROJECT" --permissions "$perms" >/dev/null ||
    "${G[@]}" iam roles create "$ROLE" --project "$PROJECT" --title "nb-batch self delete" --permissions "$perms" --stage GA >/dev/null
  local pz="projects/$PROJECT/zones/$ZONE"
  "${G[@]}" projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" \
    --role "projects/$PROJECT/roles/$ROLE" \
    --condition="title=nb-batch-own-vm,expression=resource.name.startsWith('$pz/instances/nb-batch-') || resource.name.startsWith('$pz/disks/nb-batch-')" >/dev/null
  "${G[@]}" compute firewall-rules describe nb-batch-deny-ingress >/dev/null 2>&1 ||
    "${G[@]}" compute firewall-rules create nb-batch-deny-ingress --network default --direction INGRESS --priority 900 \
      --action DENY --rules all --source-ranges 0.0.0.0/0 --target-tags "$TAG" >/dev/null
  "${G[@]}" compute firewall-rules describe nb-batch-allow-iap-ssh >/dev/null 2>&1 ||
    "${G[@]}" compute firewall-rules create nb-batch-allow-iap-ssh --network default --direction INGRESS --priority 800 \
      --action ALLOW --rules tcp:22 --source-ranges 35.235.240.0/20 --target-tags "$TAG" >/dev/null
  echo "setup ok: SA $SA_ID (delete/setMetadata on nb-batch-* only), firewall nb-batch-deny-ingress + nb-batch-allow-iap-ssh"
}

cmd_up() {  # up <id> <runners> [machine]
  local id=$1 n=$2 m=${3:-$MACHINE} name=nb-batch-$1 d=$ST/$1 now dl dr tf sf jf img rc=0
  mkdir -p "$d"; chmod 700 "$d"
  now=$(date +%s); dl=$((now + LIFETIME_MIN * 60)); dr=$((dl - DRAIN_MIN * 60))
  sf=$(mktemp); jf=$(mktemp); tf=$(mktemp); chmod 600 "$tf"
  vm_script > "$sf"; jump_script > "$jf"
  gh_ api -X POST "orgs/$ORG/actions/runners/registration-token" --jq .token > "$tf"
  if "${G[@]}" compute images describe-from-family "$IMAGE_FAMILY" >/dev/null 2>&1; then
    img=(--image-family "$IMAGE_FAMILY" --image-project "$PROJECT")
  else img=(--image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud); fi
  log "creating $name ($m Spot, $n runners, hard delete in ${LIFETIME_MIN}m)"
  "${G[@]}" compute instances create "$name" --zone "$ZONE" --machine-type "$m" "${img[@]}" \
    --boot-disk-size "${DISK_GB}GB" --boot-disk-type pd-balanced --boot-disk-auto-delete \
    --provisioning-model=SPOT --instance-termination-action=DELETE --max-run-duration="${LIFETIME_MIN}m" \
    --service-account "$SA" --scopes https://www.googleapis.com/auth/compute \
    --tags "$TAG" --labels "nb-batch=$id" \
    --metadata "^;^runner-count=$n;org=$ORG;runner-labels=$RUNNER_LABELS;runner-prefix=$PREFIX-$id;deadline=$dl;drain-at=$dr;idle-min=$IDLE_MIN;first-job-min=$FIRST_JOB_MIN;jump-host=$JUMP_HOST;ssh-targets=$SSH_TARGETS;block-project-ssh-keys=TRUE;enable-oslogin=TRUE" \
    --metadata-from-file "startup-script=$sf,nb-jump=$jf,reg-token=$tf" >/dev/null || rc=$?
  rm -f "$tf" "$sf" "$jf"
  [ "$rc" = 0 ] || die "VM create failed ($rc)"
  echo "$dl" > "$d/deadline"; echo "$dr" > "$d/route-expiry"
  local on=0 i
  for i in $(seq 60); do on=$(runners_online "$PREFIX-$id-"); [ "$on" -ge "$n" ] && break; sleep 10; done
  log "runners online $on/$n after $(( $(date +%s) - now ))s"
  local ser=""
  for i in $(seq 12); do   # deploy-target ssh check runs right after registration
    ser=$("${G[@]}" compute instances get-serial-port-output "$name" --zone "$ZONE" 2>/dev/null | grep -aoE 'NB-[A-Z-]+( [^[:cntrl:]]*)?' | sort -u || true)
    [ "$(grep -c 'SSH-' <<<"$ser")" -ge "$(wc -w <<<"$SSH_TARGETS")" ] && break; sleep 10; done
  sed 's/^/  vm: /' <<<"$ser"
  [ "$on" -gt 0 ] || return 1
  grep -q "SSH-OK ${SSH_TARGETS%% *}" <<<"$ser" || { log "VM cannot reach ${SSH_TARGETS%% *}:22 (deploys would fail)"; return 1; }
}

cmd_hold() {  # hold <id> [min]: live-change the VM agent's idle limit (read from instance metadata every minute)
  local id=$1 min=${2:-40} got
  "${G[@]}" compute instances add-metadata "nb-batch-$id" --zone "$ZONE" --metadata "idle-min=$min" >/dev/null ||
    die "hold: add-metadata failed for nb-batch-$id"
  # read it back: the agent only sees what is really in the instance metadata
  got=$("${G[@]}" compute instances describe "nb-batch-$id" --zone "$ZONE" \
        --format='value(metadata.items.filter(key:idle-min).extract(value).flatten())' 2>/dev/null | tr -d '[:space:]')
  [ "$got" = "$min" ] || die "hold: idle-min on nb-batch-$id is '${got:-unset}', wanted $min"
  log "idle limit for nb-batch-$id = ${min}m (verified)"
}
cmd_release() { cmd_hold "$1" "$IDLE_MIN"; }

cmd_route_off() { local id; id=$(need_id "${1:-}"); local r
  [ -f "$ST/$id/repos.tsv" ] || return 0
  awk -F'\t' '$8=="OK"{print $2}' "$ST/$id/repos.tsv" | sort -u | while read -r r; do
    set_route "$r" off && log "NB_BURST=off $r"; done
}

proof() {  # nothing billable left: instances, disks, addresses with the nb-batch prefix
  have_gcloud || { log "no gcloud here: verify from the laptop with nb-batch.sh status"; return 0; }
  local i d a
  i=$("${G[@]}" compute instances list --filter="name~^nb-batch-" --format='value(name)' | tr '\n' ' ') || i='?error'
  d=$("${G[@]}" compute disks list --filter="name~^nb-batch-" --format='value(name)' | tr '\n' ' ') || d='?error'
  a=$("${G[@]}" compute addresses list --filter="name~^nb-batch" --format='value(name)' | tr '\n' ' ') || a='?error'
  log "left in GCP: instances=[${i% }] disks=[${d% }] addresses=[${a% }]"
  [ -z "$i$d$a" ]
}

cmd_teardown() {  # teardown <id>: routing off -> VM deleted (or self-deletes) -> runners deregistered -> proof
  local id=$1 n z
  cmd_route_off "$id" || true
  if have_gcloud; then
    { "${G[@]}" compute instances list --filter="labels.nb-batch=$id" --format='value(name,zone.basename())' || true; } |
      while read -r n z; do "${G[@]}" compute instances delete "$n" --zone "$z" >/dev/null 2>&1 && log "VM $n deleted" || true; done
    { "${G[@]}" compute disks list --filter="name~^nb-batch-$id" --format='value(name,zone.basename())' || true; } |
      while read -r n z; do "${G[@]}" compute disks delete "$n" --zone "$z" >/dev/null 2>&1 && log "disk $n deleted" || true; done
  else
    log "no gcloud: VM deletes itself ${IDLE_MIN} idle min after its runners are removed (hard cap ${LIFETIME_MIN}m)"
  fi
  deregister "$PREFIX-$id-"
  log "runners left with prefix $PREFIX-$id-: $(runners | awk -F'\t' -v p="$PREFIX-$id-" 'index($2,p)==1' | grep -c . || true)"
  proof || true
  date +%s > "$ST/$id/torn-down"
}

# ---------------------------------------------------------------------------------------------------------------------
# Watcher (python, gh only): push -> PR -> gate + checks -> merge -> deploy runs, per leg; report.
engine_py() { cat <<'EOP'
import json,os,subprocess,sys,time,threading,calendar
D=sys.argv[1]; SELF=os.environ['NB_SELF']; WS=os.environ['NB_WS']
O=json.load(open(D+'/opts.json')); ID=O['id']; POLL=int(O.get('poll',30)); PFX=O['prefix']
ENV=dict(os.environ); ENV.pop('GH_TOKEN',None)
OKC={'success','skipped','neutral'}; BADC={'FAILURE','CANCELLED','TIMED_OUT','ACTION_REQUIRED','STARTUP_FAILURE','ERROR'}
def log(m): print(time.strftime('%H:%M:%S'),m,flush=True)
def run(a,cwd=None,t=600,env=None):
  try: return subprocess.run(a,cwd=cwd,env=env or ENV,capture_output=True,text=True,timeout=t)
  except subprocess.TimeoutExpired as e: return subprocess.CompletedProcess(a,124,'',str(e))
def gh(*a,t=120): return run(['gh',*a],t=t)
def api(path):
  p=gh('api',path);
  try: return json.loads(p.stdout) if p.returncode==0 else None
  except ValueError: return None
SP=D+'/state.json'
if os.path.exists(SP): S=json.load(open(SP))
else:
  S={'repos':[]}
  for l in open(D+'/repos.tsv'):
    f=l.rstrip('\n').split('\t')
    if len(f)<8 or f[7]!='OK': continue
    legs=[{'base':f[3],'head':f[2],'st':'push'}]
    if O['main'] and f[3]=='develop': legs.append({'base':'main','head':'develop','st':'wait-prev'})
    S['repos'].append({'dir':os.path.join(WS,f[0]),'repo':f[1],'branch':f[2],'legs':legs,'notes':[]})
LOCK=threading.Lock()
def save():
  with LOCK: json.dump(S,open(SP+'.tmp','w'),indent=1); os.replace(SP+'.tmp',SP)
def note(r,m): r['notes'].append(m); log('%s: %s'%(r['repo'],m))
def done(L,res,why=''): L['st']='done'; L['result']=res; L['why']=why

# --- gate (Mac Mini merge gate via scripts/merge-gate.sh; it queues on host locks itself) ---
GATES={}
def gate(r,L):
  k=(r['repo'],L['pr'],L['sha'])
  if O['gate']!='on':
    if k not in GATES:
      GATES[k]='pass'
      with open(os.path.join(os.environ.get('TMPDIR','/tmp'),'so360-merge-gate-skips.log'),'a') as f:
        f.write('%s SKIP %s#%s -> %s %s by nb-batch %s (gate=ci-only)\n'%(time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),r['repo'],L['pr'],L['base'],L['sha'][:9],ID))
    return 'pass'
  if k not in GATES:
    GATES[k]='running'
    def go():
      e=dict(ENV,GATE_EXPECT_SHA=L['sha']); p=run([os.path.join(WS,'scripts/merge-gate.sh'),r['repo'],str(L['pr']),L['base']],t=3600,env=e)
      GATES[k]='pass' if p.returncode==0 else 'fail'
      open('%s/gate-%s-%s.log'%(D,r['repo'].split('/')[1],L['pr']),'w').write(p.stdout[-20000:]+p.stderr[-5000:])
    threading.Thread(target=go,daemon=True).start()
  return GATES[k]

# --- failed run triage: a run that lost its burst runner (no failed step) is re-run once ---
def infra_retry(r,L,runs):
  for w in runs:
    if w.get('conclusion') not in ('failure','cancelled') or w.get('run_attempt',1)>1 or w['id'] in L.setdefault('retried',[]): continue
    js=(api('repos/%s/actions/runs/%s/jobs?per_page=100'%(r['repo'],w['id'])) or {}).get('jobs',[])
    lost=[j for j in js if j.get('conclusion') in ('failure','cancelled') and (j.get('runner_name') or '').startswith(PFX)
          and not any(s.get('conclusion')=='failure' for s in j.get('steps') or [])]
    if lost and all(j.get('conclusion') in OKC or j in lost for j in js if j.get('conclusion')):
      if gh('run','rerun',str(w['id']),'--failed','-R',r['repo']).returncode==0:
        L['retried'].append(w['id']); note(r,'run %s lost its burst runner: re-run once (%s)'%(w['id'],w['html_url'])); return True
  return False

def pr_open(r,L):
  p=gh('pr','list','-R',r['repo'],'--head',L['head'],'--base',L['base'],'--state','open','--json','number,headRefOid')
  ex=json.loads(p.stdout or '[]') if p.returncode==0 else []
  if ex: L['pr'],L['sha']=ex[0]['number'],ex[0]['headRefOid']; return True
  if L['base']=='main':
    title='release: develop -> main (nb-batch %s)'%ID; body='Release of develop to main after green develop deploys (nb-batch %s).'%ID
  else:
    subj=run(['git','-C',r['dir'],'log','--format=%s','origin/%s..%s'%(L['base'],L['head'])]).stdout.strip().splitlines()
    title=subj[-1] if len(subj)==1 else '%s (%d commits)'%(L['head'],len(subj))
    body='Batch %s (scripts/nb-batch.sh).\n\nCommits:\n%s'%(ID,'\n'.join('- '+s for s in reversed(subj)))
  p=gh('pr','create','-R',r['repo'],'--base',L['base'],'--head',L['head'],'--title',title[:250],'--body',body)
  if p.returncode: done(L,'red','PR create failed: '+(p.stderr or p.stdout).strip()[-300:]); return False
  return pr_open(r,L)

def checks(r,L):
  p=gh('pr','view',str(L['pr']),'-R',r['repo'],'--json','state,headRefOid,mergeStateStatus,statusCheckRollup,mergeCommit')
  if p.returncode: return 'pending',None
  j=json.loads(p.stdout)
  if j['state']=='MERGED': return 'merged',j
  if j['state']=='CLOSED': return 'closed',j
  if j['headRefOid']!=L['sha']: return 'moved',j
  roll=j.get('statusCheckRollup') or []
  st=lambda c:(c.get('conclusion') or c.get('state') or '').upper()
  if any(st(c) in BADC for c in roll): return 'failed',j
  pend=[c for c in roll if (c.get('__typename')=='CheckRun' and c.get('status')!='COMPLETED') or st(c) in ('PENDING','EXPECTED')]
  if pend or (not roll and time.time()-L['pr_at']<180): return 'pending',j
  return 'green',j

def step(r,i,L):
  st=L['st']
  if st=='wait-prev':
    prev=r['legs'][i-1]
    if prev['st']!='done': return
    if prev['result']!='green': return done(L,'skipped','develop leg not green')
    c=api('repos/%s/compare/main...develop'%r['repo'])
    if c is not None and c.get('ahead_by',1)==0: return done(L,'green','main already has develop')
    if c: L['carry']=c.get('ahead_by')
    L['st']='pr'; st='pr'
  if st=='push':
    p=run(['git','-C',r['dir'],'push','-u','origin',L['head']],t=900)
    if p.returncode: return done(L,'red','push rejected: '+p.stderr.strip()[-300:])
    note(r,'pushed %s'%L['head']); L['st']='pr'; st='pr'
  if st=='pr':
    if not pr_open(r,L): return
    L['pr_at']=time.time(); note(r,'PR #%s -> %s (%s)'%(L['pr'],L['base'],L['sha'][:9])); L['st']='checks'; return
  if st=='checks':
    c,j=checks(r,L)
    if c=='merged': L['merge_sha']=(j.get('mergeCommit') or {}).get('oid'); L['st']='deploy'; L['dep_at']=time.time(); return
    if c=='closed': return done(L,'red','PR #%s closed'%L['pr'])
    if c=='moved':   # new commits on the PR head (e.g. someone merged to develop): gate + checks start again
      L['sha']=j['headRefOid']; L['pr_at']=time.time(); note(r,'PR #%s head moved to %s: re-checking'%(L['pr'],L['sha'][:9])); return
    if c=='failed':
      runs=(api('repos/%s/actions/runs?head_sha=%s&per_page=50'%(r['repo'],L['sha'])) or {}).get('workflow_runs',[])
      if infra_retry(r,L,runs): return
      return done(L,'red','PR #%s checks failed'%L['pr'])
    g=gate(r,L)
    if g=='fail': return done(L,'red','Mac Mini merge gate FAILED on #%s (log %s/gate-*)'%(L['pr'],D))
    if c=='green' and g=='pass':
      p=gh('pr','merge',str(L['pr']),'-R',r['repo'],'--merge','--match-head-commit',L['sha'])
      if p.returncode: return done(L,'red','merge refused: '+(p.stderr or p.stdout).strip()[-300:])
      c,j=checks(r,L); L['merge_sha']=((j or {}).get('mergeCommit') or {}).get('oid')
      note(r,'merged #%s into %s (%s)'%(L['pr'],L['base'],(L['merge_sha'] or '?')[:9])); L['st']='deploy'; L['dep_at']=time.time()
    return
  if st=='deploy':
    if not L.get('merge_sha'):
      c,j=checks(r,L); L['merge_sha']=((j or {}).get('mergeCommit') or {}).get('oid')
      if not L['merge_sha']: return
    runs=(api('repos/%s/actions/runs?head_sha=%s&branch=%s&event=push&per_page=50'%(r['repo'],L['merge_sha'],L['base'])) or {}).get('workflow_runs')
    if runs is None: return
    if not runs:
      if time.time()-L['dep_at']>300: done(L,'green','no push workflow ran for %s in 5 min'%L['base'])
      return
    if any(w['status']!='completed' for w in runs): return
    bad=[w for w in runs if w.get('conclusion') not in OKC]
    if bad and infra_retry(r,L,bad): return
    if bad: return done(L,'red','%s deploy failed: %s'%(L['base'],' '.join(w['html_url'] for w in bad)))
    note(r,'%s deploy green (%s)'%(L['base'],', '.join(w['name'] for w in runs))); return done(L,'green')

# --- VM loss / drain: routing off, queued lane-burst runs cancelled and re-run (attempt 2 routes to Contabo) ---
zero=0; seen=False; off=False; cancelled={}
def watchdog():
  global zero,seen,off
  p=gh('api','--paginate','orgs/Neonbee-ai/actions/runners?per_page=100','--jq','[.runners[]|select((.name|startswith("%s")) and .status=="online")]|length'%PFX)
  on=sum(int(x) for x in p.stdout.split() if x.isdigit()) if p.returncode==0 else -1
  if on>0: seen=True; zero=0
  elif on==0 and seen: zero+=1
  expired=time.time()>=O['route_expiry']
  if not off and (zero>=3 or expired):
    off=True; log('burst %s: routing off for the batch repos'%('window over' if expired else 'runners gone'))
    run([SELF,'route-off',ID],t=300)
  if off:
    for r in S['repos']:
      for w in (api('repos/%s/actions/runs?status=queued&per_page=30'%r['repo']) or {}).get('workflow_runs',[]):
        age=time.time()-calendar.timegm(time.strptime(w['created_at'],'%Y-%m-%dT%H:%M:%SZ'))
        if w['id'] in cancelled or age<120 or w.get('run_attempt',1)!=1: continue
        js=(api('repos/%s/actions/runs/%s/jobs?per_page=100'%(r['repo'],w['id'])) or {}).get('jobs',[])
        if not any(j.get('status')=='queued' and 'lane-burst' in (j.get('labels') or []) for j in js): continue
        if gh('run','cancel',str(w['id']),'-R',r['repo']).returncode==0:
          cancelled[w['id']]=r['repo']; note(r,'run %s queued on burst: cancelled, re-run goes to Contabo'%w['id'])
    for wid,repo in list(cancelled.items()):
      if repo and gh('run','rerun',str(wid),'-R',repo).returncode==0: cancelled[wid]=None
  return on

end=O['route_expiry']+O['drain_s']+1800   # report deadline: everything still open after this is 'timeout'
last=0
while True:
  if os.path.exists(D+'/handed-off'): save(); log('handed off'); sys.exit(3)
  for r in S['repos']:
    for i,L in enumerate(r['legs']):
      if L['st']!='done':
        try: step(r,i,L)
        except Exception as e: log('%s leg %d: %r (retrying)'%(r['repo'],i,e))
  on=watchdog(); save()
  openl=[L for r in S['repos'] for L in r['legs'] if L['st']!='done']
  if time.time()-last>300: last=time.time(); log('progress: %d open leg(s), %s burst runner(s) online'%(len(openl),on))
  if not openl: break
  if time.time()>end:
    for r in S['repos']:
      for L in r['legs']:
        if L['st']!='done': done(L,'timeout','still %s at the report deadline'%L['st'])
    break
  time.sleep(POLL)
save()
rows=[]; red=0
for r in S['repos']:
  cell=[]
  for L in r['legs']:
    cell.append('%s:%s%s'%(L['base'],L.get('result','?'),' #%s'%L['pr'] if L.get('pr') else ''))
    if L.get('result') not in ('green','skipped'): red+=1
  why='; '.join(L.get('why','') for L in r['legs'] if L.get('why'))
  rows.append('%-40s %-60s %s'%(r['repo'],'  '.join(cell),why))
rep='nb-batch %s report (%s)\n%s\n'%(ID,time.strftime('%Y-%m-%d %H:%M'),'\n'.join(rows))
open(D+'/report.txt','w').write(rep); print(rep,flush=True)
sys.exit(1 if red else 0)
EOP
}

cmd_run() {  # run <id>: the batch itself (normally detached by go)
  local id=$1 d=$ST/$1 rc=0 r exp ep=
  echo $$ > "$d/pid"
  trap 'kill $ep 2>/dev/null; exit 143' TERM INT
  trap '[ -f "'"$d"'/handed-off" ] || cmd_teardown "'"$id"'"' EXIT
  cmd_up "$id" "$RUNNERS" || { log "no runner came online: aborting"; exit 1; }
  exp=$(cat "$d/route-expiry")
  awk -F'\t' '$8=="OK"{print $2}' "$d/repos.tsv" | sort -u | while read -r r; do
    set_route "$r" "batch@$exp" && log "NB_BURST=batch@$exp $r"; done
  python3 -c 'import json,sys; o=json.load(open(sys.argv[1])); o["route_expiry"]=int(sys.argv[2]); o["drain_s"]=int(sys.argv[3]); json.dump(o,open(sys.argv[1],"w"))' \
    "$d/opts.json" "$exp" $((DRAIN_MIN * 60))
  NB_SELF=$SELF NB_WS=$WS python3 <(engine_py) "$d" & ep=$!
  wait "$ep" || rc=$?
  [ "$rc" = 3 ] && { log "watcher handed off; no teardown here"; exit 0; }
  exit "$rc"
}

cmd_resume() {  # resume <id>: watcher only (e.g. on the Mac Mini after handoff); teardown = routing off + runners
  local id=$1 d=$ST/$1 rc=0 ep=
  echo $$ > "$d/pid"; rm -f "$d/handed-off"
  trap 'kill $ep 2>/dev/null; exit 143' TERM INT
  trap 'cmd_teardown "'"$id"'"' EXIT
  NB_SELF=$SELF NB_WS=$WS python3 <(engine_py) "$d" & ep=$!
  wait "$ep" || rc=$?
  exit "$rc"
}

cmd_go() {
  parse_sel "$@"
  local tsv n id d
  cmd_plan "$@" || true
  tsv=$(awk -F'\t' '$8=="OK"' "$ST/plan.tsv"); n=$(grep -c . <<<"$tsv" || true)
  [ "$n" -gt 0 ] || die "nothing to deliver"
  preflight >/dev/null || die "preflight blocked (see above)"
  [ "$GATE" = on ] && [ ! -x "$WS/scripts/merge-gate.sh" ] && die "--gate on needs $WS/scripts/merge-gate.sh"
  if [ "$YES" != 1 ]; then
    printf '\nThis pushes %s branch(es), opens PRs, merges to develop%s and deploys. Type GO to start: ' "$n" \
      "$([ "$MAIN" = 1 ] && echo ', then develop->main (prod HA)')"
    read -r a; [ "$a" = GO ] || die "not started"
  fi
  id=$(date +%Y%m%d-%H%M); d=$ST/$id; mkdir -p "$d"; chmod 700 "$d"
  cp "$ST/plan.tsv" "$d/repos.tsv"
  printf '{"id":"%s","main":%s,"gate":"%s","poll":%s,"prefix":"%s","route_expiry":0,"drain_s":0}\n' \
    "$id" "$([ "$MAIN" = 1 ] && echo true || echo false)" "$GATE" "${NB_POLL_S:-30}" "$PREFIX-$id-" > "$d/opts.json"
  if [ "$FG" = 1 ]; then cmd_run "$id"; return; fi
  nohup caffeinate -i "$SELF" run "$id" > "$d/log" 2>&1 &
  echo "$!" > "$d/pid"
  echo "batch $id started (pid $!). Follow: tail -f $d/log   Status: $SELF status   Stop: $SELF abort"
}

cmd_status() {
  local id; id=${1:-$(latest_id)}
  if [ -n "$id" ] && [ -d "$ST/$id" ]; then
    echo "== batch $id: controller $(alive "$id" && echo "running (pid $(cat "$ST/$id/pid"))" || echo stopped)$([ -f "$ST/$id/torn-down" ] && echo ', torn down')"
    [ -f "$ST/$id/state.json" ] && python3 - "$ST/$id/state.json" <<'EOP'
import json,sys
for r in json.load(open(sys.argv[1]))['repos']:
  print('   %-40s %s'%(r['repo'],'  '.join('%s:%s%s'%(L['base'],L.get('result',L['st']),' #%s'%L['pr'] if L.get('pr') else '') for L in r['legs'])))
EOP
    [ -f "$ST/$id/report.txt" ] && echo "   report: $ST/$id/report.txt"
    [ -f "$ST/$id/log" ] && tail -3 "$ST/$id/log" | sed 's/^/   log: /'
  else echo "== no batch state"; fi
  if have_gcloud; then echo "== GCP (nb-batch-*)"
    "${G[@]}" compute instances list --filter="name~^nb-batch-" --format='value(name,status,machineType.basename(),creationTimestamp)' | sed 's/^/   /'
    proof || true; fi
  echo "== GitHub runners $PREFIX-*: $(runners | awk -F'\t' -v p="$PREFIX-" 'index($2,p)==1{n++; if($3=="online")o++; if($4=="true")b++} END{printf "%d registered, %d online, %d busy", n, o, b}')"
}

cmd_abort() {
  local id; id=${1:-$(latest_id)}
  if [ -n "$id" ] && alive "$id"; then kill -TERM "$(cat "$ST/$id/pid")" 2>/dev/null || true
    for _ in $(seq 60); do alive "$id" || break; sleep 3; done; fi
  if [ -n "$id" ] && [ -d "$ST/$id" ]; then cmd_teardown "$id"; fi
  if have_gcloud; then   # anything else with the prefix, whatever batch it came from
    { "${G[@]}" compute instances list --filter="name~^nb-batch-" --format='value(name,zone.basename())' || true; } |
      while read -r n z; do "${G[@]}" compute instances delete "$n" --zone "$z" >/dev/null 2>&1 && log "VM $n deleted" || true; done
  fi
  deregister "$PREFIX-"
  proof
}

cmd_handoff() {  # move the watcher to the Mac Mini: gh only there; the VM self-deletes, routing expires on its own
  local host=${1:-macmini} id d; id=$(need_id ""); d=$ST/$id
  alive "$id" || die "batch $id is not running"
  python3 - "$d/state.json" <<'EOP' || die "handoff only after every branch is pushed and its PR is open"
import json,sys; s=json.load(open(sys.argv[1]))
sys.exit(0 if all(r['legs'][0]['st'] not in ('push','pr') for r in s['repos']) else 1)
EOP
  grep -q '"gate": *"on"' "$d/opts.json" && log "note: the Mini has no merge-gate client; open gates finish here first, later legs use gate=ci-only there"
  touch "$d/handed-off"; for _ in $(seq 30); do alive "$id" || break; sleep 2; done
  sed -i '' 's/"gate": *"on"/"gate": "ci-only"/' "$d/opts.json"
  ssh "$host" "mkdir -p ~/nb-batch ~/.neonbee/batch/$id" && scp -q "$SELF" "$host:nb-batch/nb-batch.sh" &&
    scp -q "$d"/repos.tsv "$d"/opts.json "$d"/state.json "$host:.neonbee/batch/$id/" ||
    { rm -f "$d/handed-off"; die "copy to $host failed; restart here with: $SELF resume $id"; }
  ssh "$host" "export PATH=/opt/homebrew/bin:\$PATH; cd ~/nb-batch && NB_WS=\$HOME/nb-batch nohup bash nb-batch.sh resume $id > ~/.neonbee/batch/$id/log 2>&1 & echo started on \$(hostname)"
  echo "follow: ssh $host tail -f ~/.neonbee/batch/$id/log"
}

cmd_smoke() {  # one runner, registration + jump check, full teardown + proof (~$0.01)
  local id=smoke-$(date +%H%M%S)
  preflight | grep BLOCK | grep -v 'already running' && die "fix the BLOCK lines first"
  mkdir -p "$ST/$id"
  trap 'cmd_teardown "'"$id"'"' EXIT
  local name=nb-batch-$id t0 i
  FIRST_JOB_MIN=10 DISK_GB=30 cmd_up "$id" 1 c2d-highcpu-2 || { log "SMOKE FAIL: runner offline or deploy target unreachable"; return 1; }
  log "GitHub: $(runners | awk -F'\t' -v p="$PREFIX-$id-" 'index($2,p)==1{print $2" "$3}')"
  log "self-delete test: setting nb-done=1 (the VM must delete itself within ~2 min)"
  "${G[@]}" compute instances add-metadata "$name" --zone "$ZONE" --metadata nb-done=1 >/dev/null; t0=$(date +%s)
  for i in $(seq 36); do "${G[@]}" compute instances describe "$name" --zone "$ZONE" --format='value(status)' >/dev/null 2>&1 || break; sleep 5; done
  "${G[@]}" compute instances describe "$name" --zone "$ZONE" >/dev/null 2>&1 &&
    log "SMOKE FAIL: VM still exists after $(( $(date +%s) - t0 ))s (teardown deletes it)" ||
    log "self-delete OK: VM gone $(( $(date +%s) - t0 ))s after the done signal"
}

case "${1:-}" in
  setup) cmd_setup ;;
  plan) shift; cmd_plan "$@" ;;
  go) shift; cmd_go "$@" ;;
  status) cmd_status "${2:-}" ;;
  abort) cmd_abort "${2:-}" ;;
  handoff) cmd_handoff "${2:-}" ;;
  smoke) cmd_smoke ;;
  up) cmd_up "${2:?id}" "${3:-$RUNNERS}" "${4:-}" ;;
  run) cmd_run "${2:?id}" ;;
  resume) cmd_resume "${2:?id}" ;;
  teardown) cmd_teardown "${2:?id}" ;;
  hold) cmd_hold "${2:?id}" "${3:-}" ;;
  release) cmd_release "${2:?id}" ;;
  route-off) cmd_route_off "${2:-}" ;;
  vm-script) vm_script ;;
  *) sed -n '2,32p' "$SELF"; exit 1 ;;
esac
