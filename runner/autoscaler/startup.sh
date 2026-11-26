#!/bin/bash
# nb-autoscaler burst VM boot (stock ubuntu-2404-lts-amd64, run as root by the GCE startup-script hook).
# Each slot k runs ONE ephemeral job per JIT config the controller writes to metadata jit-k, then waits
# for the next one. The controller (runner/autoscaler/nb_autoscaler.py on the ITVM) decides everything:
# it hands out JIT configs, and it deletes this VM (boot disk auto-deletes) after 3 idle minutes.
# No service account, no static IP, no custom image, no Docker.
set -u
MD=http://metadata.google.internal/computeMetadata/v1/instance/attributes
md() { curl -sf -m 5 -H 'Metadata-Flavor: Google' "$MD/$1"; }
[ -f /opt/nb/.done ] && { systemctl start 'nb-slot@*' 2>/dev/null; exit 0; }
SLOTS=$(md slots || echo 1)
mkdir -p /opt/nb && cd /opt/nb || exit 1

# Tool bundle first (its signed URL is short-lived): node 22, gitleaks, semgrep wheels, actions-runner.
BUNDLED=0
BU=$(md bundle-url || true)
if [ -n "$BU" ] && curl -fsSL -m 120 -o b.tgz "$BU" && mkdir -p b && tar xzf b.tgz -C b; then BUNDLED=1; fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -q && apt-get install -yq git rsync jq curl unzip zip build-essential openssh-client \
  python3 python3-pip python3-venv iptables >/dev/null
if [ "$BUNDLED" = 1 ]; then
  tar xJf b/node-v*-linux-x64.tar.xz -C /usr/local --strip-components=1 &&
  tar xzf b/gitleaks.tar.gz -C /usr/local/bin gitleaks &&
  pip3 install -q --break-system-packages --no-index --find-links b/wheels semgrep &&
  cp b/runner.tgz runner.tgz || BUNDLED=0
fi
if [ "$BUNDLED" != 1 ]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null && apt-get install -yq nodejs >/dev/null
  V=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | jq -r .tag_name | sed 's/^v//')
  curl -fsSL -o runner.tgz "https://github.com/actions/runner/releases/download/v$V/actions-runner-linux-x64-$V.tar.gz"
fi

id runner >/dev/null 2>&1 || useradd -m -s /bin/bash runner
echo 'runner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/runner
# Job code (user runner) must not read metadata: it holds the other slots' JIT configs.
iptables -C OUTPUT -m owner --uid-owner runner -d 169.254.169.254 -j REJECT 2>/dev/null ||
  iptables -A OUTPUT -m owner --uid-owner runner -d 169.254.169.254 -j REJECT

cat > /opt/nb/job-started.sh <<'HOOK'
#!/bin/bash
# Runner pre-job hook: keep >= 8 GB free so a job never fills the disk mid-build.
free_gb() { df -Pk / | awk 'NR==2{print int($4/1048576)}'; }
[ "$(free_gb)" -ge 8 ] && exit 0
find /home/runner -path '*/_work/_temp/*' -mmin +30 -delete 2>/dev/null
npm cache clean --force >/dev/null 2>&1
echo "disk guard: $(free_gb) GB free after cleanup"
[ "$(free_gb)" -ge 3 ] || { echo "::error::runner disk full ($(free_gb) GB free)"; exit 1; }
HOOK
chmod 755 /opt/nb/job-started.sh

cat > /opt/nb/slot.sh <<'SLOT'
#!/bin/bash
# nb-slot@k: wait for a new jit-k from the controller, run that one job, repeat.
k=$1; d=/home/runner/r$k; last=
md() { curl -sf -m 5 -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"; }
while true; do
  n=$(md "name-$k" || true)
  if [ -n "$n" ] && [ "$n" != "$last" ]; then
    j=$(md "jit-$k" || true); last=$n
    [ -n "$j" ] && sudo -u runner -H env ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/nb/job-started.sh \
      "$d/run.sh" --jitconfig "$j" >> "/var/log/nb-slot-$k.log" 2>&1
    rm -rf "$d/_work/_temp" 2>/dev/null
  fi
  sleep 5
done
SLOT
chmod 755 /opt/nb/slot.sh
cat > /etc/systemd/system/nb-slot@.service <<'UNIT'
[Unit]
Description=nb-autoscaler runner slot %i
After=network-online.target
[Service]
ExecStart=/opt/nb/slot.sh %i
Restart=always
PrivateTmp=yes
[Install]
WantedBy=multi-user.target
UNIT

for k in $(seq 0 $((SLOTS - 1))); do
  d=/home/runner/r$k; mkdir -p "$d" && tar xzf runner.tgz -C "$d" && chown -R runner: "$d"
  [ "$k" = 0 ] && "$d/bin/installdependencies.sh" >/dev/null 2>&1
done
systemctl daemon-reload
for k in $(seq 0 $((SLOTS - 1))); do systemctl enable --now "nb-slot@$k" >/dev/null 2>&1; done
touch /opt/nb/.done
# Fallback if the controller is down: power off after 20 min with no job (the controller reaps the
# TERMINATED VM; maxRunDuration 4h deletes a running one regardless).
cat > /etc/cron.d/nb-idle <<'CRON'
* * * * * root f=/run/nb-idle; if pgrep -f Runner.Worker >/dev/null; then rm -f $f; else [ -f $f ] || date +\%s > $f; [ $(( $(date +\%s) - $(cat $f) )) -ge 1200 ] && poweroff; fi
CRON
