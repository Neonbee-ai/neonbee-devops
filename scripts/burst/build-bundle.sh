#!/bin/bash
# Build the burst VM bundle into OUT (default ./burst-out): parts of one archive, each <= 1900 MiB
# (release assets cap at 2 GiB), plus burst-bundle.manifest ("sha256  part" lines, in order).
#
# bundle/
#   runner.tgz        latest actions/runner linux-x64
#   node.tar.xz       latest Node v22 (>= 22.12) linux-x64
#   gitleaks.tar.gz   gitleaks 8.30.1
#   wheels/           semgrep + deps for CPython 3.12 manylinux x86_64 (Ubuntu 24.04), if pip is present
#   npm/              warm npm cache from the main repos' lockfiles (VM: /home/runner/.npm)
#   ms-playwright/    Chromium for every Playwright version in those lockfiles (VM: ~/.cache/ms-playwright)
#   VERSIONS
#
# Env: GH_TOKEN (contents:read on the warm repos; without it the npm/Playwright warm-up is skipped),
#      NB_BURST_WARM_REPOS ("repo[:dir] ..." in Neonbee-ai), OUT, PART_SIZE (1900M).
set -euo pipefail
ORG=${ORG:-Neonbee-ai}
OUT=${OUT:-$PWD/burst-out}; PART_SIZE=${PART_SIZE:-1900M}; GL=8.30.1
WARM=${NB_BURST_WARM_REPOS:-so360-core so360-shell-fe so360-shell-fe:packages/design-system so360-crm-be so360-crm-fe so360-accounting-be so360-accounting-fe so360-neura-be so360-neura-fe}
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
B=$W/bundle; mkdir -p "$B" "$OUT"; rm -f "$OUT"/burst-bundle.*
dl() { curl -fsSL --retry 3 -o "$2" "$1"; }

RV=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | jq -r .tag_name | sed 's/^v//')
dl "https://github.com/actions/runner/releases/download/v$RV/actions-runner-linux-x64-$RV.tar.gz" "$B/runner.tgz"
NV=$(curl -fsSL https://nodejs.org/dist/index.json | jq -r '[.[] | select(.version | startswith("v22."))][0].version')
case "$NV" in v22.1[2-9].*|v22.[2-9][0-9].*) ;; *) echo "Node $NV < 22.12" >&2; exit 1 ;; esac
dl "https://nodejs.org/dist/$NV/node-$NV-linux-x64.tar.xz" "$B/node.tar.xz"
dl "https://github.com/gitleaks/gitleaks/releases/download/v$GL/gitleaks_${GL}_linux_x64.tar.gz" "$B/gitleaks.tar.gz"
SG=-
if python3 -m pip --version >/dev/null 2>&1; then
  python3 -m pip download -q -d "$B/wheels" --python-version 3.12 --implementation cp --abi cp312 \
    --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux_2_28_x86_64 \
    --platform manylinux_2_35_x86_64 --only-binary=:all: semgrep && SG=$(ls "$B/wheels" | sed -nE 's/^semgrep-([^-]+)-.*/\1/p' | head -1) ||
    { echo "semgrep wheels unavailable - VM installs from PyPI" >&2; rm -rf "$B/wheels"; }
fi

# Warm npm cache + Playwright browsers. Uses the bundled Node so the cache matches the VM's npm.
PW=; NPMV=-
if [ -n "${GH_TOKEN:-}" ]; then
  mkdir -p "$W/node" && tar xJf "$B/node.tar.xz" -C "$W/node" --strip-components 1
  export PATH="$W/node/bin:$PATH" npm_config_cache="$B/npm" npm_config_audit=false npm_config_fund=false
  NPMV=$(npm -v)
  raw() { curl -fsSL -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github.raw" \
    "https://api.github.com/repos/$ORG/$1/contents/$2"; }
  for spec in $WARM; do
    repo=${spec%%:*}; dir=; [ "$spec" != "$repo" ] && dir=${spec#*:}/
    p=$W/warm/$repo/${dir:-root}; mkdir -p "$p"
    if raw "$repo" "${dir}package.json" > "$p/package.json" && raw "$repo" "${dir}package-lock.json" > "$p/package-lock.json"; then
      (cd "$p" && npm ci --ignore-scripts --no-audit --no-fund --loglevel=error >/dev/null 2>&1) &&
        echo "warm: $spec" || echo "warm: $spec npm ci failed (partial cache kept)" >&2
      PW="$PW $(jq -r '[.packages["node_modules/playwright-core"].version // empty] | .[]' "$p/package-lock.json" 2>/dev/null)"
      rm -rf "$p/node_modules"
    else echo "warm: $spec lockfile not readable - skipped" >&2; fi
  done
  PW=$(tr ' ' '\n' <<<"$PW" | grep -E '^[0-9]' | sort -u | tr '\n' ' ' || true)
  for v in $PW; do
    PLAYWRIGHT_BROWSERS_PATH="$B/ms-playwright" npx -y "playwright@$v" install chromium >/dev/null &&
      echo "playwright $v chromium" || echo "playwright $v install failed" >&2
  done
else echo "GH_TOKEN unset - no npm/Playwright warm-up" >&2; fi

printf 'runner=%s\nnode=%s\nnpm=%s\ngitleaks=%s\nsemgrep=%s\nplaywright=%s\nwarm=%s\nbuilt=%s\n' \
  "$RV" "$NV" "$NPMV" "$GL" "$SG" "${PW:--}" "$WARM" "$(date -u +%FT%TZ)" > "$B/VERSIONS"
cat "$B/VERSIONS"; cp "$B/VERSIONS" "$OUT/VERSIONS"

if command -v zstd >/dev/null; then A=burst-bundle.tar.zst; tar -C "$W" -cf - bundle | zstd -q -T0 -10 > "$W/$A"
else A=burst-bundle.tar.gz; tar -C "$W" -czf "$W/$A" bundle; fi
rm -rf "$B"
split -b "$PART_SIZE" -a 2 "$W/$A" "$OUT/$A.part-"
(cd "$OUT" && for f in "$A".part-*; do sha256sum "$f" 2>/dev/null || shasum -a 256 "$f"; done) > "$OUT/burst-bundle.manifest"
ls -l "$OUT" | awk 'NR>1 {printf "%s %d MiB\n", $NF, $5/1048576}'
