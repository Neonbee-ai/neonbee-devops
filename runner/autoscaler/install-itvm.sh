#!/usr/bin/env bash
# Install / update nb-autoscaler on the Internal Tools VM. Run ON the ITVM as root, from a checkout of
# neonbee-devops at /opt/nb-autoscaler (git pull = update). Needs: python3, gcloud authenticated as a
# service account with roles/compute.instanceAdmin.v1 on gen-lang-client-0521938031 (only this SA,
# never the runner VMs, holds GCP rights). Starts in DRY_RUN=1: watch `journalctl -u nb-autoscaler -f`,
# then set DRY_RUN=0. Touches nothing else on the host (no pm2, no nginx).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
SRC=/opt/nb-autoscaler
[ -f "$SRC/runner/autoscaler/nb_autoscaler.py" ] || { echo "clone neonbee-devops to $SRC first" >&2; exit 1; }
id nbci >/dev/null 2>&1 || useradd -r -m -s /usr/sbin/nologin nbci
[ -f /etc/nb-autoscaler.env ] || install -m 600 -o nbci "$SRC/runner/autoscaler/nb-autoscaler.env.example" /etc/nb-autoscaler.env
[ -f /etc/nb-autoscaler.gh-token ] || { echo "put the PAT in /etc/nb-autoscaler.gh-token (600, nbci)" >&2; exit 1; }
install -m 644 "$SRC/runner/autoscaler/nb-autoscaler.service" /etc/systemd/system/nb-autoscaler.service
systemctl daemon-reload && systemctl enable --now nb-autoscaler && systemctl restart nb-autoscaler
echo "*/5 * * * * root $SRC/runner/autoscaler/nb-autoscaler-watchdog.sh" > /etc/cron.d/nb-autoscaler-watchdog
sudo -u nbci env $(grep -v '^#' /etc/nb-autoscaler.env | xargs) python3 "$SRC/runner/autoscaler/nb_autoscaler.py" status
