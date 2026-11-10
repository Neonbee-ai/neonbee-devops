#!/bin/bash
# Publish OUT (from build-bundle.sh) to release `burst-bundle` of this repo, replacing every asset.
# Env: GH_TOKEN (github.token with contents:write), GITHUB_REPOSITORY, OUT (default ./burst-out).
set -euo pipefail
OUT=${OUT:-$PWD/burst-out}; TAG=burst-bundle; R=${GITHUB_REPOSITORY:-Neonbee-ai/neonbee-devops}
api() { local m=$1 u=$2; shift 2; curl -fsS -m 120 -X "$m" -H "Authorization: Bearer $GH_TOKEN" \
  -H 'Accept: application/vnd.github+json' -H 'X-GitHub-Api-Version: 2022-11-28' "$u" "$@"; }
[ -s "$OUT/burst-bundle.manifest" ] || { echo "no manifest in $OUT" >&2; exit 1; }
rel=$(api GET "https://api.github.com/repos/$R/releases/tags/$TAG" 2>/dev/null || true)
[ -n "$rel" ] || rel=$(api POST "https://api.github.com/repos/$R/releases" -d "$(jq -cn --arg t "$TAG" \
  '{tag_name:$t, name:"Burst VM bundle", make_latest:"false", prerelease:true,
    body:"Built by .github/workflows/burst-bundle.yml; pulled by scripts/burst/vm-startup.sh. Assets replaced on every build."}')")
id=$(jq -r .id <<<"$rel")
for a in $(jq -r '.assets[].id' <<<"$rel"); do api DELETE "https://api.github.com/repos/$R/releases/assets/$a" >/dev/null; done
for f in $(awk '{print $2}' "$OUT/burst-bundle.manifest") burst-bundle.manifest VERSIONS; do
  [ -f "$OUT/$f" ] || continue
  api POST "https://uploads.github.com/repos/$R/releases/$id/assets?name=$f" -H 'Content-Type: application/octet-stream' \
    --data-binary @"$OUT/$f" >/dev/null && echo "uploaded $f ($(( $(wc -c < "$OUT/$f") / 1048576 )) MiB)"
done
