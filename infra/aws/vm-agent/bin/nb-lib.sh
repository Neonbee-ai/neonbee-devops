#!/usr/bin/env bash
# Shared helpers for the Neonbee VM agent. Sourced, never executed.
# Layout on the node:
#   /etc/nb-agent/agent.conf                  written by launch-template user data
#   /var/lib/nb-agent/services.json           service catalog (from s3://<bucket>/agent/services.json)
#   /opt/neonbee/services/<svc>/releases/<sha>  unpacked bundles (dist + extras + nb-release.json)
#   /opt/neonbee/services/<svc>/current       symlink -> releases/<sha> (PM2 cwd, stable path)
#   /opt/neonbee/services/<svc>/.nb-ecosystem.<svc>.json   per-app PM2 ecosystem file
#   /var/lib/nb-agent/www/__lb_health         present only while the node is healthy

NB_CONF=${NB_CONF:-/etc/nb-agent/agent.conf}
NB_ROOT=${NB_ROOT:-/opt/neonbee}
NB_STATE=${NB_STATE:-/var/lib/nb-agent}
NB_LOG_DIR=${NB_LOG_DIR:-/var/log/nb-agent}
NB_USER=${NB_USER:-neonbee}
NB_KEEP_RELEASES=${NB_KEEP_RELEASES:-3}
export PM2_HOME=${PM2_HOME:-$NB_ROOT/.pm2}

# shellcheck disable=SC1090
[ -f "$NB_CONF" ] && . "$NB_CONF"
: "${NB_REGION:=ap-south-1}"
export AWS_DEFAULT_REGION=$NB_REGION
# Every AWS VM runs the scheduler behind the DB run-once lock (see infra/aws/README.md).
export NB_SCHEDULER_DB_LOCK=${NB_SCHEDULER_DB_LOCK:-true}

mkdir -p "$NB_STATE/www" "$NB_STATE/deployed" "$NB_LOG_DIR" 2>/dev/null || true

nb_log() { printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${NB_COMPONENT:-nb-agent}" "$*" | tee -a "$NB_LOG_DIR/agent.log" >&2; }
nb_die() { nb_log "ERROR: $*"; exit 1; }

# Run PM2 as the app user with the shared PM2_HOME. Never call `pm2 ... all`.
nb_pm2() { sudo -u "$NB_USER" -H env PM2_HOME="$PM2_HOME" pm2 "$@"; }

nb_imds() {
  local token
  token=$(curl -fsS -m 2 -X PUT http://169.254.169.254/latest/api/token \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60') || return 1
  curl -fsS -m 2 -H "X-aws-ec2-metadata-token: $token" "http://169.254.169.254/latest/meta-data/$1"
}

# Catalog lookup: nb_svc <service> <field> [default]
nb_svc() {
  jq -r --arg n "$1" --arg f "$2" --arg d "${3:-}" \
    '(.services[] | select(.name == $n) | .[$f]) // $d' "$NB_STATE/services.json"
}

# Catalog lookup as compact JSON (arrays/objects such as args, env): nb_svc_json <service> <field>
# Prints "null" when the field is absent.
nb_svc_json() {
  jq -c --arg n "$1" --arg f "$2" '(.services[] | select(.name == $n) | .[$f]) // null' "$NB_STATE/services.json"
}

nb_fetch_catalog() {
  local tmp="$NB_STATE/services.json.tmp"
  aws s3 cp "s3://$NB_RELEASE_BUCKET/agent/services.json" "$tmp" --only-show-errors \
    && jq -e '.services | length > 0' "$tmp" >/dev/null \
    && mv -f "$tmp" "$NB_STATE/services.json"
}

# Render <dir>/.env from Secrets Manager: <prefix>_shared, then <prefix><svc> (svc wins).
nb_render_env() {
  local svc="$1" dir="$2" out="$2/.env.tmp" id json
  : > "$out"; chmod 600 "$out"
  for id in "${NB_SECRET_PREFIX}_shared" "${NB_SECRET_PREFIX}${svc}"; do
    json=$(aws secretsmanager get-secret-value --secret-id "$id" --query SecretString --output text 2>/dev/null) || continue
    # dotenv keeps single-quoted values literal (backslashes, '"', '#', '$', trailing '='),
    # but in double quotes it turns \n into a newline and keeps \" / \\ as-is, so @json
    # corrupts any value with a backslash or quote. Single-quote unless the value holds a
    # ' (then plain double quotes if it has no \ or ", else @json as a last resort).
    printf '%s' "$json" | jq -r --arg q "'" 'to_entries[] | (.value|tostring) as $v | "\(.key)=" +
      (if ($v|contains($q)|not) then $q + $v + $q
       elif ($v|test("[\\\\\"]")|not) then "\"" + $v + "\""
       else ($v|@json) end)' >> "$out" \
      || nb_die "secret $id is not a JSON object"
  done
  printf 'NB_SCHEDULER_DB_LOCK=true\nNODE_ENV=production\n' >> "$out"
  chown "$NB_USER:$NB_USER" "$out"
  mv -f "$out" "$dir/.env"
}
