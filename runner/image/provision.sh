#!/bin/bash
# Image family nb-ci-runner: provisioning, run ONCE on a stock ubuntu-2404 builder VM by
# `nbci.py build-image` (weekly). The controller turns the powered-off builder disk into the image.
# Contents: Node 22, the latest Actions runner, Playwright Chromium + deps, gitleaks, semgrep, a warm
# npm cache, and the one-job boot unit. No credentials of any kind are baked in.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -yq git rsync jq curl unzip zip build-essential openssh-client python3 python3-pip python3-venv iptables ca-certificates
curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && apt-get install -yq nodejs
id runner >/dev/null 2>&1 || useradd -m -s /bin/bash runner
echo 'runner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/runner

V=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | jq -r .tag_name | sed 's/^v//')
mkdir -p /opt/runner && curl -fsSL "https://github.com/actions/runner/releases/download/v$V/actions-runner-linux-x64-$V.tar.gz" | tar xz -C /opt/runner
/opt/runner/bin/installdependencies.sh
chown -R runner: /opt/runner

GL=$(curl -fsSL https://api.github.com/repos/gitleaks/gitleaks/releases/latest | jq -r .tag_name | sed 's/^v//')
curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v${GL}/gitleaks_${GL}_linux_x64.tar.gz" | tar xz -C /usr/local/bin gitleaks
pip3 install -q --break-system-packages semgrep

# Playwright Chromium (system deps as root, browsers into the runner's cache) + warm npm cache
npx -y playwright@1 install-deps chromium
sudo -u runner -H npx -y playwright@1 install chromium
[ -n "${NB_STARTUP:-}" ] || sudo -u runner -H npm cache add typescript@5 @nestjs/cli@11 vite@5 vite@7 jest@29 eslint@9 @swc/core@1 esbuild@0 \
  vitest@2 ts-node@10 prettier@3 @vitejs/plugin-react@4 @originjs/vite-plugin-federation@1 tailwindcss@3 || true

# One job per VM: read the JIT config from metadata (root only), run it as `runner`, power off.
cat > /opt/nb-run.sh <<'RUN'
#!/bin/bash
J=$(curl -sf -m 5 -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/attributes/jit) || { poweroff; exit; }
# job code must not read metadata (no SA is attached anyway; this keeps the JIT config private)
iptables -C OUTPUT -m owner --uid-owner runner -d 169.254.169.254 -j REJECT 2>/dev/null ||
  iptables -A OUTPUT -m owner --uid-owner runner -d 169.254.169.254 -j REJECT
# no job within 5 min of boot -> power off (the controller deletes the stopped VM and its disk)
( sleep 300; pgrep -f Runner.Worker >/dev/null || poweroff ) &
sudo -u runner -H env ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/nb-disk-guard.sh /opt/runner/run.sh --jitconfig "$J" > /var/log/nb-runner.log 2>&1
poweroff
RUN
cat > /opt/nb-disk-guard.sh <<'G'
#!/bin/bash
free=$(df -Pk / | awk 'NR==2{print int($4/1048576)}')
[ "$free" -ge 5 ] || { echo "::error::runner disk has ${free} GB free"; exit 1; }
G
chmod 755 /opt/nb-run.sh /opt/nb-disk-guard.sh
cat > /etc/systemd/system/nb-runner.service <<'U'
[Unit]
Description=nb-ci one-job runner
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
ExecStart=/opt/nb-run.sh
PrivateTmp=yes
[Install]
WantedBy=multi-user.target
U
systemctl enable nb-runner.service
# small image: drop caches and logs (image build only; a startup-script VM is deleted after one job)
if [ -z "${NB_STARTUP:-}" ]; then
  apt-get clean; rm -rf /var/lib/apt/lists/* /tmp/* /root/.npm/_logs
  journalctl --vacuum-time=1s || true
fi
sync
