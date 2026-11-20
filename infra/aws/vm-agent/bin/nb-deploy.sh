#!/usr/bin/env bash
# nb-deploy.sh <service> <sha> <bucket> <key>
# Called by the SSM document nb-deploy (deploy workflow, plan 2.4) and by nb-boot.sh.
# Downloads s3://<bucket>/<key>, verifies <key>.sha256, unpacks into releases/<sha>,
# renders .env from Secrets Manager, flips the `current` symlink, reloads ONLY this
# PM2 app (or reloads nginx for type=nginx), waits for the service health check and
# rolls back to the previous release if it fails. Exit 0 = deployed and healthy.
set -euo pipefail
NB_COMPONENT=nb-deploy
# shellcheck source=nb-lib.sh
. "$(dirname "$0")/nb-lib.sh"

SVC=${1:?service}; SHA=${2:?sha}; BUCKET=${3:?bucket}; KEY=${4:?key}
[[ $SVC =~ ^[a-z0-9][a-z0-9-]{1,63}$ ]] || nb_die "bad service name $SVC"
[[ $SHA =~ ^[A-Za-z0-9._-]{4,64}$ ]] || nb_die "bad sha $SHA"
[[ $KEY == "$SVC/"* ]] || nb_die "key $KEY is not under $SVC/"

[ -f "$NB_STATE/services.json" ] || nb_fetch_catalog || nb_die "no service catalog"
TYPE=$(nb_svc "$SVC" type node)
[ -n "$(nb_svc "$SVC" name)" ] || nb_die "$SVC is not in the service catalog (excluded or unknown)"

SDIR="$NB_ROOT/services/$SVC"
REL="$SDIR/releases/$SHA"
CUR="$SDIR/current"
mkdir -p "$SDIR/releases"
exec 9>"$SDIR/.deploy.lock"
flock -w 600 9 || nb_die "another deploy of $SVC is running"

PREV=""
[ -L "$CUR" ] && PREV=$(readlink "$CUR")

# ---------------------------------------------------------------- fetch + unpack
if [ ! -f "$REL/nb-release.json" ]; then
  TMP=$(mktemp -d "$SDIR/.dl.XXXXXX"); trap 'rm -rf "$TMP"' EXIT
  aws s3 cp "s3://$BUCKET/$KEY" "$TMP/bundle" --only-show-errors || nb_die "download $KEY failed"
  if aws s3 cp "s3://$BUCKET/$KEY.sha256" "$TMP/bundle.sha256" --only-show-errors 2>/dev/null; then
    WANT=$(tr -d ' \n' < "$TMP/bundle.sha256"); GOT=$(sha256sum "$TMP/bundle" | awk '{print $1}')
    [ "$WANT" = "$GOT" ] || nb_die "checksum mismatch for $KEY"
  fi
  mkdir -p "$TMP/x"
  case "$KEY" in
    *.tar.zst) zstd -dc "$TMP/bundle" | tar -C "$TMP/x" -xf - ;;
    *.tar.gz)  tar -C "$TMP/x" -xzf "$TMP/bundle" ;;
    *) nb_die "unknown bundle type $KEY" ;;
  esac
  rm -rf "$REL"; mv "$TMP/x" "$REL"
  chown -R "$NB_USER:$NB_USER" "$REL"
fi

health_ok() {
  local port path tries="${1:-30}" i
  if [ "$TYPE" = nginx ]; then nginx -t -q 2>/dev/null && systemctl is-active --quiet nginx; return; fi
  port=$(nb_svc "$SVC" port); path=$(nb_svc "$SVC" health_path /health)
  [ -n "$port" ] || return 0
  for ((i = 1; i <= tries; i++)); do
    curl -fsS -m 5 -o /dev/null "http://127.0.0.1:$port$path" && return 0
    sleep 2
  done
  return 1
}

