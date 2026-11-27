#!/bin/bash
# nb-ci controller VM boot (e2-micro, ubuntu-2404, startup-script; safe to re-run on every boot).
# Instance metadata: nbci-ref (git ref of neonbee-devops), nbci-app-id, nbci-installation-id,
# nbci-dry-run (1|0), nbci-train-repos. Secrets come from Secret Manager via the attached SA.
set -uo pipefail
MD=http://metadata.google.internal/computeMetadata/v1
md() { curl -sf -m 5 -H 'Metadata-Flavor: Google' "$MD/$1"; }
attr() { md "instance/attributes/$1"; }
P=$(md project/project-id); REF=${NBCI_REF_OVERRIDE:-$(attr nbci-ref || echo main)}
APP=$(attr nbci-app-id || true); INST=$(attr nbci-installation-id || true)
export DEBIAN_FRONTEND=noninteractive
[ "$0" = /opt/nbci-bootstrap.sh ] || { md instance/attributes/startup-script > /opt/nbci-bootstrap.sh && chmod 700 /opt/nbci-bootstrap.sh; } || true

if [ ! -f /var/lib/nbci/.base ]; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab
  apt-get update -q && apt-get install -yq git jq curl openssl python3 iptables debian-keyring debian-archive-keyring apt-transport-https
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q && apt-get install -yq caddy
  id nbci >/dev/null 2>&1 || useradd -r -m -s /usr/sbin/nologin nbci
  id ghr >/dev/null 2>&1 || useradd -m -s /bin/bash ghr
  mkdir -p /var/lib/nbci && touch /var/lib/nbci/.base
fi
# route job's Pulse-trailer step calls `gh api` (Ubuntu 24.04 universe package); idempotent for old VMs
command -v gh >/dev/null 2>&1 || apt-get install -yq gh
# night train (night-train.mjs) runs on node; Ubuntu 24.04 nodejs 18 has fetch + AbortSignal.timeout
command -v node >/dev/null 2>&1 || apt-get install -yq nodejs
# OS security patches: unattended-upgrades (security pocket), reboot at 04:40 UTC when a kernel/libc patch needs it
dpkg -s unattended-upgrades >/dev/null 2>&1 || apt-get install -yq unattended-upgrades
printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' > /etc/apt/apt.conf.d/20auto-upgrades
printf 'Unattended-Upgrade::Automatic-Reboot "true";\nUnattended-Upgrade::Automatic-Reboot-Time "04:40";\n' > /etc/apt/apt.conf.d/52nbci-reboot
systemctl enable --now unattended-upgrades apt-daily-upgrade.timer >/dev/null 2>&1 || true
git config --system --get-all safe.directory | grep -qx /opt/nbci/src || git config --system --add safe.directory /opt/nbci/src

# GitHub App installation token (JWT signed by openssl; key only in a 0600 tmpfs file for a moment)
gh_token() {
  local tok key jwt h b s
  tok=$(md instance/service-accounts/default/token | jq -r .access_token)
  key=$(mktemp -p /run); chmod 600 "$key"
  curl -sf -H "Authorization: Bearer $tok" "https://secretmanager.googleapis.com/v1/projects/$P/secrets/nbci-github-app-key/versions/latest:access" |
    jq -r .payload.data | base64 -d > "$key"
  b64u() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
  h=$(printf '{"alg":"RS256","typ":"JWT"}' | b64u); b=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' $(( $(date +%s) - 60 )) $(( $(date +%s) + 540 )) "$APP" | b64u)
  s=$(printf '%s.%s' "$h" "$b" | openssl dgst -sha256 -sign "$key" | b64u); rm -f "$key"
  curl -sf -X POST -H "Authorization: Bearer $h.$b.$s" -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/app/installations/$INST/access_tokens" | jq -r .token
}
TOKEN=; [ -n "$APP" ] && [ -n "$INST" ] && { TOKEN=$(gh_token) || TOKEN=; }; [ "$TOKEN" = null ] && TOKEN=
SRC=/opt/nbci/src
OLD_SHA=${NBCI_OLD_SHA:-$(git -C $SRC rev-parse HEAD 2>/dev/null || true)}   # survives the re-exec below
if [ -n "$TOKEN" ]; then
  if [ -d $SRC/.git ]; then git -C $SRC fetch -q "https://x-access-token:$TOKEN@github.com/Neonbee-ai/neonbee-devops.git" "$REF" && git -C $SRC checkout -q -f FETCH_HEAD
  else mkdir -p /opt/nbci && git clone -q --depth 50 -b "$REF" "https://x-access-token:$TOKEN@github.com/Neonbee-ai/neonbee-devops.git" $SRC; fi
  git -C $SRC remote set-url origin https://github.com/Neonbee-ai/neonbee-devops.git
  # bash reads this file while it runs: after a checkout, re-run the NEW bootstrap once (2026-11-27 a deploy kept
  # the old unit files until a second run)
  [ -n "${NBCI_REEXEC:-}" ] || NBCI_REEXEC=1 NBCI_OLD_SHA="$OLD_SHA" exec "$SRC/runner/controller/bootstrap.sh" "$@"
