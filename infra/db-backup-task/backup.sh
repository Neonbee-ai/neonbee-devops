#!/usr/bin/env bash
# Nightly logical backup of prod -> s3://$BUCKET/daily/Y/M/D/ (+ monthly/ on the 1st).
# Same dump flags and checks as .github/workflows/nightly-db-backup.yml.
set -euo pipefail
JOB=backup; . "$(dirname "$0")/common.sh"
trap 'echo "NB_JOB_FAILED job=backup line=$LINENO"' ERR
umask 077
pg_login
major=$(pg_dump --version | sed -E 's/[^0-9]*([0-9]+).*/\1/'); [ "$major" -ge 17 ] || fail "pg_dump $major < 17"

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
DAY=$(date -u +%F); Y=$(date -u +%Y); M=$(date -u +%m); D=$(date -u +%d)
BASE="prod-${DAY}"; DUMP="$WORK/$BASE.dump"; MAN="$WORK/$BASE.manifest.json"; SUM="$WORK/$BASE.sha256"

pg_dump --role=postgres -Fc -Z 6 -N pgbouncer -f "$DUMP"
SIZE=$(wc -c < "$DUMP" | tr -d ' '); echo "dump size: $SIZE bytes"
pg_restore --list "$DUMP" > "$WORK/toc.txt"
OBJS=$(grep -cv '^;' "$WORK/toc.txt" || true); echo "toc entries: $OBJS"
[ "$OBJS" -gt 0 ] || fail "empty dump TOC"
grep -qE 'TABLE DATA auth users ' "$WORK/toc.txt"     || fail "auth.users data missing"
grep -qE 'TABLE DATA public tenants ' "$WORK/toc.txt" || fail "public.tenants data missing"

COUNTS=$(q "SELECT json_object_agg(t, n) FROM (
  SELECT t, (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %s', t), false, true, '')))[1]::text::bigint AS n
  FROM unnest(ARRAY['public.tenants','public.organizations','auth.users','public.invoices','public.journal_entries',
                    'accounting.invoices','accounting.journal_entries']) AS t
  WHERE to_regclass(t) IS NOT NULL) s;")
SHA=$(sha256sum "$DUMP" | awk '{print $1}')
printf '%s  %s\n' "$SHA" "$BASE.dump" > "$SUM"
jq -n --arg ref "${SB_REF: -4}" --arg dump "$BASE.dump" --arg sha "$SHA" --arg pg "$(pg_dump --version)" \
      --argjson bytes "$SIZE" --argjson toc "$OBJS" --argjson counts "$COUNTS" \
      '{project_ref_suffix:$ref, created_utc:(now|todate), dump:$dump, bytes:$bytes, sha256:$sha, toc_entries:$toc,
        pg_dump:$pg, row_counts:$counts, runner:"ecs-fargate"}' > "$MAN"
echo "row counts: $COUNTS"

up() { aws s3 cp "$1" "s3://$BUCKET/$2" --only-show-errors --sse AES256 --checksum-algorithm SHA256; }
p="daily/$Y/$M/$D"
up "$DUMP" "$p/$BASE.dump"; up "$MAN" "$p/$BASE.manifest.json"; up "$SUM" "$p/$BASE.sha256"
if [ "$D" = "01" ]; then
  up "$DUMP" "monthly/$Y/$M/$BASE.dump"; up "$MAN" "monthly/$Y/$M/$BASE.manifest.json"; up "$SUM" "monthly/$Y/$M/$BASE.sha256"
fi
RSIZE=$(aws s3api head-object --bucket "$BUCKET" --key "$p/$BASE.dump" --query ContentLength --output text)
[ "$RSIZE" = "$SIZE" ] || fail "uploaded size $RSIZE != local $SIZE"
ok "s3://$BUCKET/$p/$BASE.dump bytes=$SIZE toc=$OBJS sha256=$SHA"
