#!/usr/bin/env bash
# Backend smoke probes for the web UI — boot the app, hit every
# route a human reviewer would click, assert expected status, exit
# non-zero on any failure.
#
# Usage: bash tests/web-ui-smoke.sh
# Env:   WORKSPACE_PATH, CLI_CHATS_PATH inherited from the calling shell.

set -u

PORT="${CLAW_QA_PORT:-3001}"   # use 3001 so a running 3000 instance isn't disturbed
BASE="http://127.0.0.1:${PORT}"
# Per-call curl bounds — connection setup capped at 2s, total wall-clock at
# 5s — so a stalled socket can't hang the script indefinitely.
CURL_FLAGS=(--silent --show-error --connect-timeout 2 --max-time 5)
LOG=$(mktemp)
trap "rm -f $LOG" EXIT

cd "$(dirname "$0")/.." || exit 1

# Boot the app in the background on the chosen port.
python3 app.py --port "$PORT" > "$LOG" 2>&1 &
APP_PID=$!
trap "kill $APP_PID 2>/dev/null; rm -f $LOG" EXIT

# Wait up to 15s for /api/workspaces to respond. Connection-refused during
# this loop is expected (app is still booting), so stderr is muted; the
# final check below re-runs with stderr visible if the wait times out.
for i in $(seq 1 30); do
  if curl "${CURL_FLAGS[@]}" -f -o /dev/null "$BASE/api/workspaces" 2>/dev/null; then break; fi
  sleep 0.5
done

if ! curl "${CURL_FLAGS[@]}" -f -o /dev/null "$BASE/api/workspaces"; then
  echo "[FAIL] app never became responsive on $PORT"
  echo "--- boot log ---"; cat "$LOG"
  exit 1
fi

fail=0
probe() {
  local label="$1" url="$2" expect="$3"
  local code
  code=$(curl "${CURL_FLAGS[@]}" -o /dev/null -w "%{http_code}" "$BASE$url")
  if [ "$code" = "$expect" ]; then
    printf "  [pass]  %-44s %s (expected %s)\n" "$label" "$code" "$expect"
  else
    printf "  [FAIL]  %-44s %s (expected %s)\n" "$label" "$code" "$expect"
    fail=$((fail + 1))
  fi
}

echo "=== Page routes ==="
probe "/"            "/"            200
probe "/search"      "/search"      200
probe "/config"      "/config"      200

echo ""
echo "=== JSON API ==="
probe "/api/workspaces"          "/api/workspaces"          200
probe "/api/composers"           "/api/composers"           200
probe "/api/detect-environment"  "/api/detect-environment"  200
probe "/api/search?q=foo"        "/api/search?q=foo"        200
probe "/api/search (no q -> 400)" "/api/search"             400

# Find a non-global workspace id to drive the workspace-scoped probes.
# Failing to PARSE /api/workspaces is a real bug (200 with malformed JSON
# would otherwise slip through as a false green), so the python parse
# runs without `2>/dev/null` and exit-status is checked separately.
if WS_ID=$(curl "${CURL_FLAGS[@]}" "$BASE/api/workspaces" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for w in data:
    if w.get('id') and w['id'] != 'global':
        print(w['id'])
        break
"); then
  if [ -n "$WS_ID" ]; then
    echo ""
    echo "=== Workspace-scoped routes (WS=$WS_ID) ==="
    probe "/workspace/<id>"            "/workspace/$WS_ID"               200
    probe "/api/workspaces/<id>"       "/api/workspaces/$WS_ID"          200
    probe "/api/workspaces/<id>/tabs"  "/api/workspaces/$WS_ID/tabs"     200
  else
    echo ""
    echo "[skip] no non-global workspace found; workspace-scoped probes skipped"
  fi
else
  printf "\n  [FAIL]  %-44s parse error on /api/workspaces payload\n" "workspace-id extraction"
  fail=$((fail + 1))
fi

echo ""
if [ "$fail" -eq 0 ]; then
  echo "all smoke probes pass"
  exit 0
else
  echo "$fail probe(s) failed — see /tmp boot log for context"
  echo "--- boot log tail ---"; tail -20 "$LOG"
  exit 1
fi
