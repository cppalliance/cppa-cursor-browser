#!/usr/bin/env bash
# Backend smoke probes for the web UI — boot the app, hit every
# route a human reviewer would click, assert expected status, exit
# non-zero on any failure.
#
# Usage: bash tests/web-ui-smoke.sh
# Env:
#   WORKSPACE_PATH               inherited; path to Cursor workspaceStorage
#   CLI_CHATS_PATH               inherited; path to Cursor CLI chats
#   CLAW_QA_PORT (default 3001)  port for the test instance
#   CLAW_QA_REQUIRE_WORKSPACE    set to "1" to require at least one
#                                non-global workspace; the workspace-scoped
#                                probes will FAIL instead of skip when no
#                                workspace data is reachable. Recommended
#                                in CI runs that seed fixture data.
#
# Note: workspace-scoped probes (/workspace/<id>, /api/workspaces/<id>,
# /api/workspaces/<id>/tabs) are best-effort by default. With no Cursor
# data on the host they are reported as a [WARN] skip so a fresh CI
# environment can still pass the page + JSON-API probes, but the warning
# line makes the partial coverage obvious in the log. CI runs that seed
# a workspace fixture should set CLAW_QA_REQUIRE_WORKSPACE=1 so the
# skip becomes a hard failure.

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

# probe_page: HTTP-200 + body sniff. Catches the "empty 200" template
# regression where a status-only probe would silently pass (e.g. a base
# template renders OK but the per-route block doesn't expand). The needle
# is grepped as a fixed string; UTF-8 em-dashes pass through fine.
probe_page() {
  local label="$1" url="$2" needle="$3"
  local body
  body=$(curl "${CURL_FLAGS[@]}" -w "\n__STATUS__:%{http_code}" "$BASE$url")
  local code="${body##*__STATUS__:}"
  body="${body%__STATUS__:*}"
  if [ "$code" != "200" ]; then
    printf "  [FAIL]  %-44s %s (expected 200)\n" "$label" "$code"
    fail=$((fail + 1))
    return
  fi
  if printf "%s" "$body" | grep -qF "$needle"; then
    printf "  [pass]  %-44s 200 + body contains expected <title>\n" "$label"
  else
    printf "  [FAIL]  %-44s 200 but body missing: %s\n" "$label" "$needle"
    fail=$((fail + 1))
  fi
}

echo "=== Page routes ==="
probe_page "/"        "/"        "<title>Projects — Cursor Chat Browser</title>"
probe_page "/search"  "/search"  "<title>Search — Cursor Chat Browser</title>"
probe_page "/config"  "/config"  "<title>Configuration — Cursor Chat Browser</title>"

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
    echo "=== Workspace-scoped routes ==="
    if [ "${CLAW_QA_REQUIRE_WORKSPACE:-0}" = "1" ]; then
      printf "  [FAIL]  %-44s no non-global workspace reachable (CLAW_QA_REQUIRE_WORKSPACE=1)\n" "workspace-scoped probes"
      fail=$((fail + 1))
    else
      echo "  [WARN]  no non-global workspace reachable — 3 workspace-scoped"
      echo "          probes (/workspace/<id>, /api/workspaces/<id>,"
      echo "          /api/workspaces/<id>/tabs) are NOT exercised in this run."
      echo "          Set CLAW_QA_REQUIRE_WORKSPACE=1 (or seed a fixture) to"
      echo "          turn this skip into a failure."
    fi
  fi
else
  printf "\n  [FAIL]  %-44s parse error on /api/workspaces payload\n" "workspace-id extraction"
  fail=$((fail + 1))
fi

echo ""
if [ "$fail" -eq 0 ]; then
  if [ -z "${WS_ID:-}" ] && [ "${CLAW_QA_REQUIRE_WORKSPACE:-0}" != "1" ]; then
    echo "all smoke probes pass (3 workspace-scoped probes skipped — see [WARN] above)"
  else
    echo "all smoke probes pass"
  fi
  exit 0
else
  echo "$fail probe(s) failed — see /tmp boot log for context"
  echo "--- boot log tail ---"; tail -20 "$LOG"
  exit 1
fi
