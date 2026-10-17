#!/bin/bash
# A pointer delivered while fswatch is alive but silent (an fseventsd stall) is
# still announced by the bounded catch-up sweep: once, and never if answered.
# Pass --noisy to show unrelated events cannot postpone the catch-up deadline.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

# Hermetic on a pool host: no live seat's resolver, handler or workspace leaks in.
for v in $(compgen -e | grep '^SUTANDO_'); do unset "$v"; done
TMP="$(mktemp -d)"
WATCHER_PID=""
# Not `kill -TERM -$PID`: without job control the watcher shares this test's group.
trap 'if [ -n "$WATCHER_PID" ]; then kill -TERM "$WATCHER_PID" 2>/dev/null || true; wait "$WATCHER_PID" 2>/dev/null; fi; rm -rf "$TMP"' EXIT

CATCHUP=2
WS="$TMP/ws"; INBOX="$WS/deliveries/w-test"
mkdir -p "$WS/tasks" "$WS/results" "$WS/state" "$INBOX" "$TMP/bin"
# Alive and blocked, reporting nothing: what a stalled fseventsd looks like from here.
printf '#!/bin/sh\nexec tail -f /dev/null\n' > "$TMP/bin/fswatch"
if [ "${1:-}" = "--noisy" ]; then
  printf '#!/bin/sh\nwhile :; do printf "/unrelated-event\\n"; sleep 0.1; done\n' > "$TMP/bin/fswatch"
fi
chmod +x "$TMP/bin/fswatch"
# Once armed, a cleanup step outlasts any catch-up read timeout still pending.
SLOW="$TMP/slow-cleanup"
printf '#!/bin/sh\n[ -e "%s" ] && sleep %s\nexec %s "$@"\n' "$SLOW" "$(( CATCHUP + 1 ))" "$(command -v pgrep)" > "$TMP/bin/pgrep"
chmod +x "$TMP/bin/pgrep"
# A pointer's payload is the same-named file in tasks/; rc 3 is "no payload".
RESOLVER="$TMP/resolver.sh"
cat > "$RESOLVER" <<EOF
#!/bin/sh
p='$WS/tasks/'"\$(basename "\$1")"
[ -f "\$p" ] || exit 3
printf '%s\n' "\$p"
EOF
chmod +x "$RESOLVER"

OUT="$TMP/out"; ERR="$TMP/err"; : > "$OUT"; : > "$ERR"
PATH="$TMP/bin:$PATH" \
SUTANDO_WORKSPACE_DIR="$WS" \
SUTANDO_RESULTS_DIR="$WS/results" \
SUTANDO_INBOX_RESOLVER="$RESOLVER" \
SUTANDO_WATCHER_CATCHUP_SECONDS="$CATCHUP" \
bash "$REPO/src/watch-tasks-stream.sh" "$INBOX" --role standby --inbox "$INBOX" > "$OUT" 2>"$ERR" &
WATCHER_PID=$!

# The sentinel is stamped after the startup sweep, so anything dropped after it
# can only be found by catch-up.
for _ in $(seq 1 100); do
  compgen -G "$WS/state/watch-tasks-stream*.pid" > /dev/null && break
  sleep 0.1
done
compgen -G "$WS/state/watch-tasks-stream*.pid" > /dev/null
check $? "the watcher subscribed (sentinel stamped after the startup sweep)"

# An answered task: its result is live before its pointer lands.
printf 'id: task-answered\naccess_tier: owner\ntask: already done\n' > "$WS/tasks/task-answered.txt"
printf 'done.\n' > "$WS/results/task-answered.txt"
: > "$INBOX/task-answered.txt"
# A live task: payload first, then the pointer, as the pool writer delivers.
printf 'id: task-catch-up\naccess_tier: owner\ntask: recover me\n' > "$WS/tasks/task-catch-up.txt"
dropped=$(date +%s)
: > "$INBOX/task-catch-up.txt"

LIVE="TASK_FILE: $WS/tasks/task-catch-up.txt"
seen=""
for _ in $(seq 1 $(( (CATCHUP + 4) * 10 ))); do
  grep -qF "$LIVE" "$OUT" && { seen=$(date +%s); break; }
  sleep 0.1
done
took="never"; [ -z "$seen" ] || took="$(( seen - dropped ))s"
[ -n "$seen" ] && [ $(( seen - dropped )) -le $(( CATCHUP + 3 )) ]
check $? "a pointer fswatch never reported is announced within ${CATCHUP}s + 3s margin (took $took)"

# Three more catch-up intervals: each re-sweeps, none may re-announce.
sleep $(( CATCHUP * 3 + 1 ))
[ "$(grep -cF "$LIVE" "$OUT")" = "1" ]
check $? "it is announced exactly once across further catch-up sweeps (got $(grep -cF "$LIVE" "$OUT"))"
! grep -q 'task-answered' "$OUT"
check $? "a pointer whose task already has a result is never announced"
grep -q 'already answered, not dispatching again: .*task-answered' "$ERR"
check $? "...because the sweep reached it and refused it, not because nothing looked"
kill -0 "$WATCHER_PID" 2>/dev/null
check $? "the watcher is still live (catch-up runs under the subscription, not instead of it)"

# bash 3.2's read -t alarm must not cut the TERM trap short and leave the watcher running.
: > "$SLOW"
kill -TERM "$WATCHER_PID"
for _ in $(seq 1 $(( (CATCHUP + 1) * 3 * 10 ))); do kill -0 "$WATCHER_PID" 2>/dev/null || break; sleep 0.1; done
if kill -0 "$WATCHER_PID" 2>/dev/null; then
  pkill -9 -P "$WATCHER_PID" 2>/dev/null; kill -9 "$WATCHER_PID" 2>/dev/null; trc=137
else
  wait "$WATCHER_PID"; trc=$?
fi
WATCHER_PID=""
[ "$trc" = "0" ]
check $? "TERM during a pending catch-up read still stops the watcher through its trap (exit $trc)"

echo
if [ "$fail" = "0" ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail of $((pass+fail)) checks failed"; exit 1; fi