fi
# nbci-update.timer (every 10 min): only move when the ref has a new sha that did not already fail its health check
if [ "${NBCI_ONLY_IF_CHANGED:-}" = 1 ]; then
  NEW_SHA=$(git -C $SRC rev-parse HEAD 2>/dev/null || true)
  if [ -z "$NEW_SHA" ] || [ "$NEW_SHA" = "$OLD_SHA" ] || [ "$NEW_SHA" = "$(cat /var/lib/nbci/bad-ref 2>/dev/null)" ]; then
    [ -n "$OLD_SHA" ] && [ "$NEW_SHA" != "$OLD_SHA" ] && git -C $SRC checkout -q -f "$OLD_SHA"
    exit 0
  fi
  # follow main only to a commit whose GitHub check runs are all completed green (none/pending/failed: stay, retry in 10 min)
  if ! GH_TOKEN="$TOKEN" python3 "$SRC/runner/controller/nbci_update.py" --check-green "$NEW_SHA"; then
    echo "nbci-update: $NEW_SHA not green yet, staying on ${OLD_SHA:-none}"
    [ -n "$OLD_SHA" ] && git -C $SRC checkout -q -f "$OLD_SHA"
    exit 0
  fi
fi
if [ ! -f $SRC/runner/controller/nbci.py ]; then
  # No App key / ids yet (or GitHub down): nothing to run. Retry every 15 min from cron, exit 0 so
  # there is no crash loop and no spend; the first good run replaces this cron with the daily one.
  echo "nbci source missing (GitHub App key/ids not configured?); retrying every 15 min"
  echo '*/15 * * * * root flock -n /run/nbci-boot.lock /opt/nbci-bootstrap.sh >>/var/log/nbci-update.log 2>&1' > /etc/cron.d/nbci
  exit 0
fi

