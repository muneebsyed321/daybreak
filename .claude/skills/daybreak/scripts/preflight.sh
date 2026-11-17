#!/usr/bin/env bash
# DAYBREAK preflight: optional reseed or URL change, one run, compact report.
set -euo pipefail
API="${DAYBREAK_API:-http://localhost:8000}"

curl -sf "$API/" >/dev/null || { echo "backend down at $API"; exit 1; }

case "${1:-}" in
  demo) curl -sf -X POST "$API/api/seed-demo" >/dev/null ;;
  url)  curl -sf -X POST "$API/api/update-url" -H 'content-type: application/json' \
          -d "{\"url\": \"${2:?usage: preflight.sh url <https://...>}\"}" >/dev/null ;;
esac

curl -sf --max-time 300 -X POST "$API/api/run" >/dev/null
curl -sf "$API/api/report/latest" | python3 "$(dirname "$0")/report.py" "$API"
