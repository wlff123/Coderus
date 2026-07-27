#!/usr/bin/env bash
set -euo pipefail

umask 077
ROOT="${CODERUS_ROOT:-/opt/coderus}"
DATABASE="${CODERUS_DATABASE:-$ROOT/data/coderus.db}"
DRAIN_TIMEOUT="${CODERUS_DRAIN_TIMEOUT:-3600}"
CUTOVER_TIMEOUT="${CODERUS_CUTOVER_TIMEOUT:-30}"
PORT="${CODERUS_PORT:-18082}"
DRAIN_GATE="$ROOT/data/release-draining"
LOCK_FILE="$ROOT/data/release.lock"
ROLLBACK_FAILED="$ROOT/data/ROLLBACK_FAILED"
PUBLIC_KEY="${CODERUS_RELEASE_PUBLIC_KEY:-$ROOT/release-public-key.pem}"
HISTORY_RETAIN="${CODERUS_RELEASE_HISTORY_RETAIN:-20}"
RELEASE_RETAIN="${CODERUS_RELEASE_RETAIN:-5}"
BACKUP_RETAIN="${CODERUS_BACKUP_RETAIN:-20}"
[[ $# -eq 1 ]] || { echo "Usage: $0 <release-id>" >&2; exit 2; }
RELEASE_ID="$1"
[[ "$RELEASE_ID" =~ ^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$ ]] || {
  echo "Invalid release id" >&2
  exit 2
}
TARGET="$ROOT/releases/$RELEASE_ID"
PYTHON="$TARGET/.venv/bin/python"
[[ -d "$TARGET" && ! -L "$TARGET" && -f "$TARGET/VERIFIED" && -x "$PYTHON" ]] || {
  echo "Release is not verified: $RELEASE_ID" >&2
  exit 1
}

# Verification can be expensive, so complete it before draining or taking the release lock.
cd "$TARGET"
"$PYTHON" -m coderus.release_install --verify-release "$TARGET" \
  --public-key "$PUBLIC_KEY"
"$PYTHON" -m coderus.release_ops check-schema "$DATABASE" "$TARGET/release.json"
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "Another release operation is running" >&2; exit 1; }
[[ -L "$ROOT/current" ]] || { echo "Current release is missing" >&2; exit 1; }
OLD_RELEASE="$(readlink -f "$ROOT/current")"
OLD_ID="$(basename "$OLD_RELEASE")"
[[ "$OLD_RELEASE" == "$ROOT/releases/$OLD_ID" ]] || {
  echo "Current release points outside releases directory" >&2
  exit 1
}
[[ "$OLD_ID" != "$RELEASE_ID" ]] || { echo "Release is already active"; exit 0; }
ORIGINAL_PREVIOUS_ID=""
if [[ -L "$ROOT/previous" ]]; then
  original_previous="$(readlink -f "$ROOT/previous")"
  ORIGINAL_PREVIOUS_ID="$(basename "$original_previous")"
  [[ "$original_previous" == "$ROOT/releases/$ORIGINAL_PREVIOUS_ID" ]] || {
    echo "Previous release points outside releases directory" >&2
    exit 1
  }
fi

switch_link() {
  local pointer="$1"
  local release_id="$2"
  rm -f "$ROOT/$pointer"
  ln -s "releases/$release_id" "$ROOT/$pointer"
  [[ "$(readlink "$ROOT/$pointer")" == "releases/$release_id" ]]
}

wait_until_idle() {
  local deadline=$((SECONDS + DRAIN_TIMEOUT))
  local status
  while (( SECONDS < deadline )); do
    if "$PYTHON" -m coderus.release_ops check-idle "$DATABASE"; then
      return 0
    else
      status=$?
    fi
    [[ $status -eq 2 ]] || return 1
    sleep 2
  done
  echo "Timed out waiting for active work to drain" >&2
  return 1
}

remaining_budget() {
  local remaining_ms=$((CUTOVER_DEADLINE_MS - $(date +%s%3N)))
  (( remaining_ms > 0 )) || return 1
  printf '%s\n' "$remaining_ms"
}

remaining_seconds() {
  local remaining_ms
  remaining_ms="$(remaining_budget)" || return 1
  printf '%d.%03d\n' "$((remaining_ms / 1000))" "$((remaining_ms % 1000))"
}

run_with_budget() {
  local remaining
  remaining="$(remaining_seconds)" || return 124
  timeout --foreground "${remaining}s" "$@"
}

mark_rollback_failed() {
  local reason="$1"
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$reason" >"$ROLLBACK_FAILED"
  echo "ROLLBACK FAILED: $reason; drain gate remains enabled" >&2
  exit 2
}

BACKUP=""
MAINTENANCE_PID=""
rollback_failed_promotion() {
  local reason="$1"
  trap - ERR
  if [[ -n "$MAINTENANCE_PID" ]] && kill -0 "$MAINTENANCE_PID" 2>/dev/null; then
    kill "$MAINTENANCE_PID" 2>/dev/null || mark_rollback_failed "cannot stop maintenance"
    for _ in {1..3}; do
      kill -0 "$MAINTENANCE_PID" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$MAINTENANCE_PID" 2>/dev/null; then
      kill -KILL "$MAINTENANCE_PID" 2>/dev/null \
        || mark_rollback_failed "cannot terminate maintenance"
    fi
  fi
  if ! CODERUS_STOP_TIMEOUT=5 CODERUS_FORCE_STOP=1 \
    bash "$ROOT/scripts/container-stop.sh"; then
    mark_rollback_failed "production process did not stop"
  fi
  switch_link current "$OLD_ID" || mark_rollback_failed "cannot restore current link"
  if [[ -n "$ORIGINAL_PREVIOUS_ID" ]]; then
    switch_link previous "$ORIGINAL_PREVIOUS_ID" \
      || mark_rollback_failed "cannot restore previous link"
  else
    rm -f "$ROOT/previous" || mark_rollback_failed "cannot clear previous link"
  fi
  if [[ -n "$BACKUP" ]]; then
    [[ -f "$BACKUP" ]] || mark_rollback_failed "database backup is missing"
    rm -f "${DATABASE}-wal" "${DATABASE}-shm" \
      || mark_rollback_failed "cannot clear stale WAL files"
    "$PYTHON" -m coderus.release_ops backup "$BACKUP" "$DATABASE" \
      || mark_rollback_failed "database restore failed"
  fi
  if ! CODERUS_START_TIMEOUT=10 bash "$ROOT/scripts/container-start.sh"; then
    mark_rollback_failed "previous release failed to start"
  fi
  rm -f "$DRAIN_GATE" "$ROLLBACK_FAILED"
  echo "Promotion failed and was rolled back: $reason" >&2
  exit 1
}

touch "$DRAIN_GATE"
if ! wait_until_idle; then
  rm -f "$DRAIN_GATE"
  exit 1
fi

CUTOVER_STARTED=$SECONDS
CUTOVER_DEADLINE_MS=$(( $(date +%s%3N) + CUTOVER_TIMEOUT * 1000 ))
trap 'rollback_failed_promotion "unexpected failure at line $LINENO"' ERR
if ! CODERUS_CUTOVER_DEADLINE_MS="$CUTOVER_DEADLINE_MS" \
  CODERUS_FORCE_STOP=1 \
  bash "$ROOT/scripts/container-stop.sh"; then
  rollback_failed_promotion "current release did not stop"
fi
run_with_budget "$PYTHON" -m coderus.release_ops check-idle "$DATABASE" \
  || rollback_failed_promotion "work remained active after stop"

backup_candidate="$ROOT/backups/$(date -u +%Y%m%d-%H%M%S)-before-$RELEASE_ID.db"
run_with_budget "$PYTHON" -m coderus.release_ops backup "$DATABASE" "$backup_candidate" \
  || rollback_failed_promotion "database backup failed"
BACKUP="$backup_candidate"
run_with_budget "$PYTHON" -m coderus.release_ops migrate "$DATABASE" \
  || rollback_failed_promotion "database migration failed"
switch_link previous "$OLD_ID" || rollback_failed_promotion "cannot update previous link"
switch_link current "$RELEASE_ID" || rollback_failed_promotion "cannot update current link"

nohup "$PYTHON" -m coderus serve \
  --runtime maintenance \
  --config "$ROOT/config.yaml" \
  --secrets "$ROOT/secrets.env" \
  --port "$PORT" \
  >"$ROOT/data/logs/maintenance.log" 2>&1 </dev/null 9>&- &
MAINTENANCE_PID=$!
maintenance_ready=0
while (( SECONDS - CUTOVER_STARTED < CUTOVER_TIMEOUT )); do
  kill -0 "$MAINTENANCE_PID" 2>/dev/null \
    || rollback_failed_promotion "maintenance exited during startup"
  remaining="$(remaining_seconds)" || break
  if curl --connect-timeout "$remaining" --max-time "$remaining" \
    -fsS "http://127.0.0.1:${PORT}/readyz" >/dev/null; then
    maintenance_ready=1
    break
  fi
  sleep 0.2
done
(( maintenance_ready == 1 )) || rollback_failed_promotion "maintenance readiness timed out"
kill "$MAINTENANCE_PID" || rollback_failed_promotion "cannot stop maintenance"
while (( $(date +%s%3N) < CUTOVER_DEADLINE_MS )); do
  kill -0 "$MAINTENANCE_PID" 2>/dev/null || break
  sleep 0.1
done
if kill -0 "$MAINTENANCE_PID" 2>/dev/null; then
  kill -KILL "$MAINTENANCE_PID" \
    || rollback_failed_promotion "cannot terminate maintenance"
fi
wait "$MAINTENANCE_PID" 2>/dev/null || true
MAINTENANCE_PID=""

remaining_budget >/dev/null || rollback_failed_promotion "active startup deadline exhausted"
if ! CODERUS_CUTOVER_DEADLINE_MS="$CUTOVER_DEADLINE_MS" \
  bash "$ROOT/scripts/container-start.sh"; then
  rollback_failed_promotion "new release failed to start"
fi
remaining="$(remaining_seconds)" || rollback_failed_promotion "readiness deadline exhausted"
curl --connect-timeout "$remaining" --max-time "$remaining" \
  -fsS "http://127.0.0.1:${PORT}/readyz" >/dev/null \
  || rollback_failed_promotion "new release lost readiness"
sleep 0.2
remaining="$(remaining_seconds)" || rollback_failed_promotion "stability deadline exhausted"
curl --connect-timeout "$remaining" --max-time "$remaining" \
  -fsS "http://127.0.0.1:${PORT}/readyz" >/dev/null \
  || rollback_failed_promotion "new release readiness was not stable"
(( SECONDS - CUTOVER_STARTED <= CUTOVER_TIMEOUT )) \
  || rollback_failed_promotion "cutover exceeded ${CUTOVER_TIMEOUT} seconds"

trap - ERR
history_payload="$(printf \
  '{"release_id":"%s","previous_id":"%s","backup":"%s","cutover_seconds":%d}' \
  "$RELEASE_ID" "$OLD_ID" "$BACKUP" "$((SECONDS - CUTOVER_STARTED))")"
"$PYTHON" -m coderus.release_ops write-history "$ROOT/data/release-history" \
  "$history_payload" --retain "$HISTORY_RETAIN" \
  || rollback_failed_promotion "cannot write release history"
rm -f "$DRAIN_GATE" "$ROLLBACK_FAILED"
"$PYTHON" -m coderus.release_ops prune-artifacts "$ROOT" \
  --retain-releases "$RELEASE_RETAIN" --retain-backups "$BACKUP_RETAIN" \
  || echo "Warning: release artifact retention failed" >&2
echo "Promoted Coderus to $RELEASE_ID in $((SECONDS - CUTOVER_STARTED)) seconds"
