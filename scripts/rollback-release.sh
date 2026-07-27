#!/usr/bin/env bash
set -euo pipefail

umask 077
ROOT="${CODERUS_ROOT:-/opt/coderus}"
DATABASE="${CODERUS_DATABASE:-$ROOT/data/coderus.db}"
DRAIN_GATE="$ROOT/data/release-draining"
LOCK_FILE="$ROOT/data/release.lock"
ROLLBACK_FAILED="$ROOT/data/ROLLBACK_FAILED"

exec 9>"$LOCK_FILE"
flock -n 9 || { echo "Another release operation is running" >&2; exit 1; }
[[ -L "$ROOT/current" && -L "$ROOT/previous" ]] || {
  echo "Current or previous release is missing" >&2
  exit 1
}
CURRENT="$(readlink -f "$ROOT/current")"
PREVIOUS="$(readlink -f "$ROOT/previous")"
CURRENT_ID="$(basename "$CURRENT")"
PREVIOUS_ID="$(basename "$PREVIOUS")"
[[ "$CURRENT" == "$ROOT/releases/$CURRENT_ID" ]] || exit 1
[[ "$PREVIOUS" == "$ROOT/releases/$PREVIOUS_ID" ]] || exit 1
[[ "$CURRENT_ID" != "$PREVIOUS_ID" ]] || { echo "No previous release available"; exit 0; }
PYTHON="$CURRENT/.venv/bin/python"
cd "$CURRENT"

switch_link() {
  local pointer="$1"
  local release_id="$2"
  rm -f "$ROOT/$pointer"
  ln -s "releases/$release_id" "$ROOT/$pointer"
  [[ "$(readlink "$ROOT/$pointer")" == "releases/$release_id" ]]
}

fail_closed() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >"$ROLLBACK_FAILED"
  echo "ROLLBACK FAILED: $1; drain gate remains enabled" >&2
  exit 2
}

touch "$DRAIN_GATE"
"$PYTHON" -m coderus.release_ops check-idle "$DATABASE" || {
  rm -f "$DRAIN_GATE"
  exit 1
}
"$PYTHON" -m coderus.release_ops check-schema "$DATABASE" \
  "$PREVIOUS/release.json" || {
  rm -f "$DRAIN_GATE"
  echo "Previous release does not support the current database schema" >&2
  exit 1
}
CODERUS_STOP_TIMEOUT=5 bash "$ROOT/scripts/container-stop.sh" \
  || fail_closed "current release did not stop"
switch_link current "$PREVIOUS_ID" || fail_closed "cannot switch current link"
switch_link previous "$CURRENT_ID" || fail_closed "cannot switch previous link"

if ! CODERUS_START_TIMEOUT=10 bash "$ROOT/scripts/container-start.sh"; then
  CODERUS_STOP_TIMEOUT=3 bash "$ROOT/scripts/container-stop.sh" \
    || fail_closed "failed target process did not stop"
  switch_link current "$CURRENT_ID" || fail_closed "cannot restore current link"
  switch_link previous "$PREVIOUS_ID" || fail_closed "cannot restore previous link"
  CODERUS_START_TIMEOUT=10 bash "$ROOT/scripts/container-start.sh" \
    || fail_closed "original release failed to restart"
  rm -f "$DRAIN_GATE"
  echo "Rollback startup failed; restored $CURRENT_ID" >&2
  exit 1
fi
rm -f "$DRAIN_GATE" "$ROLLBACK_FAILED"
echo "Rolled back Coderus to $PREVIOUS_ID"
