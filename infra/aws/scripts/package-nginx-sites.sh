#!/usr/bin/env bash
# package-nginx-sites.sh [--upload <bucket>] [--sha <id>]
# Regenerates infra/aws/nginx/sites from services.json, then builds the nb-nginx-sites
# release: tar of sites/ + ensure-tls.sh + nb-release.json -> zstd (gzip fallback).
# With --upload: s3://<bucket>/nb-nginx-sites/<sha>.tar.zst (+ .sha256) and
# nb-nginx-sites/current.json {sha,key,prev,prev_key} (same format as the deploy workflow).
# Nodes pick it up on boot (nb-boot reads current.json) or via the nb-deploy SSM document.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; NGX="$HERE/../nginx"
BUCKET=""; SHA=""
while [ $# -gt 0 ]; do case "$1" in
  --upload) BUCKET=${2:?bucket}; shift 2 ;;
  --sha) SHA=${2:?sha}; shift 2 ;;
  *) echo "usage: $0 [--upload <bucket>] [--sha <id>]" >&2; exit 2 ;;
esac; done
[ -n "$SHA" ] || SHA=$(git -C "$HERE" rev-parse --short=12 HEAD)
python3 "$NGX/gen_sites.py" >/dev/null
if [ -n "$BUCKET" ] && [ -n "$(git -C "$HERE" status --porcelain -- "$NGX")" ]; then
  echo "refusing to upload: infra/aws/nginx has uncommitted changes (sha $SHA would not match)" >&2; exit 1
fi
OUT=$(mktemp -d); trap 'rm -rf "$OUT"' EXIT
mkdir -p "$OUT/stage"
cp -R "$NGX/sites" "$NGX/ensure-tls.sh" "$OUT/stage/"
printf '{"service":"nb-nginx-sites","sha":"%s","created_at":"%s"}\n' "$SHA" "$(date -u +%FT%TZ)" > "$OUT/stage/nb-release.json"
if command -v zstd >/dev/null 2>&1; then EXT=tar.zst; tar -C "$OUT/stage" -cf - . | zstd -q -T0 -10 -o "$OUT/bundle.$EXT"
else EXT=tar.gz; tar -C "$OUT/stage" -czf "$OUT/bundle.$EXT" .; fi
SUM=$( { shasum -a 256 "$OUT/bundle.$EXT" 2>/dev/null || sha256sum "$OUT/bundle.$EXT"; } | awk '{print $1}')
KEY="nb-nginx-sites/$SHA.$EXT"
echo "bundle $KEY ($(wc -c < "$OUT/bundle.$EXT") bytes, sha256 $SUM)"
[ -n "$BUCKET" ] || { cp "$OUT/bundle.$EXT" "./nb-nginx-sites-$SHA.$EXT"; echo "dry run: wrote ./nb-nginx-sites-$SHA.$EXT"; exit 0; }
aws s3 cp "$OUT/bundle.$EXT" "s3://$BUCKET/$KEY" --only-show-errors
printf '%s\n' "$SUM" > "$OUT/bundle.sha256"; aws s3 cp "$OUT/bundle.sha256" "s3://$BUCKET/$KEY.sha256" --only-show-errors
PSHA=""; PKEY=""
if aws s3 cp "s3://$BUCKET/nb-nginx-sites/current.json" "$OUT/cur.json" --only-show-errors 2>/dev/null; then
  PSHA=$(jq -r '.sha // ""' "$OUT/cur.json"); PKEY=$(jq -r '.key // ""' "$OUT/cur.json")
fi
printf '{"sha":"%s","key":"%s","prev":"%s","prev_key":"%s","updated_at":"%s"}\n' "$SHA" "$KEY" "$PSHA" "$PKEY" "$(date -u +%FT%TZ)" > "$OUT/current.json"
aws s3 cp "$OUT/current.json" "s3://$BUCKET/nb-nginx-sites/current.json" --only-show-errors --content-type application/json
echo "promoted s3://$BUCKET/nb-nginx-sites/current.json -> $SHA (prev: ${PSHA:-none})"
