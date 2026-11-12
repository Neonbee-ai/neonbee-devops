#!/bin/bash
# Plain-bash asserts for scripts/burst/size.sh (burst_size, burst_cost). Usage: bash tests/burst_size_test.sh
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck source=scripts/burst/size.sh
. "$ROOT/scripts/burst/size.sh"
pass=0; fail=0
eq() { if [ "$2" = "$3" ]; then pass=$((pass + 1)); else fail=$((fail + 1)); echo "FAIL [$1] got '$2' want '$3'"; fi; }
sz() { burst_size "$@" 2>/dev/null; }

# auto: runners = clamp(jobs, 1, 20), smallest t2d with >= 1 vCPU per runner, disk 30 + 3/runner
eq auto-0      "$(sz auto 0)"   "1 t2d-standard-2 33"
eq auto-1      "$(sz auto 1)"   "1 t2d-standard-2 33"
eq auto-2      "$(sz auto 2)"   "2 t2d-standard-2 36"
eq auto-3      "$(sz auto 3)"   "3 t2d-standard-4 39"
eq auto-5      "$(sz auto 5)"   "5 t2d-standard-8 45"
eq auto-9      "$(sz auto 9)"   "9 t2d-standard-16 57"
eq auto-16     "$(sz auto 16)"  "16 t2d-standard-16 78"
eq auto-17     "$(sz auto 17)"  "17 t2d-standard-32 81"
eq auto-50     "$(sz auto 50)"  "20 t2d-standard-32 90"
eq auto-junk   "$(sz auto abc)" "1 t2d-standard-2 33"
eq auto-empty  "$(sz '' 4)"     "4 t2d-standard-4 42"
# label presets
eq s  "$(sz s 40)"  "2 t2d-standard-2 36"
eq m  "$(sz m 0)"   "8 t2d-standard-8 54"
eq l  "$(sz l 0)"   "16 t2d-standard-16 78"
eq xl "$(sz xl 0)"  "20 t2d-standard-32 90"
# manual overrides (workflow_dispatch machine_type / runners)
eq machine-only     "$(sz auto 3 t2d-standard-16)"     "3 t2d-standard-16 39"
eq runners-only     "$(sz auto 0 '' 6)"                "6 t2d-standard-8 48"
eq both             "$(sz auto 0 t2d-standard-4 4)"    "4 t2d-standard-4 42"
eq ram-cap          "$(sz auto 0 t2d-standard-2 10)"   "4 t2d-standard-2 42"
eq runners-cap      "$(sz l 0 '' 99)"                  "20 t2d-standard-16 90"
eq preset-runners   "$(sz m 0 '' 3)"                   "3 t2d-standard-8 39"
# refusals
sz huge 1 >/dev/null; eq bad-size "$?" 1
sz auto 1 n2-standard-8 >/dev/null; eq bad-family "$?" 1
sz auto 1 t2d-standard-60 >/dev/null; eq over-cap "$?" 1
sz auto 1 t2d-standard-6 >/dev/null; eq no-such-shape "$?" 1
eq err-msg "$(burst_size huge 1 2>&1 >/dev/null)" "burst_size: unknown size 'huge' (auto|s|m|l|xl)"

# cost: vCPU*h*core + vCPU*4GB*h*ram + disk GB-month prorated
eq cost-catalog   "$(burst_cost 16 60 78 0.01 0.001)" "0.235"
eq cost-zero      "$(burst_cost 8 0 54)"              "0.000"
eq cost-fallback  "$(burst_cost 16 30 78)"            "$(awk 'BEGIN { printf "%.3f\n", 16*0.5*0.0090 + 64*0.5*0.0012 + 78*0.10/730*0.5 }')"
eq cost-xl-3h     "$(burst_cost 32 180 90)"           "$(awk 'BEGIN { printf "%.3f\n", 32*3*0.0090 + 128*3*0.0012 + 90*0.10/730*3 }')"

echo "burst_size_test: $pass passed, $fail failed"; [ "$fail" = 0 ]
