#!/bin/bash
# GCE startup-script for the label-driven burst VM (neonbee-ci-burst). Stock Ubuntu 24.04 boots, pulls the
# pre-built bundle (release `burst-bundle` of this public repo; built by burst-bundle.yml), registers
# gcp-burst-01..N org runners and arms the idle watchdog that DELETES this VM (and its auto-delete disk)
# after 3 idle minutes. If the bundle is missing or corrupt it falls back to upstream downloads.
#
# Metadata (set by scripts/burst/burst-up.sh): runner-count, reg-token, runner-labels, org, bundle-base, nb-jump.
set -uo pipefail
exec >>/var/log/nb-burst-startup.log 2>&1
md() { curl -sf -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/$1"; }
mda() { md "instance/attributes/$1"; }
[ -f /opt/burst/.done ] && { systemctl start 'actions.runner.*' || true; exit 0; }
N=$(mda runner-count); TOKEN=$(mda reg-token); LABELS=$(mda runner-labels); ORG=$(mda org)
BASE=$(mda bundle-base || echo https://github.com/Neonbee-ai/neonbee-devops/releases/download/burst-bundle)
echo "$(date -u +%FT%TZ) start N=$N labels=$LABELS"
mkdir -p /opt/burst && cd /opt/burst || exit 1

export DEBIAN_FRONTEND=noninteractive
systemctl disable --now unattended-upgrades apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true
# Base tools + Chromium runtime libs for the bundled Playwright browsers (one apt pass).
apt-get update -q && apt-get install -yq git rsync jq curl unzip zip zstd xz-utils build-essential openssh-client python3 python3-pip \
  libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 libdrm2 libxkbcommon0 libatspi2.0-0t64 libxcomposite1 \
  libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2t64 fonts-liberation

# --- bundle: manifest lists "sha256  part" lines; parts concatenate to burst-bundle.tar.gz -----------------
B=
if curl -fsSL -o manifest "$BASE/burst-bundle.manifest"; then
  ok=1
  while read -r sum part; do
    [ -n "$part" ] || continue
    curl -fsSL --retry 3 -o "$part" "$BASE/$part" && echo "$sum  $part" | sha256sum -c --quiet || { ok=0; break; }
  done < manifest
  if [ "$ok" = 1 ]; then
    unz='gzip -dc'; case "$(awk 'NR==1 {print $2}' manifest)" in *.zst.*) unz='zstd -dc' ;; esac
    awk '{print $2}' manifest | xargs cat | $unz | tar x -C /opt/burst && B=/opt/burst/bundle
    awk '{print $2}' manifest | xargs rm -f
  fi
fi
echo "bundle: ${B:-unavailable - upstream fallback}"

if [ -n "$B" ] && [ -f "$B/runner.tgz" ]; then cp "$B/runner.tgz" /opt/burst/runner.tgz
else V=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | jq -r .tag_name | sed 's/^v//')
  curl -fsSL -o /opt/burst/runner.tgz "https://github.com/actions/runner/releases/download/v$V/actions-runner-linux-x64-$V.tar.gz"; fi