activate() {  # switch `current` to $1 (absolute release dir) and (re)start
  local target="$1" script action
  if [ "$TYPE" = nginx ]; then
    ln -sfn "$target" "$CUR.new" && mv -Tf "$CUR.new" "$CUR"

    # Bundle holds sites/*.conf; /etc/nginx/sites-enabled/neonbee -> current/sites (set in the AMI).
    nginx -t -q && systemctl reload nginx
    return
  fi
  nb_render_env "$SVC" "$target"
  if [ "$(nb_svc "$SVC" install false)" = true ] && [ ! -d "$target/node_modules" ] && [ -f "$target/package-lock.json" ]; then
    # Runtime deps only, no build (runner builds; plan 2.4 moves node_modules into the bundle).
    sudo -u "$NB_USER" -H bash -c "cd '$target' && npm ci --omit=dev --ignore-scripts --no-audit --no-fund" >&2
  fi
  script=$(nb_svc "$SVC" script auto)
  if [ "$script" = auto ]; then
    script=$(cd "$target" && find dist -name main.js -not -path '*/node_modules/*' | sort | head -1)
    [ -n "$script" ] || { nb_log "no dist/**/main.js in $SVC release; set .script in the catalog"; return 1; }
  fi
  if [ "$script" = .next/standalone/server.js ]; then
    # Next standalone: do what the app's `npm start` does on the VM today - put static
    # assets, public/ and the rendered .env beside server.js (it chdirs to its own dir).
    [ -f "$target/$script" ] || { nb_log "no $script in $SVC release"; return 1; }
    cp -r "$target/.next/static" "$target/.next/standalone/.next/" 2>/dev/null || true
    [ -d "$target/public" ] && cp -r "$target/public" "$target/.next/standalone/"
    cp "$target/.env" "$target/.next/standalone/.env"
    chown -R "$NB_USER:$NB_USER" "$target/.next/standalone"
  fi
  # Everything that can fail on a bad bundle happens BEFORE the symlink flips.
  ln -sfn "$target" "$CUR.new" && mv -Tf "$CUR.new" "$CUR"
  local eco="$SDIR/.nb-ecosystem.$SVC.json"
  action=$(nb_pm2 jlist 2>/dev/null | NB_NAME="$SVC" NB_SCRIPT="$CUR/$script" NB_CWD="$CUR" NB_ECO="$eco" \
    NB_INST="$(nb_svc "$SVC" instances 1)" NB_MEM="$(nb_svc "$SVC" max_memory 1G)" \
    NB_PORT="$(nb_svc "$SVC" port)" NB_EXEC="$(nb_svc "$SVC" exec_mode cluster)" \
    NB_ARGS="$(nb_svc_json "$SVC" args)" NB_ENV="$(nb_svc_json "$SVC" env)" \
    node "$(dirname "$0")/nb-pm2-apply.js") || nb_die "cannot write $eco"
  chown "$NB_USER:$NB_USER" "$eco"
  if [ "$action" = reload ]; then
    nb_pm2 reload "$eco" --only "$SVC" --update-env && return 0
    nb_log "pm2 reload $SVC failed, falling back to delete+start of this app only"
  fi
  nb_pm2 delete "$SVC" >/dev/null 2>&1 || true
  nb_pm2 start "$eco" --only "$SVC" --update-env
}

nb_log "deploy $SVC $SHA (prev: ${PREV:-none})"
if activate "$REL" && health_ok 30; then
  nb_pm2 save --force >/dev/null 2>&1 || true
  printf '{"service":"%s","sha":"%s","key":"%s","at":"%s"}\n' "$SVC" "$SHA" "$KEY" "$(date -u +%FT%TZ)" \
    > "$NB_STATE/deployed/$SVC.json"
  # Keep the newest N releases plus whatever `current` points at.
  (cd "$SDIR/releases" && ls -1t | tail -n +$((NB_KEEP_RELEASES + 1)) | while read -r d; do
     [ "$SDIR/releases/$d" = "$(readlink "$CUR")" ] || rm -rf "${SDIR:?}/releases/$d"; done) || true
  nb_log "deploy $SVC $SHA OK"
  exit 0
fi

nb_log "deploy $SVC $SHA FAILED health"
if [ -n "$PREV" ] && [ "$PREV" != "$REL" ] && [ -d "$PREV" ]; then
  nb_log "rolling $SVC back to $PREV"
  activate "$PREV" && health_ok 30 && nb_log "rollback of $SVC OK" || nb_log "rollback of $SVC ALSO FAILED"
  nb_pm2 save --force >/dev/null 2>&1 || true
fi
exit 1