cat > /etc/nbci.env <<ENV
GCP_PROJECT=$P
GH_APP_ID=$APP
GH_APP_INSTALLATION_ID=$INST
DRY_RUN=$(attr nbci-dry-run || echo 1)
TRAIN_REPOS=$(attr nbci-train-repos || true)
PULSE_MCP_URL=$(attr nbci-pulse-url || echo https://pulse.moonhive.in/mcp)
NBCI_ALERT_TASK=$(attr nbci-alert-task || echo 71b8be5b)
NBCI_HOST=$(attr nbci-host || echo nbci.moonhive.in)
NB_DAILY_VM_MIN=$(attr nbci-daily-vm-min || echo 3000)
NB_DAILY_CAP_USD=$(attr nbci-daily-cap || echo 4)
RUNNER_BOOT=$(attr nbci-runner-boot || echo image)
HANDS_OFF_LEGACY=$(attr nbci-hands-off-legacy || echo 1)
ENV
cat > /etc/systemd/system/nbci.service <<'U'
[Unit]
Description=nb-ci controller (runner VMs, fallback, merge train)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0
[Service]
Type=notify
User=nbci
EnvironmentFile=/etc/nbci.env
StateDirectory=nbci
RuntimeDirectory=nbci
ExecStart=/usr/bin/python3 /opt/nbci/src/runner/controller/nbci.py loop
Restart=always
RestartSec=10
WatchdogSec=900
NotifyAccess=main
MemoryMax=300M
CPUWeight=1000
Nice=-5
[Install]
WantedBy=multi-user.target
U
install -m 644 /opt/nbci/src/runner/controller/nbci-guard.service /etc/systemd/system/nbci-guard.service
# Auto-update (Pulse 652ffcd8): every 10 min nbci_update.py moves the controller to the new sha of nbci-ref (a branch
# name such as main; a pinned sha never moves), health-checks it and rolls back + alerts when it fails.
cat > /etc/systemd/system/nbci-update.service <<'U'
[Unit]
Description=nb-ci controller auto-update (follow nbci-ref, health check, rollback)
After=network-online.target
[Service]
Type=oneshot
EnvironmentFile=/etc/nbci.env
ExecStart=/usr/bin/flock -n /run/nbci-boot.lock /usr/bin/python3 /opt/nbci/src/runner/controller/nbci_update.py
TimeoutStartSec=1500
Nice=10
U
cat > /etc/systemd/system/nbci-update.timer <<'U'
[Unit]
Description=nb-ci controller auto-update every 10 min
[Timer]
OnBootSec=10min
OnUnitInactiveSec=10min
AccuracySec=30s
[Install]
WantedBy=timers.target
U
# Night train (overnight develop->main release train, runs only here). Metadata nbci-night-train
# off|dry|live (default off), nbci-nt-deadline (ISO UTC; the train stops after it), nbci-nt-aqa-repos (comma list).
cat > /etc/night-train.env <<ENV
NT_MODE=$(attr nbci-night-train || echo off)
NT_DEADLINE=$(attr nbci-nt-deadline || true)
NT_AQA_REPOS=$(attr nbci-nt-aqa-repos || true)
NT_HOME=/var/lib/night-train
HOME=/var/lib/night-train
ENV
# Continuous merge train (nbci_train.py): metadata nbci-merge-train develop|off (default develop = ON for develop),
# nbci-promote-main on|off (default off; develop->main only after a real Dev AutoQA pass for the exact sha).
cat > /etc/merge-train.env <<ENV
MT_TRAIN=$(attr nbci-merge-train || echo develop)
MT_PROMOTE=$(attr nbci-promote-main || echo off)
MT_HOME=/var/lib/merge-train
HOME=/var/lib/merge-train
ENV
cat > /etc/systemd/system/merge-train.service <<'U'
[Unit]
Description=merge train: merge green develop PRs; optional gated develop->main promotion
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
User=nbci
EnvironmentFile=/etc/nbci.env
EnvironmentFile=/etc/merge-train.env
StateDirectory=merge-train
ExecStart=/usr/bin/python3 /opt/nbci/src/runner/controller/merge-train-run.py
TimeoutStartSec=280
MemoryMax=250M
Nice=5
U
cat > /etc/systemd/system/merge-train.timer <<'U'
[Unit]
Description=merge train every 5 min
[Timer]
OnCalendar=*:0/5
AccuracySec=30s
[Install]
WantedBy=timers.target
U
cat > /etc/systemd/system/night-train.service <<'U'
[Unit]
Description=night train: one release tick (merges night-train PRs, develop->main, failures -> Pulse)
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
User=nbci
EnvironmentFile=/etc/nbci.env
EnvironmentFile=/etc/night-train.env
StateDirectory=night-train
RuntimeDirectory=night-train
ExecStart=/usr/bin/python3 /opt/nbci/src/runner/controller/night-train-run.py
TimeoutStartSec=840
MemoryMax=250M
Nice=5
U
cat > /etc/systemd/system/night-train.timer <<'U'
[Unit]
Description=night train every 15 min (the script stops itself after NT_DEADLINE)
[Timer]
OnCalendar=*:0/15
AccuracySec=30s
[Install]
WantedBy=timers.target
U
IP=$(md instance/network-interfaces/0/access-configs/0/external-ip)
HOST=$(attr nbci-host || echo nbci.moonhive.in)
# Fixed host (DNS-only A record kept current by the ops/nbci-dns workflow) + the sslip.io name of today's IP as the
# fallback. Port 443 only (firewall): ACME uses TLS-ALPN on 443, the HTTP challenge and :80 redirects are off.
cat > /etc/caddy/Caddyfile <<C
{
  auto_https disable_redirects
}
$HOST, ${IP//./-}.sslip.io {
  tls {
    issuer acme {
      disable_http_challenge
    }
  }
  handle /gh {
    reverse_proxy 127.0.0.1:8787
  }
  handle /healthz {
    reverse_proxy 127.0.0.1:8787
  }
  respond 404
}
C
cat > /etc/cron.d/nbci <<'CRON'
*/5 * * * * root /opt/nbci/src/runner/controller/watchdog.sh
17 4 * * * root /opt/nbci/src/runner/controller/bootstrap.sh >/var/log/nbci-update.log 2>&1
CRON
systemctl daemon-reload
systemctl enable --now caddy nbci nbci-guard nbci-update.timer
if grep -qE "^MT_TRAIN=develop$" /etc/merge-train.env; then systemctl enable --now merge-train.timer; else systemctl disable --now merge-train.timer 2>/dev/null || true; fi
if grep -qE "^NT_MODE=(dry|live)$" /etc/night-train.env; then systemctl enable --now night-train.timer; else systemctl disable --now night-train.timer 2>/dev/null || true; fi
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 &&
  { systemctl reload caddy || systemctl restart caddy; } || echo 'nbci: Caddyfile invalid - webhook down, polling only'
systemctl restart nbci nbci-guard

# 2 controller-local runners for nb-ctl jobs (auto repos only; legacy nb-route runners are never shared) (route/notify: seconds of curl/jq). User ghr cannot
# reach the metadata server, so job code can never use the controller's service account.
iptables -C OUTPUT -m owner --uid-owner ghr -d 169.254.169.254 -j REJECT 2>/dev/null ||
  iptables -A OUTPUT -m owner --uid-owner ghr -d 169.254.169.254 -j REJECT
if [ -n "$TOKEN" ]; then
  V=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | jq -r .tag_name | sed 's/^v//')
  for i in 1 2; do
    d=/home/ghr/r$i
    [ -f "$d/.runner" ] && { systemctl start "actions.runner.Neonbee-ai.nbci-ctl-$i.service" 2>/dev/null; continue; }
    mkdir -p "$d" && curl -fsSL "https://github.com/actions/runner/releases/download/v$V/actions-runner-linux-x64-$V.tar.gz" | tar xz -C "$d"
    chown -R ghr: "$d"; [ "$i" = 1 ] && "$d/bin/installdependencies.sh" >/dev/null 2>&1
    RT=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" https://api.github.com/orgs/Neonbee-ai/actions/runners/registration-token | jq -r .token)
    sudo -u ghr "$d/config.sh" --unattended --replace --url https://github.com/Neonbee-ai --token "$RT" \
      --name "nbci-ctl-$i" --labels nb-ctl,nb-route --work _work
    (cd "$d" && ./svc.sh install ghr && ./svc.sh start)
  done
fi
# The loop owns the e2-micro: with both runners busy (load 4-5) a pass took up to 522 s (2026-11-27), so slot
# refills and VM buys lagged minutes. Runner jobs yield CPU and are squeezed (never killed) under memory pressure.
for i in 1 2; do
  mkdir -p "/etc/systemd/system/actions.runner.Neonbee-ai.nbci-ctl-$i.service.d"
  printf '[Service]\nCPUWeight=20\nNice=10\nIOWeight=20\nMemoryHigh=280M\n' \
    > "/etc/systemd/system/actions.runner.Neonbee-ai.nbci-ctl-$i.service.d/nbci-yield.conf"
done
systemctl daemon-reload
# applied live, no restart (a restart would kill a running job); Nice takes effect at the runner's next start
for i in 1 2; do systemctl set-property --runtime "actions.runner.Neonbee-ai.nbci-ctl-$i.service" CPUWeight=20 IOWeight=20 MemoryHigh=280M 2>/dev/null || true; done
