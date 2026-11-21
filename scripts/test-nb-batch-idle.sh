#!/usr/bin/env bash
# BDD: nb-batch hold/release change the VM idle limit through instance metadata (stubbed gcloud).
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); NB="$HERE/nb-batch.sh"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
cat > "$T/gcloud" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$GCLOG"
STUB
chmod +x "$T/gcloud"; export GCLOG="$T/log" GCLOUD="$T/gcloud" HOME="$T"
fail=0
check() { if grep -q -- "$2" "$GCLOG"; then echo "ok   $1"; else echo "FAIL $1 (wanted: $2)"; fail=1; fi; }

: > "$GCLOG"; "$NB" hold abc >/dev/null 2>&1
check "given a running batch when hold then idle limit is 40 min" "add-metadata nb-batch-abc .*idle-min=40"
: > "$GCLOG"; "$NB" hold abc 25 >/dev/null 2>&1
check "given hold with minutes then that limit is used" "idle-min=25"
: > "$GCLOG"; "$NB" release abc >/dev/null 2>&1
check "given a held batch when release then idle limit returns to 3 min" "idle-min=3"
exit $fail
