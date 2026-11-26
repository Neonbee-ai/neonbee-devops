#!/usr/bin/env bash
# Shared helpers for the Fargate DB jobs (infra/db-backup-fargate.yml).
# Env from the task definition: SUPABASE_ACCESS_TOKEN + SB_REF (Secrets Manager), BUCKET.
set -euo pipefail
: "${SUPABASE_ACCESS_TOKEN:?missing}" "${SB_REF:?missing}" "${BUCKET:?missing}"

ok()   { echo "NB_JOB_OK job=$JOB $*"; }
fail() { echo "NB_JOB_FAILED job=$JOB $*"; exit 1; }

# Short-lived login-role credentials minted by the Supabase CLI (never echoed, no stored DB password).
pg_login() {
  local proj; proj="$(mktemp -d)"
  ( cd "$proj"
    supabase init --force >/dev/null
    echo "" | supabase link --project-ref "$SB_REF" >/dev/null 2>&1 || fail "supabase link failed (token expired/revoked?)"
    supabase db dump --linked --dry-run 2>/dev/null \
      | grep -E '^export PG(HOST|PORT|USER|PASSWORD|DATABASE)="' > "$proj/pgenv.sh" || true )
  [ "$(grep -c '^export PG' "$proj/pgenv.sh")" -eq 5 ] || fail "could not read temp PG credentials"
  # shellcheck disable=SC1091
  . "$proj/pgenv.sh"; rm -rf "$proj"
  export PGSSLMODE=require PGCONNECT_TIMEOUT=30
}

# psql with SET ROLE postgres (the temp login role must assume postgres to read every schema).
q() { printf 'SET ROLE postgres;\n%s\n' "$1" | psql -X -q -v ON_ERROR_STOP=1 -At; }
