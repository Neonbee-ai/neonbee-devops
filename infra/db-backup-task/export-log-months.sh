#!/usr/bin/env bash
# Export whole months of log tables to S3 before the nightly db_retention_run deletes them.
# Pulse acc79468. Docs: documents/database/log-retention.md
#
# For every table in db_retention_policy with require_export, every closed month that is
# fully past the shortest keep_days of that table and not yet in db_archive_exports:
#   \copy (SELECT * ... month) -> gzip -> sha256 -> s3://neonbee-prod-db-backups/archive/<table>/<yyyy-mm>.csv.gz
#   -> head-object check -> INSERT INTO db_archive_exports.
# The DB deletes a month only after its row exists here. Read-only on data tables.
#
# Runs as the ECS Fargate task nb-db-log-export (infra/db-backup-fargate.yml), daily 01:30 UTC,
# before pg_cron nb-log-retention at 02:30 UTC. Port of scripts/db-archive/export-log-months.sh.
# Usage (in the container): export-log-months.sh [--dry-run]
set -euo pipefail
JOB=log-export; . "$(dirname "$0")/common.sh"
trap 'echo "NB_JOB_FAILED job=log-export line=$LINENO"' ERR

BUCKET="${ARCHIVE_BUCKET:-neonbee-prod-db-backups}"
PREFIX="${ARCHIVE_PREFIX:-archive}"
DRY=0; [[ "${1:-}" == "--dry-run" ]] && DRY=1
AWS="${AWS_BIN:-$(command -v aws)}"
pg_login
export PGAPPNAME="db-archive-export" PGOPTIONS="-c statement_timeout=300s"

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

# table|ts_column|month
TODO=$(q "
  WITH p AS (
    SELECT table_name, min(ts_column) AS ts_column, min(keep_days) AS keep_days
      FROM public.db_retention_policy WHERE enabled AND require_export
     GROUP BY table_name)
  SELECT p.table_name || '|' || p.ts_column || '|' || to_char(m, 'YYYY-MM-DD')
    FROM p
    CROSS JOIN LATERAL generate_series(
      date_trunc('month', now() - interval '24 months'),
      date_trunc('month', now() - make_interval(days => p.keep_days)) - interval '1 month',
      interval '1 month') m
   WHERE NOT EXISTS (SELECT 1 FROM public.db_archive_exports e
                      WHERE e.table_name = p.table_name AND e.month = m::date)
   ORDER BY 1;")

[[ -z "$TODO" ]] && { ok "nothing to export"; exit 0; }

while IFS='|' read -r TABLE TS MONTH; do
  YM="${MONTH:0:7}"
  RANGE="$TS >= '$MONTH'::timestamptz AND $TS < ('$MONTH'::date + interval '1 month')::timestamptz"
  N=$(q "SELECT count(*) FROM public.\"$TABLE\" WHERE $RANGE")
  KEY="$PREFIX/$TABLE/$YM.csv.gz"
  echo "$TABLE $YM rows=$N -> s3://$BUCKET/$KEY"
  [[ $DRY -eq 1 ]] && continue

  F="$WORK/$TABLE-$YM.csv.gz"
  if [[ "$N" -gt 0 ]]; then
    q "\\copy (SELECT * FROM public.\"$TABLE\" WHERE $RANGE ORDER BY $TS) TO STDOUT WITH (FORMAT csv, HEADER)" | gzip -9 > "$F"
    LINES=$(gzip -dc "$F" | wc -l | tr -d ' ')
    # header + rows; multi-line CSV values can only add lines, never remove
    if [[ "$LINES" -lt $((N + 1)) ]]; then fail "$TABLE $YM wrote $LINES lines for $N rows"; fi
  else
    : | gzip -9 > "$F"
  fi
  SHA=$(sha256sum "$F" | cut -d' ' -f1)
  "$AWS" s3 cp "$F" "s3://$BUCKET/$KEY" --only-show-errors --sse AES256 \
    --checksum-algorithm SHA256 --metadata "rows=$N,sha256=$SHA,table=$TABLE,month=$YM"
  "$AWS" s3api head-object --bucket "$BUCKET" --key "$KEY" >/dev/null
  q "INSERT INTO public.db_archive_exports (table_name, month, s3_uri, row_count, sha256)
                   VALUES ('$TABLE', '$MONTH', 's3://$BUCKET/$KEY', $N, '$SHA')
                   ON CONFLICT (table_name, month) DO NOTHING"
done <<< "$TODO"
ok "exported $(grep -c . <<< "$TODO") table-months"
