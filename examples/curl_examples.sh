#!/usr/bin/env bash
# Callable examples against a locally running service (default port 8000).
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== health =="
curl -sS "$BASE/healthz"; echo

echo "== 1. direct two-source mix =="
curl -sS -X POST "$BASE/v1/plans" -H 'Content-Type: application/json' \
  --data-binary "@$HERE/01_direct_mix.json" | python3 -m json.tool

echo "== 1b. idempotent replay (same request_id + same content) =="
curl -sS -X POST "$BASE/v1/plans" -H 'Content-Type: application/json' \
  --data-binary "@$HERE/01_direct_mix.json" | python3 -m json.tool

echo "== 1c. same request_id, different content -> 409 conflict =="
python3 - "$HERE/01_direct_mix.json" <<'PY' > /tmp/changed.json
import json, sys
req = json.load(open(sys.argv[1]))
req["diluent"]["volume_nl"] += 1
print(json.dumps(req))
PY
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/v1/plans" \
  -H 'Content-Type: application/json' --data-binary @/tmp/changed.json

echo "== 2. serial dilution through intermediate containers =="
curl -sS -X POST "$BASE/v1/plans" -H 'Content-Type: application/json' \
  --data-binary "@$HERE/02_serial_dilution.json" | python3 -m json.tool

echo "== 3. infeasible request (blocked targets, no partial plan) =="
curl -sS -X POST "$BASE/v1/plans" -H 'Content-Type: application/json' \
  --data-binary "@$HERE/03_infeasible.json" | python3 -m json.tool

echo "== 4. fetch a frozen plan =="
curl -sS "$BASE/v1/plans/demo-direct-001" | python3 -m json.tool
