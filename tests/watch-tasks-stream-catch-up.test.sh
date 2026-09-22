#!/bin/bash
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
WATCHER_PID=""
unset SUTANDO_INBOX_RESOLVER
trap 'kill -TERM -"$WATCHER_PID" 2>/dev/null || true; rm -rf "$TMP"' EXIT

WS="$TMP/ws"
mkdir -p "$WS/tasks" "$WS/results" "$WS/state" "$TMP/bin"
cat > "$TMP/bin/fswatch" <<'EOF'
#!/bin/sh
sleep 0.2
EOF
chmod +x "$TMP/bin/fswatch"

OUT="$TMP/out"
PATH="$TMP/bin:$PATH" \
SUTANDO_WORKSPACE_DIR="$WS" \
SUTANDO_RESULTS_DIR="$WS/results" \
SUTANDO_WATCHER_CATCHUP_SECONDS=1 \
bash "$REPO/src/watch-tasks-stream.sh" "$WS/tasks" > "$OUT" 2>/dev/null &
WATCHER_PID=$!

sleep 0.5
printf 'id: catch-up\naccess_tier: owner\ntask: recover me\n' > "$WS/tasks/task-catch-up.txt"
for _ in $(seq 1 20); do
  grep -q 'TASK_FILE: task-catch-up.txt' "$OUT" 2>/dev/null && break
  sleep 0.25
done

count="$(grep -c 'TASK_FILE: task-catch-up.txt' "$OUT" 2>/dev/null || true)"
if [ "$count" -eq 1 ] && kill -0 "$WATCHER_PID" 2>/dev/null; then
  echo 'PASS — catch-up scan emits once and re-arms after fswatch exits.'
else
  echo "FAIL — expected one emission from a live re-armed watcher, got $count."
  exit 1
fi
