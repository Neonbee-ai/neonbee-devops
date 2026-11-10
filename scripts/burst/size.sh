#!/bin/bash
# Burst VM sizing: pure bash, no I/O. Source it and call burst_size.
#
#   burst_size SIZE JOBS [MACHINE] [RUNNERS]  ->  prints "RUNNERS MACHINE DISK_GB"
#
# SIZE: auto | s | m | l | xl (from the PR label burst / burst:s|m|l|xl).
#   auto  runners = clamp(JOBS, 1, 20); machine = smallest t2d-standard-{2,4,8,16,32} with
#         >= 1 vCPU per runner (t2d = 4 GB RAM per vCPU, so >= 2 GB per runner holds too)
#   s     t2d-standard-2,   2 runners
#   m     t2d-standard-8,   8 runners
#   l     t2d-standard-16, 16 runners
#   xl    t2d-standard-32, 20 runners
# MACHINE / RUNNERS: optional manual overrides (workflow_dispatch inputs). A machine above 32 vCPU or
#   not t2d-standard-N is refused; runners are clamped to 1..20 and to the machine's RAM (2 GB each).
# Disk: 30 GB + 3 GB per runner. Returns 1 on bad input (message on stderr).
BURST_MAX_RUNNERS=20
BURST_MAX_VCPU=32
BURST_MACHINES="2 4 8 16 32"

burst_size() {
  local size=${1:-auto} jobs=${2:-0} machine=${3:-} runners=${4:-} vcpu="" v
  case "$jobs" in ''|*[!0-9]*) jobs=0 ;; esac
  case "$runners" in ''|*[!0-9]*) runners= ;; esac
  case "$size" in
    s)  vcpu=2;  [ -n "$runners" ] || runners=2 ;;
    m)  vcpu=8;  [ -n "$runners" ] || runners=8 ;;
    l)  vcpu=16; [ -n "$runners" ] || runners=16 ;;
    xl) vcpu=32; [ -n "$runners" ] || runners=20 ;;
    auto|'') [ -n "$runners" ] || runners=$jobs ;;
    *) echo "burst_size: unknown size '$size' (auto|s|m|l|xl)" >&2; return 1 ;;
  esac
  [ "$runners" -lt 1 ] && runners=1
  [ "$runners" -gt "$BURST_MAX_RUNNERS" ] && runners=$BURST_MAX_RUNNERS
  if [ -n "$machine" ]; then
    case "$machine" in t2d-standard-[0-9]*) vcpu=${machine#t2d-standard-} ;;
      *) echo "burst_size: machine '$machine' is not t2d-standard-N" >&2; return 1 ;; esac
    case " $BURST_MACHINES " in *" $vcpu "*) ;; *) echo "burst_size: no t2d-standard-$vcpu (2/4/8/16/32)" >&2; return 1 ;; esac
    # RAM cap: 4 GB per vCPU, 2 GB per runner
    [ "$runners" -gt $((vcpu * 2)) ] && runners=$((vcpu * 2))
  elif [ -z "$vcpu" ]; then
    vcpu=$BURST_MAX_VCPU
    for v in $BURST_MACHINES; do [ "$v" -ge "$runners" ] && { vcpu=$v; break; }; done
  fi
  [ "$vcpu" -gt "$BURST_MAX_VCPU" ] && { echo "burst_size: $vcpu vCPU over the $BURST_MAX_VCPU cap" >&2; return 1; }
  echo "$runners t2d-standard-$vcpu $((30 + 3 * runners))"
}

# Estimated cost of one burst VM life (pure, awk only):
#   burst_cost VCPU MINUTES DISK_GB [CORE_HR RAM_GB_HR]  ->  prints "USD" with 3 decimals
# T2D = 4 GB RAM per vCPU. Rates: Spot $/vCPU-hour and $/GB-hour, from the Cloud Billing Catalog when the
# caller found them, else the FALLBACK constants below (ESTIMATE ONLY - europe-west1 T2D Spot, may be off
# +-50%; refresh from https://cloud.google.com/spot-vms/pricing). pd-balanced at $0.10 per GB-month.
BURST_FALLBACK_CORE_HR=0.0090
BURST_FALLBACK_RAM_GB_HR=0.0012
BURST_DISK_GB_MONTH=0.10
burst_cost() {
  local vcpu=$1 min=$2 disk=$3 core=${4:-$BURST_FALLBACK_CORE_HR} ram=${5:-$BURST_FALLBACK_RAM_GB_HR}
  awk -v c="$vcpu" -v m="$min" -v d="$disk" -v cr="$core" -v rr="$ram" -v dr="$BURST_DISK_GB_MONTH" \
    'BEGIN { h = m / 60; printf "%.3f\n", c * h * cr + c * 4 * h * rr + d * dr / 730 * h }'
}
