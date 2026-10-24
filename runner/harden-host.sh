#!/usr/bin/env bash
# Keeps the runner + preview host (207.180.217.124) alive under memory pressure.
#
# 2026-10-22 ~08:46 UTC: 10 runner slots + every preview's pm2 backends + a Shell
# build exhausted RAM. The VM stopped answering ping/SSH, all contabo-* runners
# went offline and every pr-*.skyoffice360.com preview returned 502/522.
#
# What this does (idempotent, safe to re-run):
#   1. swap file (min(RAM/2, 8 GB)) when the host has none, swappiness 10
#   2. all runner slots share one cgroup (runners.slice) capped below total RAM, at
#      lower CPU weight, so a runaway build is OOM-killed inside the slice and
#      sshd / nginx / preview pm2 always keep headroom
#   3. earlyoom kills the biggest build process before the kernel stalls
#   4. a hard lockup reboots the box after 10 s instead of leaving it dark
#   5. nginx + pm2 come back on boot with the saved preview process list
#
# Usage (as root, on the runner VM only — never the Dev VM):
#   bash runner/harden-host.sh --dry-run   # print what would change
#   bash runner/harden-host.sh             # apply; restarts idle runner services
set -euo pipefail

DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1
run() { if [ "$DRY" = 1 ]; then echo "DRY: $*"; else bash -c "$*"; fi; }

[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
ls -d /opt/actions-runner* >/dev/null 2>&1 || { echo "no /opt/actions-runner* here — wrong host?"; exit 1; }

mem_mb=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
cpus=$(nproc)
echo "host: ${mem_mb} MB RAM, ${cpus} CPUs"

# Reserve for the OS, sshd, nginx and preview pm2 apps (300 MB cap each).
# grep exits 1 on zero matches; under pipefail that must not fail the pipeline.
pv_count=$( { pm2 jlist 2>/dev/null || true; } | { grep -o '"name":"pv-[^"]*"' || true; } | wc -l | tr -d ' ')
reserve_mb=$(( 2048 + pv_count * 300 ))
[ "$reserve_mb" -gt $(( mem_mb / 2 )) ] && reserve_mb=$(( mem_mb / 2 ))
runners_max_mb=$(( mem_mb - reserve_mb ))
runners_high_mb=$(( runners_max_mb * 90 / 100 ))
echo "preview backends: ${pv_count}; reserve ${reserve_mb} MB; runners.slice MemoryHigh=${runners_high_mb}M MemoryMax=${runners_max_mb}M"

# 1. Swap
if ! swapon --show=NAME --noheadings | grep -q .; then
  sz=$(( mem_mb / 2 < 8192 ? mem_mb / 2 : 8192 ))
  avail_mb=$(df -m --output=avail / | tail -1 | tr -d ' ')
  if [ "$avail_mb" -gt $(( sz + 10240 )) ]; then
    run "fallocate -l ${sz}M /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile"
    grep -q '^/swapfile ' /etc/fstab || run "echo '/swapfile none swap sw 0 0' >> /etc/fstab"
  else
    echo "WARN: only ${avail_mb} MB free on / — skipped the ${sz} MB swap file"
  fi
else
  echo "swap already on: $(swapon --show=NAME,SIZE --noheadings | tr '\n' ' ')"
fi

# 2 + 4. Kernel settings
run "cat > /etc/sysctl.d/90-neonbee-runner.conf <<'EOF'
vm.swappiness = 10
vm.min_free_kbytes = 131072
kernel.panic = 10
kernel.panic_on_oops = 1
kernel.softlockup_panic = 1
EOF
sysctl -q --system"

# 2. Runner slice
run "cat > /etc/systemd/system/runners.slice <<EOF
[Unit]
Description=GitHub Actions runner slots (memory-capped)

[Slice]
MemoryAccounting=yes
MemoryHigh=${runners_high_mb}M
MemoryMax=${runners_max_mb}M
CPUWeight=50
EOF"

units=$(systemctl list-units --all --type=service --no-legend 'actions.runner.*' | awk '{print $1}' | sed 's/^●//')
[ -n "$units" ] || echo "WARN: no actions.runner.* services found"
for u in $units; do
  run "mkdir -p /etc/systemd/system/${u}.d && printf '[Service]\nSlice=runners.slice\n' > /etc/systemd/system/${u}.d/10-slice.conf"
done
run "systemctl daemon-reload"

# 3. earlyoom
if ! command -v earlyoom >/dev/null; then
  run "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq earlyoom >/dev/null"
fi
run "cat > /etc/default/earlyoom <<'EOF'
EARLYOOM_ARGS=\"-m 5 -s 10 -r 3600 --prefer '(^|/)(node|npm|tsc|vitest|jest|esbuild|next-server)$' --avoid '(^|/)(sshd|nginx|systemd|Runner.Listener|PM2.*)$'\"
EOF
systemctl enable --now earlyoom >/dev/null && systemctl restart earlyoom"

# 5. Boot persistence for previews
run "systemctl enable nginx >/dev/null"
if systemctl list-unit-files 'pm2-*.service' --no-legend | grep -q .; then
  run "systemctl enable \$(systemctl list-unit-files 'pm2-*.service' --no-legend | awk '{print \$1}') >/dev/null"
else
  run "pm2 startup systemd -u root --hp /root >/dev/null"
fi
# Never save an empty process list over an existing dump (it would drop every app on reboot).
if [ "$(pm2 jlist 2>/dev/null | grep -c '"name"' || true)" -gt 0 ]; then
  run "pm2 save --force >/dev/null"
else
  echo "pm2 has no running apps; leaving ~/.pm2/dump.pm2 untouched"
fi

# Move runner services into the slice. A busy slot is left alone (its job would die)
# and picks the slice up on its next restart.
for u in $units; do
  if systemctl status "$u" --no-pager -n0 2>/dev/null | grep -q Runner.Worker; then
    echo "busy, not restarted: $u"
  else
    run "systemctl restart '$u'"
  fi
done

echo "done. check: systemctl status runners.slice; swapon --show; systemctl is-active earlyoom"
