#!/bin/bash
# Behaviour test of the shared route step (`- id: r`) in every neonbee-* reusable workflow: the burst PR
# label routes to nb-pool (Contabo fallback built in) and dispatches burst-on-label.yml with the size.
# Plain bash: extracts each run block, runs it against a stub curl. Usage: bash tests/burst_route_test.sh
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd); T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok() { pass=$((pass + 1)); }
bad() { fail=$((fail + 1)); echo "FAIL [$1] $2"; }
mkdir -p "$T/bin"
cat > "$T/bin/curl" <<'STUB'
#!/bin/bash
echo "$*" >> "$STUB_LOG"
case "$*" in
  */pulls*)      [ "${STUB_API_FAIL:-}" = 1 ] && exit 22; printf '%s' "${STUB_PULLS:-[]}" ;;
  */dispatches*) [ "${STUB_DISPATCH_FAIL:-}" = 1 ] && exit 22; for a in "$@"; do last=$a; done; echo "$last" >> "$STUB_LOG.body" ;;
  *) exit 22 ;;
esac
STUB
chmod +x "$T/bin/curl"

# run block of `- id: r`, dedented
extract() { awk '
  /^ *- id: r$/ { inr = 1; next }
  inr && /^ *run: \|$/ { match($0, /^ */); ind = RLENGTH; body = 1; next }
  body { match($0, /^ */); if ($0 !~ /^ *$/ && RLENGTH <= ind) exit; print substr($0, ind + 3) }' "$1"; }

# route WF NAME EXPECT_LANE EXPECT_DISPATCH(size|none) [env...]
route() {
  local wf=$1 name=$2 lane=$3 disp=$4; shift 4
  : > "$T/log"; rm -f "$T/log.body" "$T/out"
  env -i PATH="$T/bin:/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin" HOME="$T" STUB_LOG="$T/log" GITHUB_OUTPUT="$T/out" \
    GITHUB_STEP_SUMMARY="$T/sum" GITHUB_API_URL=https://api.github.com GITHUB_REPOSITORY=Neonbee-ai/so360-crm-fe \
    GITHUB_RUN_ID=1 GH_TOKEN=gh EVENT=pull_request SHA=abc1234 ATTEMPT=1 MODE=off BRANCH=feat/x PR_LABELS='[]' "$@" \
    bash -e "$T/r.sh" >/dev/null 2>"$T/err" || { bad "$wf/$name" "exit $? $(head -c 300 "$T/err")"; return; }
  got=$(sed -n 's/^runs_on=//p' "$T/out" | tail -1)
  [ "$got" = "$lane" ] || { bad "$wf/$name" "lane $got != $lane"; return; }
  if [ "$disp" = none ]; then [ ! -s "$T/log.body" ] || { bad "$wf/$name" "unexpected dispatch $(cat "$T/log.body")"; return; }
  else
    sz=$(jq -r .inputs.size "$T/log.body" 2>/dev/null)
    [ "$sz" = "$disp" ] || { bad "$wf/$name" "dispatch size '$sz' != $disp"; return; }
    [ "$(jq -r '.ref + " " + .inputs.repo + " " + .inputs.sha' "$T/log.body")" = "main Neonbee-ai/so360-crm-fe abc1234" ] ||
      { bad "$wf/$name" "dispatch body $(cat "$T/log.body")"; return; }
  fi
  ok
}

POOL='["self-hosted","nb-pool"]'; VMLANE='["self-hosted","runner-vm"]'; BURST='["self-hosted","lane-burst"]'
first=
for f in "$ROOT"/.github/workflows/neonbee-deploy-{backend,nextjs,preview,prod,vite-mfe}.yml "$ROOT"/.github/workflows/neonbee-pr-gate.yml; do
  wf=$(basename "$f" .yml); extract "$f" > "$T/r.sh"
  [ -s "$T/r.sh" ] || { bad "$wf" "no route block"; continue; }
  if [ -z "$first" ]; then first=$(cat "$T/r.sh"); elif [ "$first" != "$(cat "$T/r.sh")" ]; then bad "$wf" "route block differs from the others"; else ok; fi
  grep -q 'NB_BURST_DISPATCH_TOKEN:.*required: false' "$f" && ok || bad "$wf" "secret NB_BURST_DISPATCH_TOKEN not declared optional"
  route "$wf" label            "$POOL"   auto PR_LABELS='["bug","burst"]' DISPATCH_TOKEN=t
  route "$wf" label-m          "$POOL"   m    PR_LABELS='["burst:m"]' DISPATCH_TOKEN=t
  route "$wf" label-xl         "$POOL"   xl   PR_LABELS='["burst:xl"]' DISPATCH_TOKEN=t
  route "$wf" bad-size-label   "$VMLANE" none PR_LABELS='["burst:huge","burstable"]' DISPATCH_TOKEN=t
  route "$wf" no-token         "$POOL"   none PR_LABELS='["burst"]'
  route "$wf" dispatch-fails   "$POOL"   none PR_LABELS='["burst:l"]' DISPATCH_TOKEN=t STUB_DISPATCH_FAIL=1
  route "$wf" no-label         "$VMLANE" none DISPATCH_TOKEN=t
  route "$wf" labels-null      "$VMLANE" none PR_LABELS=null DISPATCH_TOKEN=t
  route "$wf" push-merged-pr   "$POOL"   s    EVENT=push PR_LABELS=null DISPATCH_TOKEN=t STUB_PULLS='[{"number":7,"labels":[{"name":"burst:s"}]}]'
  route "$wf" push-unlabelled  "$VMLANE" none EVENT=push PR_LABELS=null DISPATCH_TOKEN=t STUB_PULLS='[{"number":7,"labels":[]}]'
  route "$wf" push-api-fails   "$VMLANE" none EVENT=push PR_LABELS=null DISPATCH_TOKEN=t STUB_API_FAIL=1
  route "$wf" batch-unchanged  "$BURST"  none MODE=batch
  route "$wf" label-beats-batch "$POOL"  auto MODE=batch PR_LABELS='["burst"]' DISPATCH_TOKEN=t
done
echo "burst_route_test: $pass passed, $fail failed"; [ "$fail" = 0 ]