if [ -n "$B" ] && [ -f "$B/node.tar.xz" ]; then NODE_TXZ=$B/node.tar.xz
else NV=$(curl -fsSL https://nodejs.org/dist/index.json | jq -r '[.[] | select(.version | startswith("v22."))][0].version')
  curl -fsSL -o /opt/burst/node.tar.xz "https://nodejs.org/dist/$NV/node-$NV-linux-x64.tar.xz"; NODE_TXZ=/opt/burst/node.tar.xz; fi
tar xJf "$NODE_TXZ" -C /usr/local --strip-components 1 --exclude='*/CHANGELOG.md' --exclude='*/README.md' --exclude='*/LICENSE'
NV=$(/usr/local/bin/node -v | tr -d v)
mkdir -p "/opt/burst/node/$NV/x64" && tar xJf "$NODE_TXZ" -C "/opt/burst/node/$NV/x64" --strip-components 1
if [ -n "$B" ] && [ -f "$B/gitleaks.tar.gz" ]; then tar xzf "$B/gitleaks.tar.gz" -C /usr/local/bin gitleaks
else GL=8.30.1; curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v$GL/gitleaks_${GL}_linux_x64.tar.gz" | tar xz -C /usr/local/bin gitleaks; fi
if [ -n "$B" ] && [ -d "$B/wheels" ]; then pip3 install -q --break-system-packages --no-index --find-links "$B/wheels" semgrep ||
  pip3 install -q --break-system-packages semgrep
else pip3 install -q --break-system-packages semgrep; fi
echo "tools: node=$NV $(gitleaks version 2>/dev/null | head -1) semgrep=$(semgrep --version 2>/dev/null)"

id runner >/dev/null 2>&1 || useradd -m -s /bin/bash runner
echo 'runner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/runner
mda nb-jump | bash
# Warm caches from the bundle: npm (shared ~/.npm, cacache is concurrency-safe) + Playwright browsers.
mkdir -p /home/runner/.cache
[ -n "$B" ] && [ -d "$B/npm" ] && rm -rf /home/runner/.npm && mv "$B/npm" /home/runner/.npm
[ -n "$B" ] && [ -d "$B/ms-playwright" ] && mv "$B/ms-playwright" /home/runner/.cache/ms-playwright
chown -R runner: /home/runner/.npm /home/runner/.cache 2>/dev/null || true
# Private /tmp per runner: jobs write fixed paths like /tmp/gitleaks ("Text file busy").
mkdir -p "/etc/systemd/system/actions.runner.$ORG-.service.d"
printf '[Service]\nPrivateTmp=yes\n' > "/etc/systemd/system/actions.runner.$ORG-.service.d/tmp.conf"
systemctl daemon-reload

for i in $(seq -w 1 "$N"); do
  d=/home/runner/r$i; mkdir -p "$d/_work/_tool/node" && tar xzf /opt/burst/runner.tgz -C "$d"
  # Seed the tool cache so actions/setup-node finds Node 22 without a download (hard links: no extra disk).
  cp -al "/opt/burst/node/$NV" "$d/_work/_tool/node/$NV" && touch "$d/_work/_tool/node/$NV/x64.complete"
  chown -R runner: "$d"
  [ "$i" = "$(printf %0${#N}d 1)" ] && "$d/bin/installdependencies.sh" >/dev/null
  sudo -u runner "$d/config.sh" --unattended --replace --url "https://github.com/$ORG" --token "$TOKEN" \
    --name "gcp-burst-$i" --labels "$LABELS" --work _work >/dev/null || { echo "r$i register failed"; continue; }
  (cd "$d" && ./svc.sh install runner >/dev/null && ./svc.sh start >/dev/null)
done
chown runner: /home/runner  # runner dir mkdir above leaves home root-owned -> ~/.ssh, ~/.cache EACCES
echo "$(date -u +%FT%TZ) runners: $(systemctl list-units 'actions.runner.*' --no-legend | grep -c running)/$N"

# Self-delete: stop runners, then DELETE this instance via the metadata token of SA nb-burst-self (its
# custom role allows instances.delete on neonbee-ci-burst only). Boot disk is autoDelete -> $0 left behind.
# Offline gcp-burst-* registrations are removed by burst-on-label.yml (reap) once the VM is gone.
cat > /opt/burst/self-delete.sh <<'EOD'
#!/bin/bash
md() { curl -sf -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/$1"; }
logger -t nb-burst "self-delete: $1"
systemctl stop 'actions.runner.*' 2>/dev/null
P=$(md project/project-id); Z=$(md instance/zone); Z=${Z##*/}; I=$(md instance/name)
T=$(md instance/service-accounts/default/token | jq -r .access_token)
curl -fsS -m 30 -X DELETE -H "Authorization: Bearer $T" \
  "https://compute.googleapis.com/compute/v1/projects/$P/zones/$Z/instances/$I" >/dev/null || poweroff
EOD
# Idle watchdog: 3 consecutive idle 1-min checks (no GitHub job, no AutoQA kit job) -> self-delete.
cat > /opt/burst/idle.sh <<'EOI'
#!/bin/bash
f=/opt/burst/idle; pgrep -f 'Runner.Worker|comb-beeline/e2e/runner.mjs' >/dev/null && { echo 0 > $f; exit 0; }
n=$(( $(cat $f 2>/dev/null || echo 0) + 1 )); echo $n > $f
[ $n -ge 3 ] && [ ! -e /opt/burst/keep ] && /opt/burst/self-delete.sh "idle ${n} min"
exit 0
EOI
# Disk guard: past 75%, drop repo checkouts of idle runners, then logs, then (past 90%) caches.
cat > /opt/burst/nb-diskclean.sh <<'EOC'
#!/bin/bash
T=${1:-75}; use() { df --output=pcent / | tail -1 | tr -dc 0-9; }
[ "$(use)" -lt "$T" ] && exit 0
b=$(use)
for d in /home/runner/r*; do
  pgrep -f "$d/bin/Runner.Worker" >/dev/null && continue
  find "$d/_work" -mindepth 1 -maxdepth 1 ! -name '_tool' ! -name '_actions' -mmin +15 -exec rm -rf {} + 2>/dev/null
done
journalctl --vacuum-size=200M >/dev/null 2>&1
[ "$(use)" -ge 90 ] && find /home/runner/.cache /home/runner/.npm -type f -atime +0 -delete 2>/dev/null
logger -t nb-diskclean "disk ${b}% -> $(use)%"
EOC
chmod 755 /opt/burst/self-delete.sh /opt/burst/idle.sh /opt/burst/nb-diskclean.sh; echo 0 > /opt/burst/idle
echo '* * * * * root /opt/burst/idle.sh' > /etc/cron.d/burst-idle
echo '*/5 * * * * root /opt/burst/nb-diskclean.sh 75' > /etc/cron.d/nb-diskclean
rm -rf /opt/burst/bundle /opt/burst/node.tar.xz
touch /opt/burst/.done
echo "$(date -u +%FT%TZ) ready"
