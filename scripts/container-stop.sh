#!/usr/bin/env bash
set -euo pipefail

ROOT="${CODERUS_ROOT:-/opt/coderus}"
PID_FILE="$ROOT/data/coderus.pid"
STOP_TIMEOUT="${CODERUS_STOP_TIMEOUT:-15}"
FORCE_STOP="${CODERUS_FORCE_STOP:-0}"
DEADLINE_MS="${CODERUS_CUTOVER_DEADLINE_MS:-}"

now_ms() {
  date +%s%3N
}

if [[ -z "$DEADLINE_MS" ]]; then
  DEADLINE_MS=$(( $(now_ms) + STOP_TIMEOUT * 1000 ))
fi
[[ -L "$ROOT/current" ]] || { echo "Current release is not configured" >&2; exit 1; }
RELEASE="$(readlink -f "$ROOT/current")"

if [[ ! -f "$PID_FILE" ]]; then
  echo "Coderus is not running"
  exit 0
fi

pid="$(<"$PID_FILE")"
if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "Removed stale Coderus PID file"
  exit 0
fi

command_line="$(tr '\0' ' ' < "/proc/$pid/cmdline")"
process_cwd="$(readlink -f "/proc/$pid/cwd")"
if [[ "$command_line" != *"coderus serve"* \
  || "$command_line" != *"--config $ROOT/config.yaml"* \
  || "$command_line" == *"--runtime preview"* ]]; then
  echo "PID $pid does not belong to Coderus; refusing to stop it" >&2
  exit 1
fi
if [[ "$command_line" == *"--runtime active"* ]]; then
  [[ "$process_cwd" == "$RELEASE" ]] || {
    echo "PID $pid belongs to a different Coderus release" >&2
    exit 1
  }
else
  [[ -f "$RELEASE/LEGACY_RUNTIME" ]] || {
    echo "PID $pid is not the managed legacy Coderus process" >&2
    exit 1
  }
  if [[ -f "$RELEASE/LEGACY_ROOT_CWD" ]]; then
    [[ "$process_cwd" == "$ROOT" ]] || exit 1
  else
    [[ "$process_cwd" == "$RELEASE" ]] || exit 1
  fi
fi

kill "$pid"
grace_deadline="$DEADLINE_MS"
if [[ "$FORCE_STOP" == "1" ]]; then
  now="$(now_ms)"
  remaining_ms=$((DEADLINE_MS - now))
  if (( remaining_ms > 1 )); then
    reserve_ms=500
    (( reserve_ms < remaining_ms )) || reserve_ms=$((remaining_ms / 2))
    grace_deadline=$((DEADLINE_MS - reserve_ms))
  fi
fi
while (( $(now_ms) < grace_deadline )); do
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "Coderus stopped"
    exit 0
  fi
  sleep 0.2
done

if [[ "$FORCE_STOP" == "1" ]]; then
  kill -KILL "$pid" 2>/dev/null || true
  while (( $(now_ms) < DEADLINE_MS )); do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$PID_FILE"
      echo "Coderus force-stopped after graceful timeout"
      exit 0
    fi
    sleep 0.1
  done
fi
if ! kill -0 "$pid" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "Coderus force-stopped after graceful timeout"
  exit 0
fi
echo "Coderus did not stop before its deadline" >&2
exit 1
