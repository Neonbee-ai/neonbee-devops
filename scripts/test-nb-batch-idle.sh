#!/usr/bin/env bash
# BDD: nb-batch hold/release change the VM idle limit through instance metadata and verify it by reading it back
# (stubbed gcloud keeps the metadata in a file, so a write that does not stick is detectable).
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); NB="$HERE/nb-batch.sh"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
cat > "$T/gcloud" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$GCLOG"
case "$*" in
  *add-metadata*) [ -n "${GC_DROP_WRITE:-}" ] && exit 0
                  for a in "$@"; do last=$a; done; echo "${last#idle-min=}" > "$GCMETA" ;;
  *describe*)     cat "$GCMETA" 2>/dev/null ;;
esac
STUB
chmod +x "$T/gcloud"; export GCLOG="$T/log" GCMETA="$T/meta" GCLOUD="$T/gcloud" HOME="$T"
fail=0
check() { if grep -q -- "$2" "$GCLOG"; then echo "ok   $1"; else echo "FAIL $1 (wanted: $2)"; fail=1; fi; }
rc() { "$@" >/dev/null 2>&1; echo $?; }

: > "$GCLOG"; r=$(rc "$NB" hold abc)
check "given a running batch when hold then idle limit is 40 min" "add-metadata nb-batch-abc .*idle-min=40"
check "given hold when applied then it reads the metadata back" "describe nb-batch-abc"
[ "$r" = 0 ] && echo "ok   given a write that sticks when hold then exit 0" || { echo "FAIL hold exit $r"; fail=1; }
: > "$GCLOG"; "$NB" hold abc 25 >/dev/null 2>&1
check "given hold with minutes then that limit is used" "idle-min=25"
: > "$GCLOG"; "$NB" release abc >/dev/null 2>&1
check "given a held batch when release then idle limit returns to 3 min" "idle-min=3"
rm -f "$GCMETA"; r=$(GC_DROP_WRITE=1 rc "$NB" hold abc)
[ "$r" != 0 ] && echo "ok   given a write that does not stick when hold then exit non-zero" || { echo "FAIL silent hold"; fail=1; }
exit $fail
