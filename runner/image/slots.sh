# Packed job VM (appended to the startup-script after provision.sh; NB_SLOTS is set by the controller).
# NB_SLOTS one-job runner slots share the VM. The controller refills a slot after each job with a fresh JIT
# config in instance metadata jit-<i> (slot-<i> = its runner name), so the VM never holds a registration
# token and keeps taking jobs while the queue is non-empty; the controller deletes it after 3 min idle.
# Co-tenancy: each slot is its own user nbs<i> (own HOME, npm cache, work dir _work under /opt/slots/<i>,
# private /tmp), has no sudo and cannot reach the metadata server; its processes are killed and its work
# dir, temp files and HOME (npm content cache and Playwright browsers kept) are wiped after every job.
N=${NB_SLOTS:-1}
# a slot user sees only its own processes (no neighbour's argv/environ in /proc)
mount -o remount,hidepid=invisible /proc 2>/dev/null || mount -o remount,hidepid=2 /proc || echo 'nb-ci: no hidepid'
# 4 slots share 16 GB: a build peak spills to swap instead of the OOM killer taking a neighbour's job
if ! swapon --show | grep -q /swapfile; then
  { fallocate -l 8G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile; } || echo 'nb-ci: no swap'
fi
cat > /opt/nb-slot.sh <<'SLOT'
#!/bin/bash
# One slot: wait for a JIT config this slot has not run yet, run that one job as nbs<i>, clean up, repeat.
i=$1; U=nbs$i; D=/opt/slots/$i
MD=http://metadata.google.internal/computeMetadata/v1/instance
md() { curl -sf -m 5 -H 'Metadata-Flavor: Google' "$MD/$1"; }
if ! id "$U" >/dev/null 2>&1; then
  useradd -m -s /bin/bash "$U"
fi
iptables -C OUTPUT -m owner --uid-owner "$U" -d 169.254.169.254 -j REJECT 2>/dev/null ||
  iptables -A OUTPUT -m owner --uid-owner "$U" -d 169.254.169.254 -j REJECT
last=
while :; do
  J=$(md "attributes/jit-$i") || J=
  h=$(printf %s "$J" | sha256sum | cut -c1-16)
  if [ -z "$J" ] || [ "$h" = "$last" ]; then sleep 3; continue; fi
  last=$h
  # cache bucket token (only VMs with a service account); refreshed per job, a reused VM outlives one token
  T=$(md service-accounts/default/token | jq -r .access_token 2>/dev/null) && [ -n "$T" ] && [ "$T" != null ] &&
    { umask 022; printf %s "$T" > /run/nb-gcs-token; }
  # Playwright installs in the background after boot: give this slot the browsers once they exist (kept across jobs)
  if [ -f /opt/nb-ready.playwright ] && [ ! -d "/home/$U/.cache/ms-playwright" ] && [ -d /home/runner/.cache/ms-playwright ]; then
    mkdir -p "/home/$U/.cache" && cp -a /home/runner/.cache/ms-playwright "/home/$U/.cache/" && chown -R "$U:" "/home/$U/.cache"
  fi
  mkdir -p "$D" && rsync -a --delete /opt/runner/ "$D/" && chown -R "$U:" "$D"
  echo "== slot $i job start $(date -u +%FT%TZ)" >> "/var/log/nb-slot-$i.log"
  # npm "global" config/prefix per slot: the shared Node in /opt/hostedtoolcache belongs to `runner`, so
  # `npm config set ... --global` failed with EACCES on every slot (2026-11-27) and, made writable, would have
  # shared one job's registry token with its neighbours. Both live in the slot HOME, wiped after the job.
  # runuser, not sudo: sudo logs the whole command line (the JIT config) to the journal.
  H=/home/$U
  runuser -u "$U" -- env HOME="$H" USER="$U" LOGNAME="$U" NPM_CONFIG_GLOBALCONFIG="$H/.npmrc-global" \
    NPM_CONFIG_PREFIX="$H/.npm-global" PATH="$H/.npm-global/bin:$PATH" \
    ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/nb-disk-guard.sh "$D/run.sh" --jitconfig "$J" >> "/var/log/nb-slot-$i.log" 2>&1
  pkill -9 -u "$U" 2>/dev/null; sleep 1
  rm -rf "$D/_work"
  find /tmp /var/tmp /dev/shm -xdev -mindepth 1 -user "$U" -delete 2>/dev/null
  find "/home/$U" -mindepth 1 -maxdepth 1 ! -name .npm ! -name .cache -exec rm -rf {} + 2>/dev/null
  find "/home/$U/.npm" -mindepth 1 -maxdepth 1 ! -name _cacache -exec rm -rf {} + 2>/dev/null
  find "/home/$U/.cache" -mindepth 1 -maxdepth 1 ! -name ms-playwright -exec rm -rf {} + 2>/dev/null
  cp -a /etc/skel/. "/home/$U/" && chown -R "$U:" "/home/$U"
done
SLOT
chmod 700 /opt/nb-slot.sh
cat > /etc/systemd/system/nb-slot@.service <<'U'
[Unit]
Description=nb-ci runner slot %i
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
ExecStart=/opt/nb-slot.sh %i
Restart=always
RestartSec=5
PrivateTmp=yes
KillMode=control-group
[Install]
WantedBy=multi-user.target
U
# memory telemetry for the slot count decision: one line a minute on the serial console
# (gcloud compute instances get-serial-port-output <vm> | grep nbci-mem): total/used/available MB, swap used, load, busy slots
cat > /opt/nb-mem.sh <<'MEM'
#!/bin/bash
while :; do
  echo "nbci-mem $(date -u +%H:%M:%S) $(free -m | awk '/^Mem:/{printf "mem_total=%d used=%d avail=%d", $2, $3, $7} /^Swap:/{printf " swap_used=%d", $3}') load=$(cut -d' ' -f1 /proc/loadavg) busy=$(pgrep -c -f Runner.Worker)" > /dev/ttyS0
  sleep 60
done
MEM
chmod 755 /opt/nb-mem.sh
cat > /etc/systemd/system/nb-mem.service <<'U'
[Unit]
Description=nb-ci memory telemetry to the serial console
[Service]
ExecStart=/opt/nb-mem.sh
Restart=always
U
systemctl daemon-reload
systemctl start --no-block nb-mem.service
for i in $(seq 1 "$N"); do systemctl start --no-block "nb-slot@$i.service"; done
