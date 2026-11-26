#!/usr/bin/env bash
# Build vm-agent.tar.gz (consumed by the Image Builder component) and optionally upload it
# with the service catalog:  ./package-agent.sh <out-dir> [s3://<bucket>]
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
OUT=${1:?out dir}; DEST=${2:-}
mkdir -p "$OUT"
tar -C "$HERE/vm-agent" -czf "$OUT/vm-agent.tar.gz" bin systemd nginx
echo "wrote $OUT/vm-agent.tar.gz"
if [ -n "$DEST" ]; then
  aws s3 cp "$OUT/vm-agent.tar.gz" "$DEST/agent/vm-agent.tar.gz"
  [ -f "$HERE/vm-agent/services.json" ] && aws s3 cp "$HERE/vm-agent/services.json" "$DEST/agent/services.json"
fi
